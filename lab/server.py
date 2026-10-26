"""Deskhand Lab — a harness that runs each checkpoint and explains what it did.

The lab never reimplements checkpoint logic.

- Checkpoints 00 and 01 run in their own subprocess (cwd = that checkpoint
  folder) through the runners in lab/runners/, which import the checkpoint
  unmodified and observe it from the outside.
- Checkpoints 02–04 are FastAPI services. The lab starts them on their own
  port (one click, or the printed command), talks to them over the same HTTP
  contract curl uses (POST /tickets, GET /runs/{id}/events, POST /approve,
  GET /runs/{id}) and reads the log lines they append to logs/runs.jsonl.

Two checkpoints are never imported into one Python process — they all have an
llm.py, models.py, errors.py ...

Run (from the repo root):   py lab\\server.py          -> http://localhost:8010
Binds to 127.0.0.1 only: every run spends real money on your API key.

HTTP contract (the UI in lab/static/ is one client of it):
- GET  /api/health                      LLM config (never keys), mock API, services
- GET  /api/tickets                     shared dataset + Session 1 demos + checkpoint catalogue
- GET  /api/incidents                   the ten v1 incidents, mapped to tickets and checkpoints
- POST /api/run                         run one checkpoint on one ticket -> NDJSON stream
- POST /api/runs/{run_id}/approve       answer a paused 02–04 run's approval gate
- POST /api/estimate                    rough cost of N runs, for the confirm dialog
- GET  /api/runs/{run_id}               a finished trace (the UI's #run/<id> links)
- POST /api/services/{name}/start|stop  manage the mock API and the 02–04 services
                                        (stop also ends one started in a terminal);
                                        start takes whitelisted flags, passed only as
                                        the environment of that one process
- GET  /api/services/{name}/log         the last lines a managed service printed
- POST /api/follow                      follow an existing 02–04 request_id from event 0
- GET  /api/follow/recent               request_ids the lab submitted (survives an F5)
- POST /api/search                      checkpoint 03's own retrieve() on a bare query
- GET  /api/logs                        01–04's logs/runs.jsonl as one stream (the Logs tab):
                                        ?tail=N, ?after=<cursor> or ?request_id=<id>
"""

from __future__ import annotations

import ast
import asyncio
import atexit
import hashlib
import math
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

# How one run flows (POST /api/run):
#   1. The browser posts {checkpoint, ticket ref}. resolve_ticket() turns the
#      ref into what each checkpoint reads: free text for 00, a ticket dict
#      for 01, a ticket_id or ticket_text body for 02–04.
#   2. The answer is a StreamingResponse of NDJSON, one JSON event per line.
#      The first line is a "start" event. core.js runStream() reads the lines
#      as they arrive and hands each one to the view.
#   3. 00 and 01: _spawn() hands the request to lab/runners/cpXX.py (cwd =
#      the checkpoint folder), started ahead of time by the warm pool when it
#      can be, and forwards the runner's events (redacted).
#      02–04: _run_service() POSTs /tickets to the running service, then
#      _watch() reads its SSE stream and re-emits each event as {"type": "sse"}.
#   4. The last line is a "trace". _finish_trace() enriches it (code links,
#      costs, order facts, commands, a summary), keeps it in RUNS for the
#      #run/<id> links, and records its cost for the next estimate.

LAB_DIR = Path(__file__).resolve().parent
REPO = LAB_DIR.parent
RUNNERS = LAB_DIR / "runners"
STATIC = LAB_DIR / "static"
TICKETS_PATH = REPO / "shared" / "data" / "tickets.json"
ORDERS_SEED = REPO / "shared" / "mock_order_api" / "orders_seed.json"
POLICIES_DIR = REPO / "shared" / "policies"
MOCK_DIR = REPO / "shared" / "mock_order_api"
ENV_PATH = REPO / ".env"

LAB_PORT = int(os.getenv("LAB_PORT", "8010"))
# Ports probed for a running 02–04 service. 8000 is uvicorn's default, for a
# service a student started without --port; services are found by title.
SERVICE_PORTS = [
    int(p) for p in os.getenv("LAB_SERVICE_PORTS", "8000,8002,8003,8004").split(",")
]
RUN_TIMEOUT_S = 300  # a run that is actually working
APPROVAL_TIMEOUT_S = 900  # a run parked at the approval gate, waiting for a human
SERVICE_START_TIMEOUT_S = 45  # until a started service answers /openapi.json
RECONNECT_WINDOW_S = 60  # how long a run keeps retrying a service that went down
INGEST_TIMEOUT_S = 180  # `py ingest.py` before the first 03/04 start
MAX_REPEAT = 20  # most runs one Repeat batch or estimate may ask for
SERVER_MTIME = (
    Path(__file__).stat().st_mtime
)  # to notice when this file changes under a running server
TICKETS_PER_MONTH = 50_000  # the 00 README's "Try this" #2 projection

# The checkpoint catalogue. `kind` decides the path through this file:
# "subprocess" checkpoints run through `runner` once per request; "http"
# checkpoints are long-running services the lab talks to on `port`. `setup`
# is the one-time command printed before the start command.
CHECKPOINTS: dict[str, dict[str, Any]] = {
    "00": {
        "name": "Broken bot",
        "dir": "checkpoints/00-broken-bot",
        "runner": "cp00.py",
        "kind": "subprocess",
        "sessions": "Session 1",
        "blurb": "Day one's bot: one giant prompt, nothing checked. The “before” picture.",
    },
    "01": {
        "name": "Triage",
        "dir": "checkpoints/01-triage",
        "runner": "cp01.py",
        "kind": "subprocess",
        "sessions": "Sessions 2–3",
        "blurb": "Sorts a ticket into four labels, checks the answer, asks once more if it's broken, logs every call.",
    },
    "02": {
        "name": "Service",
        "dir": "checkpoints/02-service",
        "kind": "http",
        "port": 8002,
        "sessions": "Sessions 4–6",
        "blurb": "Works a ticket step by step: looks up the order, checks the plan against rules, asks a person before big refunds, never pays twice.",
    },
    "03": {
        "name": "RAG",
        "dir": "checkpoints/03-rag",
        "kind": "http",
        "port": 8003,
        "sessions": "Sessions 7–9",
        "setup": "py ingest.py",
        "blurb": "Looks up the policies before it plans. Every refund or replacement must cite the section it used.",
    },
    "04": {
        "name": "Production",
        "dir": "checkpoints/04-production",
        "kind": "http",
        "port": 8004,
        "sessions": "Sessions 10–12",
        "setup": "py ingest.py",
        "blurb": "Screens tickets for attacks, checks the reply, caps the spend per run, has a kill switch. Eight of v1's ten problems fixed, two partly.",
    },
}
# The students' copy of the repo grows one checkpoint at a time, so the lab
# offers only the checkpoints whose folder is on disk.
CHECKPOINTS = {k: v for k, v in CHECKPOINTS.items() if (REPO / v["dir"]).is_dir()}

# Where each behaviour lives. Located by content at request time, so the line
# numbers stay right while the checkpoints are being edited.
# Each entry is (key, file, needle): cite() returns the first line that
# contains the needle. A needle that no longer matches comes back marked
# "moved", never as a wrong line.
ANCHORS: dict[str, list[tuple[str, str, str]]] = {
    "00": [
        ("policies_glob", "checkpoints/00-broken-bot/bot.py", "glob.glob("),
        ("prompt", "checkpoints/00-broken-bot/bot.py", 'prompt = f"""'),
        ("generous", "checkpoints/00-broken-bot/bot.py", "Be generous"),
        ("loop", "checkpoints/00-broken-bot/bot.py", "while True"),
        ("temperature", "checkpoints/00-broken-bot/bot.py", "temperature=1.0"),
        ("stream_print", "checkpoints/00-broken-bot/bot.py", 'print(delta, end=""'),
        ("json_loads", "checkpoints/00-broken-bot/bot.py", "json.loads(raw)"),
        ("bare_except", "checkpoints/00-broken-bot/bot.py", "except Exception:"),
        ("give_up", "checkpoints/00-broken-bot/bot.py", "attempts >= 3"),
        ("return_none", "checkpoints/00-broken-bot/bot.py", "return None"),
        ("refund_if", "checkpoints/00-broken-bot/bot.py", 'plan.get("refund_amount")'),
        ("refund_issued", "checkpoints/00-broken-bot/bot.py", "refund issued"),
        ("incidents", "checkpoints/00-broken-bot/incidents.py", "INCIDENTS = ["),
    ],
    "01": [
        (
            "main_request_id",
            "checkpoints/01-triage/main.py",
            "request_id = str(uuid.uuid4())",
        ),
        ("main_run_started", "checkpoints/01-triage/main.py", '"run.started"'),
        (
            "main_user_message",
            "checkpoints/01-triage/main.py",
            "errors.user_message(exc.internal_error_code)",
        ),
        ("system_prompt", "checkpoints/01-triage/triage.py", 'SYSTEM_PROMPT = """'),
        ("max_repair", "checkpoints/01-triage/triage.py", "MAX_REPAIR_ATTEMPTS = "),
        (
            "not_repairable",
            "checkpoints/01-triage/triage.py",
            "if exc.internal_error_code != errors.LLM_MALFORMED_OUTPUT",
        ),
        ("repair_log", "checkpoints/01-triage/triage.py", '"triage.repair"'),
        (
            "repair_message",
            "checkpoints/01-triage/triage.py",
            "Your previous response was not a valid TriageResult",
        ),
        ("cost_add", "checkpoints/01-triage/triage.py", "cost_usd += usage.cost_usd"),
        ("completed_log", "checkpoints/01-triage/triage.py", '"triage.completed"'),
        ("exhausted", "checkpoints/01-triage/triage.py", "raise errors.DeskhandError("),
        ("parse", "checkpoints/01-triage/llm.py", "beta.chat.completions.parse("),
        (
            "length_error",
            "checkpoints/01-triage/llm.py",
            "except openai.LengthFinishReasonError",
        ),
        (
            "rate_limited",
            "checkpoints/01-triage/llm.py",
            "except openai.RateLimitError",
        ),
        ("parsed_none", "checkpoints/01-triage/llm.py", "if message.parsed is None"),
        ("temperature", "checkpoints/01-triage/llm.py", "temperature: float = 0.0"),
        ("price_table", "checkpoints/01-triage/llm.py", "PRICE_TABLE: dict"),
        ("cost_fn", "checkpoints/01-triage/llm.py", "def _cost_usd"),
        ("user_messages", "checkpoints/01-triage/errors.py", "_USER_MESSAGES: dict"),
        ("user_message", "checkpoints/01-triage/errors.py", "def user_message"),
        ("triage_result", "checkpoints/01-triage/models.py", "class TriageResult"),
        ("log_path", "checkpoints/01-triage/obslog.py", "LOG_PATH = "),
        ("log_event", "checkpoints/01-triage/obslog.py", "def log_event"),
    ],
}


def _service_anchors(cp: str) -> list[tuple[str, str, str]]:
    """The 02–04 anchors share names; each checkpoint's own files are cited."""
    d = CHECKPOINTS[cp]["dir"]
    common = [
        ("normalize", f"{d}/nodes.py", "def normalize_ticket("),
        ("triage_node", f"{d}/nodes.py", "def triage("),
        ("triage_repair", f"{d}/nodes.py", '"triage.repair"'),
        ("extract_order_id", f"{d}/nodes.py", "def _extract_order_id("),
        ("retry_policy", f"{d}/nodes.py", "def _order_api_call("),
        ("lookup_order", f"{d}/nodes.py", '@tool("lookup_order"'),
        ("issue_refund", f"{d}/nodes.py", '@tool("issue_refund"'),
        ("fetch_order", f"{d}/nodes.py", "def fetch_order_data("),
        ("order_not_found", f"{d}/nodes.py", '"order.not_found"'),
        ("plan_prompt", f"{d}/nodes.py", "PLAN_SYSTEM_PROMPT = ("),
        ("generate_plan", f"{d}/nodes.py", "def generate_resolution_plan("),
        ("validate_plan", f"{d}/nodes.py", "def validate_plan("),
        ("max_repair", f"{d}/nodes.py", "MAX_REPAIR_ATTEMPTS = "),
        ("repair_plan", f"{d}/nodes.py", "def repair_plan("),
        ("repair_exhausted", f"{d}/nodes.py", "errors.REPAIR_EXHAUSTED,"),
        ("approval_gate", f"{d}/nodes.py", "def approval_gate("),
        ("pause", f"{d}/nodes.py", "return PAUSE"),
        ("execute_actions", f"{d}/nodes.py", "def execute_actions("),
        (
            "idempotency_key",
            f"{d}/nodes.py",
            'idempotency_key = f"{state.request_id}:{index}"',
        ),
        ("draft_reply", f"{d}/nodes.py", "def draft_customer_reply("),
        ("reply_delta", f"{d}/nodes.py", "emit(events.REPLY_DELTA"),
        ("rejected_reply", f"{d}/nodes.py", "REJECTED by a human reviewer"),
        ("finalize", f"{d}/nodes.py", "def finalize("),
        ("handle_failure", f"{d}/nodes.py", "def handle_failure("),
        ("nodes_registry", f"{d}/nodes.py", "NODES: dict"),
        ("rule1", f"{d}/rules.py", "# Rule 1"),
        ("rule2", f"{d}/rules.py", "# Rule 2"),
        ("rule3", f"{d}/rules.py", "# Rule 3"),
        ("rule4", f"{d}/rules.py", "# Rule 4"),
        ("auto_approve_limit", f"{d}/rules.py", "AUTO_APPROVE_LIMIT_USD = "),
        ("events_contract", f"{d}/events.py", "REPLY_DELTA = "),
        ("publish", f"{d}/events.py", "def publish("),
        ("save_state", f"{d}/store.py", "def save_state("),
        ("append_event", f"{d}/store.py", "def append_event("),
        ("events_since", f"{d}/store.py", "def events_since("),
        ("user_messages", f"{d}/errors.py", "USER_MESSAGES: dict"),
        ("user_message_for", f"{d}/errors.py", "def user_message_for"),
        ("post_tickets", f"{d}/app.py", "def submit_ticket("),
        ("approve_endpoint", f"{d}/app.py", "def approve_run("),
        ("events_endpoint", f"{d}/app.py", "def run_events("),
        ("get_run", f"{d}/app.py", "def get_run("),
        ("interrupt", f"{d}/graph_lg.py", "decision = interrupt("),
        ("wrap_error", f"{d}/graph_lg.py", "except Exception as exc:"),
        ("log_event", f"{d}/obslog.py", "def log_event"),
        ("structured_output", f"{d}/llm.py", "with_structured_output("),
        ("stream_text", f"{d}/llm.py", "def stream_text("),
        ("price_table", f"{d}/llm.py", "PRICE_TABLE"),
    ]
    if cp == "02":
        common += [
            ("plain_loop", f"{d}/graph.py", "while node is not None"),
            ("plain_save", f"{d}/graph.py", "store.save_state("),
            ("runtime_switch", f"{d}/runtime.py", "DESKHAND_RUNTIME"),
        ]
    if cp in ("03", "04"):
        common += [
            ("retrieve_policy", f"{d}/nodes.py", "def retrieve_policy("),
            ("policy_retrieved", f"{d}/nodes.py", '"policy.retrieved"'),
            ("contradiction_rule", f"{d}/nodes.py", "CONTRADICT each other"),
            ("cite_only_given", f"{d}/nodes.py", "never invent one"),
            ("rule5", f"{d}/rules.py", "# Rule 5"),
            ("top_k", f"{d}/retrieve.py", "TOP_K = "),
            ("build_query", f"{d}/retrieve.py", "def build_query("),
            ("embed", f"{d}/llm.py", "def embed_texts("),
        ]
    if cp == "04":
        common += [
            ("guard_input", f"{d}/nodes.py", "def guard_input("),
            ("input_guard", f"{d}/guardrails.py", "def input_guard("),
            ("injection_patterns", f"{d}/guardrails.py", "_INJECTION_PATTERNS"),
            ("guard_classifier", f"{d}/guardrails.py", "_GUARD_SYSTEM = ("),
            ("output_guard", f"{d}/guardrails.py", "def output_guard("),
            ("output_guard_call", f"{d}/nodes.py", "guardrails.output_guard("),
            ("safe_fallback", f"{d}/guardrails.py", "SAFE_FALLBACK_REPLY = "),
            ("budget_cap", f"{d}/budget.py", "BUDGET_USD_PER_RUN = "),
            ("charge", f"{d}/budget.py", "def charge("),
            ("model_triage", f"{d}/budget.py", "MODEL_TRIAGE = "),
            ("model_plan", f"{d}/budget.py", "MODEL_PLAN = "),
        ]
    return common


for _cp in ("02", "03", "04"):
    if _cp in CHECKPOINTS:
        ANCHORS[_cp] = _service_anchors(_cp)

# The ten v1 incidents from the Session 1 scoreboard, each mapped to a ticket
# that tends to reproduce it on checkpoint 00 and to the checkpoint that
# retires it. Ticket ids are from shared/data/tickets.json; letters are the
# Session 1 demos in checkpoints/00-broken-bot/incidents.py.
# "retired" follows the decks' scoreboards (Session 2 retires #2, Session 4
# #3, Session 5 #1 #5 #9, Session 9 #6, Session 11 #7 #10). An incident the
# decks only ever mark PARTIAL carries "partial": True, and the board says
# "narrowed" instead of "retired" for it.
INCIDENT_BOARD: list[dict[str, Any]] = [
    {
        "n": 1,
        "text": "Sometimes it returns invalid resolution schemas.",
        "reproduce": {"incident": "A"},
        "retired": {"checkpoint": "02", "incident": "A"},
        "watch": "On 00, json.loads blows up on JSON cut off at max_tokens (finish_reason “length”, never checked). On 01 the provider is handed the schema and Pydantic parses the triage labels (partial: the resolution plan doesn't exist yet). On 02 the plan gets the same schema plus business rules, which retires it.",
    },
    {
        "n": 2,
        "text": "Sometimes it sends customers a generic LLM apology instead of an answer.",
        "reproduce": {"incident": "B"},
        "retired": {"checkpoint": "01", "incident": "B"},
        "watch": "On 00, “let me check the shipping status for you” is the answer: model text goes straight to the customer. On 01 the model can only return four labels, and a failure ends in a sentence from errors.py, never in model text. 02 then drafts the reply from a validated plan.",
    },
    {
        "n": 3,
        "text": "Sometimes partial failures are streamed to the support agent's screen.",
        "reproduce": {"incident": "A"},
        "retired": {"checkpoint": "02", "incident": "A"},
        "watch": "Read “What the customer saw” on 00: a half-built JSON reply for every retry. On 02 only reply.delta streams; the plan arrives whole or not at all.",
    },
    {
        "n": 4,
        "text": "Retries are not always effective.",
        "reproduce": {"incident": "A"},
        "retired": {
            "checkpoint": "01",
            "incident": "A",
            "inject_truncate": 1,
            "partial": True,
        },
        "watch": "00 resends the identical prompt up to three times. 01 repairs once and says what was wrong — switch fault injection on to watch the repair run. Still partial on every scoreboard: a busy provider is passed up unchanged, and 02's order-API retries only cover the tool.",
    },
    {
        "n": 5,
        "text": "The order-lookup API may fail, time out, or return inconsistent data.",
        "reproduce": {"ticket_id": "T-1001"},
        "retired": {"checkpoint": "02", "ticket_id": "T-1001"},
        "watch": "00 never calls the order API at all. 02 wraps it in a tool with a 5s timeout, two retries and taxonomy-coded failures. To see them, open Setup, stop the mock API and start it again with chaos “flaky” (MOCK_CHAOS=flaky); start it with “none” afterwards.",
    },
    {
        "n": 6,
        "text": "It once suggested a refund larger than the order total.",
        "reproduce": {"incident": "C"},
        "retired": {"checkpoint": "03", "incident": "C"},
        "watch": "00 believes the customer's $249.99 for a $24.99 order, is told to “be generous”, and nothing checks the amount. 02 looks the order up; rule 1 caps any refund at the total and sends an ask above the total to a human, and anything over $50 waits for approval (partial). 03 also grounds the amount in the retrieved policy, which retires it.",
    },
    {
        "n": 7,
        "text": "Costs spike on long ticket threads.",
        "reproduce": {"ticket_id": "T-1031"},
        "retired": {"checkpoint": "04", "ticket_id": "T-1031"},
        "watch": "Open “The prompt” on 00: every policy document is in every call. 03 retrieves six sections instead; 04 adds the per-run budget cap, which retires it.",
    },
    {
        "n": 8,
        "text": "The same ticket can produce different resolutions.",
        "reproduce": {"incident": "C", "view": "repeat"},
        "retired": {
            "checkpoint": "02",
            "incident": "C",
            "view": "repeat",
            "partial": True,
        },
        "watch": "Use Repeat. 00 samples at temperature 1.0 with no schema. 02 runs at temperature 0 with a schema and rules, so the plan rarely moves; 04 measures it as determinism_agreement and gates releases on it. Partial on every scoreboard: a model is never same input, same output.",
    },
    {
        "n": 9,
        "text": "A crash after issuing a refund left the ticket unresolved — and a retry refunded twice.",
        "reproduce": {"incident": "C"},
        "retired": {"checkpoint": "02", "ticket_id": "T-1001"},
        "watch": "00 prints “refund issued!” with no record. 02 saves state after every node and sends an Idempotency-Key with every refund, so a resumed run cannot pay twice. To crash it for real: in Setup, start 02 with “crash after refund” and run T-1004 (a $45 refund, no gate); when the service dies, start it again without the flag and the run resumes on the page.",
    },
    {
        "n": 10,
        "text": "Nobody can debug why a specific run failed.",
        "reproduce": {"incident": "A"},
        "retired": {"checkpoint": "04", "incident": "A"},
        "watch": "00 leaves no request ID, no log line, no error code. 01 writes one JSON line per event under a request_id; 02 persists state and events in SQLite too (partial on the scoreboard from Session 4); 04's dashboard groups failures by code, which retires it.",
    },
]

app = FastAPI(title="Deskhand Lab")


class _RevalidateUI:
    """The page and lab/static/ are revalidated on every load (no-cache: the
    browser asks, and an unchanged file comes back as a 304 via its ETag).
    Without it, a browser keeps an old views.js after the lab is updated and
    a new tab in index.html routes to nothing. Plain ASGI, so the run
    streams (/api/run, /api/follow) pass through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/" or path.startswith("/static/")):
            return await self.app(scope, receive, send)

        async def send_no_cache(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"cache-control"
                ]
                message = {
                    **message,
                    "headers": headers + [(b"cache-control", b"no-cache")],
                }
            await send(message)

        await self.app(scope, receive, send_no_cache)


app.add_middleware(_RevalidateUI)

# ---------------------------------------------------------------- env & secrets
# .env is read again on every call with dotenv_values(), never load_dotenv():
# an edit shows up without a restart, and the keys never enter the lab's own
# environment. The child processes load .env themselves. Nothing here writes it.


def _env() -> dict[str, str]:
    values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    return {k: (v or "") for k, v in values.items()}


def _secrets() -> list[str]:
    """Every value in .env whose name looks secret. Used only to redact."""
    names = re.compile(r"KEY|SECRET|TOKEN|PASSWORD", re.I)
    # Short values are skipped: replacing "1" or "true" everywhere would wreck the output.
    return [v for k, v in _env().items() if names.search(k) and len(v) >= 8]


def redact(text: str) -> str:
    """Replace every .env secret value in text. Runner events, runner stderr
    and managed-service logs pass through here before they reach the page."""
    for secret in _secrets():
        text = text.replace(secret, "[redacted]")
    return text


def _is_set(value: str | None) -> bool:
    value = (value or "").strip()
    return bool(value) and not value.startswith(
        "your-"
    )  # "your-..." is the .env.example placeholder


def _v1_llm(env: dict[str, str]) -> dict:
    """What checkpoint 00 will actually call. bot.py only knows Azure and
    OpenAI: any other LLM_PROVIDER (anthropic included) falls through to its
    OpenAI branch, so it needs OPENAI_API_KEY and uses OPENAI_MODEL."""
    provider = (env.get("LLM_PROVIDER") or "azure").strip().lower()
    if provider == "azure":
        return {
            "provider": "azure",
            "model": env.get("AZURE_OPENAI_DEPLOYMENT") or None,
            "note": None,
            "ready": _is_set(env.get("AZURE_OPENAI_API_KEY"))
            and _is_set(env.get("AZURE_OPENAI_ENDPOINT"))
            and bool(env.get("AZURE_OPENAI_DEPLOYMENT")),
        }
    note = (
        None
        if provider == "openai"
        else (
            f"bot.py only knows Azure and OpenAI, so with LLM_PROVIDER={provider} it calls OpenAI "
            "(OPENAI_API_KEY, OPENAI_MODEL)"
        )
    )
    return {
        "provider": "openai",
        "model": env.get("OPENAI_MODEL") or "gpt-4o-mini",
        "note": note,
        "ready": _is_set(env.get("OPENAI_API_KEY")),
    }


# ---------------------------------------------------------------- model switch
# The header's model picker. It never edits .env: the choice travels as env
# vars on the processes the lab starts (runners, managed services), and every
# checkpoint's load_dotenv() leaves variables that are already set alone.
# The Claude ids come from LAB_HAIKU_MODEL / LAB_SONNET_MODEL (shell or .env).

MODEL_OPTIONS: dict[str, dict[str, str]] = {
    "gpt": {"label": "GPT"},
    "haiku": {
        "label": "Claude Haiku",
        "var": "LAB_HAIKU_MODEL",
        "default": "claude-haiku-4-5",
    },
    "sonnet": {
        "label": "Claude Sonnet",
        "var": "LAB_SONNET_MODEL",
        "default": "claude-sonnet-5",
    },
}

# The OpenAI-compatible providers the checkpoints accept (OPENAI_COMPATIBLE in
# each llm.py, defaults copied from there). With one of them in LLM_PROVIDER,
# the picker's first entry is that provider instead of GPT. key None = it runs
# on the laptop and needs no key.
COMPATIBLE: dict[str, dict[str, str | None]] = {
    "gemini": {
        "label": "Gemini",
        "model_var": "GEMINI_MODEL",
        "model": "gemini-3.5-flash-lite",
        "key": "GEMINI_API_KEY",
    },
    "openrouter": {
        "label": "OpenRouter",
        "model_var": "OPENROUTER_MODEL",
        "model": "openrouter/free",
        "key": "OPENROUTER_API_KEY",
    },
    "ollama": {
        "label": "Ollama",
        "model_var": "OLLAMA_MODEL",
        "model": "llama3.2",
        "key": None,
    },
    "lmstudio": {
        "label": "LM Studio",
        "model_var": "LMSTUDIO_MODEL",
        "model": "google/gemma-3-4b",
        "key": None,
    },
}


def compatible_model(provider: str, env: dict[str, str]) -> str:
    spec = COMPATIBLE[provider]
    return env.get(spec["model_var"]) or spec["model"]


def compatible_key_set(provider: str, env: dict[str, str]) -> bool:
    key = COMPATIBLE[provider]["key"]
    return key is None or _is_set(env.get(key))


def claude_model(choice: str, env: dict[str, str] | None = None) -> str:
    """The Claude model id behind "haiku" or "sonnet": shell, then .env, then the default."""
    opt = MODEL_OPTIONS[choice]
    env = _env() if env is None else env
    return (os.getenv(opt["var"]) or env.get(opt["var"]) or opt["default"]).strip()


# Lab-process memory only: a lab restart goes back to what .env says.
MODEL_CHOICE: dict[str, str | None] = {
    "id": None
}  # None = nothing picked yet: run as .env says


def _gpt_provider(env: dict[str, str]) -> str:
    """The OpenAI-family provider .env is set up for: LLM_PROVIDER if it is one
    (the compatible providers included), else whichever of Azure / OpenAI has
    a key."""
    provider = (env.get("LLM_PROVIDER") or "azure").strip().lower()
    if provider in ("azure", "openai", *COMPATIBLE):
        return provider
    return "azure" if _is_set(env.get("AZURE_OPENAI_API_KEY")) else "openai"


def _default_choice(env: dict[str, str]) -> str:
    """The picker entry that matches .env, shown before anyone picks."""
    if (env.get("LLM_PROVIDER") or "").strip().lower() != "anthropic":
        return "gpt"
    return "sonnet" if "sonnet" in (env.get("ANTHROPIC_MODEL") or "") else "haiku"


def current_choice() -> str:
    return MODEL_CHOICE["id"] or _default_choice(_env())


def model_overrides(
    checkpoint: str | None = None, choice: str | None = None
) -> dict[str, str]:
    """Env vars that put a checkpoint process on the picked model. Empty until
    the picker is used, so a lab nobody touches behaves exactly like .env."""
    choice = choice or MODEL_CHOICE["id"]
    if choice is None:
        return {}
    env = _env()
    gpt = _gpt_provider(env)
    if choice == "gpt" or checkpoint == "00":  # bot.py only knows Azure and OpenAI
        return {"LLM_PROVIDER": gpt}
    model = claude_model(choice, env)
    out = {
        "LLM_PROVIDER": "anthropic",
        "ANTHROPIC_MODEL": model,
        # plan generation (04's routing) on the same model, so the pick means what it says
        "ANTHROPIC_MODEL_STRONG": "",
        # Anthropic has no embeddings API: 03/04 keep embedding with the OpenAI-family key
        "EMBEDDING_PROVIDER": env.get("EMBEDDING_PROVIDER") or gpt,
    }
    if "haiku-4" in model:
        out["ANTHROPIC_EFFORT"] = ""  # Haiku 4.5 rejects the effort parameter
    return out


def effective_env(checkpoint: str | None = None) -> dict[str, str]:
    """.env with the picker's overrides on top. Used for what the lab shows
    and prices; the processes get os.environ plus model_overrides() and load
    .env themselves."""
    return {**_env(), **model_overrides(checkpoint)}


def _model_options(env: dict[str, str]) -> list[dict]:
    """The picker's entries, each with the model it resolves to and whether its key is set."""
    gpt = _gpt_provider(env)
    if gpt in COMPATIBLE:
        gpt_label, gpt_model, gpt_ready = (
            COMPATIBLE[gpt]["label"],
            compatible_model(gpt, env),
            compatible_key_set(gpt, env),
        )
    else:
        gpt_label = "GPT"
        gpt_model = (
            env.get("AZURE_OPENAI_DEPLOYMENT")
            if gpt == "azure"
            else (env.get("OPENAI_MODEL") or "gpt-4o-mini")
        )
        gpt_ready = (
            _is_set(env.get("AZURE_OPENAI_API_KEY"))
            and _is_set(env.get("AZURE_OPENAI_ENDPOINT"))
            if gpt == "azure"
            else _is_set(env.get("OPENAI_API_KEY"))
        )
    claude_ready = _is_set(env.get("ANTHROPIC_API_KEY"))
    out = [
        {
            "id": "gpt",
            "label": gpt_label,
            "provider": gpt,
            "model": gpt_model,
            "ready": gpt_ready,
        }
    ]
    for cid in ("haiku", "sonnet"):
        out.append(
            {
                "id": cid,
                "label": MODEL_OPTIONS[cid]["label"],
                "provider": "anthropic",
                "model": claude_model(cid, env),
                "ready": claude_ready,
                "not_ready": None if claude_ready else "set ANTHROPIC_API_KEY in .env",
            }
        )
    return out


# ------------------------------------------------------------------- citations
# Every code link in the UI is a citation: a repo-relative file, a line found
# by searching for a needle, and a vscode:// link that opens it in the editor.
# Lines are looked up at request time and cached by file mtime, so the links
# stay right while a checkpoint is edited during class.

_source_cache: dict[str, tuple[float, list[str]]] = {}  # rel path -> (mtime, lines)


def _source_lines(rel: str) -> list[str]:
    """A repo file's lines, re-read only when its mtime changes."""
    path = REPO / rel
    mtime = path.stat().st_mtime
    cached = _source_cache.get(rel)
    if cached is None or cached[0] != mtime:
        cached = (mtime, path.read_text(encoding="utf-8").splitlines())
        _source_cache[rel] = cached
    return cached[1]


def cite_line(rel: str, line: int | None) -> dict:
    """A citation for a known line: its text (for the tooltip) and an editor link."""
    path = REPO / rel
    text = ""
    if line and path.exists():
        lines = _source_lines(rel)
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    href = (
        "vscode://file/" + str(path).replace("\\", "/") + (f":{line}" if line else "")
    )
    return {"file": rel, "line": line, "text": text, "href": href}


def cite(rel: str, needle: str) -> dict:
    """A citation for the first line containing needle; "missing" or "moved" if none."""
    try:
        lines = _source_lines(rel)
    except OSError:
        return {**cite_line(rel, None), "missing": True}
    for i, line in enumerate(lines, start=1):
        if needle in line:
            return cite_line(rel, i)
    return {**cite_line(rel, None), "moved": needle}


def citations(checkpoint: str) -> dict[str, dict]:
    return {key: cite(rel, needle) for key, rel, needle in ANCHORS.get(checkpoint, [])}


def _repo_rel(path: str | None) -> str | None:
    """An absolute path from a runner -> repo-relative posix, or None outside the repo."""
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return None


def node_order(checkpoint: str) -> list[str]:
    """The node registry of nodes.py, in graph order — read, not copied."""
    cp = CHECKPOINTS[checkpoint]
    if cp["kind"] != "http":
        return []
    try:
        lines = _source_lines(f"{cp['dir']}/nodes.py")
    except OSError:
        return []
    names: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("NODES: dict"):
            inside = True
            continue
        if inside:
            if line.startswith("}"):
                break
            m = re.match(r'\s*"([a-z_]+)":', line)
            if m:
                names.append(m.group(1))
    return names


# --------------------------------------------------------------------- tickets
# A run names its ticket one of three ways: a dataset ticket_id, a Session 1
# incident letter, or free text. resolve_ticket() turns any of them into one
# dict that carries every shape a checkpoint might need.


def _tickets() -> list[dict]:
    return json.loads(TICKETS_PATH.read_text(encoding="utf-8"))["tickets"]


def _flatten(ticket: dict) -> str:
    """Dataset ticket -> one block of text for checkpoint 00, which only takes
    free text. Same shape checkpoint 02's app.py builds (_load_ticket_text), so
    00 and 02 read identical input in Compare mode."""
    header = f"Ticket {ticket['ticket_id']} from {ticket['customer_name']}"
    if ticket.get("order_id"):
        header += f" (order {ticket['order_id']})"
    body = "\n\n".join(f"[{m['from']}] {m['text']}" for m in ticket["messages"])
    return f"{header}\n{body}"


_incidents_cache: dict[str, Any] = {}


def incidents() -> list[dict]:
    """The Session 1 demos, read from checkpoint 00's own incidents.py by the
    00 runner (never copied into the lab)."""
    path = REPO / CHECKPOINTS["00"]["dir"] / "incidents.py"
    mtime = path.stat().st_mtime
    if _incidents_cache.get("mtime") != mtime:
        # incidents.py imports bot.py, so reading it is the runner's job: the
        # lab process never imports checkpoint code. Re-read when the file changes.
        events = list(_spawn("00", {"mode": "list"}))
        found = next((e for e in events if e.get("type") == "incidents"), None)
        if found is None:
            raise HTTPException(
                500, "could not read incidents.py: " + _first_error(events)
            )
        out = []
        for i, item in enumerate(found["incidents"]):
            match = re.search(r"Incident ([A-Z])", item["title"])
            letter = match.group(1) if match else chr(ord("A") + i)
            out.append({**item, "key": f"incident:{letter}", "letter": letter})
        _incidents_cache.update(mtime=mtime, items=out)
    return _incidents_cache["items"]


class TicketRef(BaseModel):
    ticket_id: str | None = None
    incident: str | None = None  # "A" | "B" | "C"
    text: str | None = None
    # Free text only: who the ticket is from. 02–04 get it as customer_name, so
    # their ownership rule can run; 00 and 01 have no such field and ignore it.
    customer_name: str | None = None


def resolve_ticket(ref: TicketRef) -> dict:
    """-> {key, label, text (for 00), ticket (dict, for 01), order_id}"""
    if ref.ticket_id:
        ticket = next((t for t in _tickets() if t["ticket_id"] == ref.ticket_id), None)
        if ticket is None:
            raise HTTPException(404, f"unknown ticket_id {ref.ticket_id}")
        return {
            "key": ticket["ticket_id"],
            "label": ticket["ticket_id"],
            "text": _flatten(ticket),
            "ticket": ticket,
            "order_id": ticket.get("order_id"),
            "source": "dataset",
        }
    if ref.incident:
        item = next(
            (i for i in incidents() if i["letter"] == ref.incident.upper()), None
        )
        if item is None:
            raise HTTPException(404, f"unknown incident {ref.incident}")
        text = item["text"]
        label = f"Incident {item['letter']}"
        key = item["key"]
        source = "incidents.py"
    elif ref.text and ref.text.strip():
        text = ref.text.strip()
        name = (ref.customer_name or "").strip()[:120] or None
        label = "custom" + (f" from {name}" if name else "")
        # Stable for the same text and sender, so OBSERVED_COST finds a re-run.
        key = (
            "custom:"
            + hashlib.sha256((text + "\x00" + (name or "")).encode()).hexdigest()[:8]
        )
        source = "custom"
    else:
        raise HTTPException(422, "provide ticket_id, incident, or text")
    if source != "custom":
        name = None
    # The lab's own guess at the order id, used only for its annotations
    # (lookup_order). The checkpoints find the order id themselves.
    match = re.search(r"\bO-\d{4,6}\b", text)
    # The 01 runner's one-ticket file: 01 ignores the "From" name, as it would any lab text.
    ticket = {
        "ticket_id": key.replace(":", "-"),
        "customer_name": "(lab)",
        "order_id": None,
        "channel": "lab",
        "messages": [{"from": "customer", "text": text}],
    }
    return {
        "key": key,
        "label": label,
        "text": text,
        "ticket": ticket,
        "customer_name": name,
        "order_id": match.group(0) if match else None,
        "source": source,
        "order_id_from_text": bool(match),
    }


# ------------------------------------------------------------------ subprocess
# Checkpoints 00 and 01, and 03's index tools, run as one short-lived runner
# process per request. A fresh process per run means the checkpoint's modules
# (llm, models, errors, ...) never meet another checkpoint's in sys.modules,
# imports resolve from the checkpoint folder exactly as `py bot.py` would, a
# hung run can be killed, and no state leaks from one run into the next.
# The wire format is in lab/runners/labproto.py: one JSON request on stdin,
# NDJSON events on stdout, one terminal event last.
#
# Starting a runner costs seconds before the checkpoint runs a line: Python
# imports the provider SDKs, thousands of files, slower still where an
# antivirus scans each one. So the lab keeps the NEXT runner started: a warm
# runner has imported those third-party packages (labproto.preload) and sits
# blocked on stdin. A run takes one and a replacement starts behind it. It is
# still one fresh process per run, and the checkpoint's own modules and .env
# still load after the request, so nothing above changes.
WARM_MAX = 3  # warm runners per key at most: Repeat keeps up to three runs in flight


class _Runner:
    """One runner process, with its stderr drained from the start so a chatty
    import cannot fill the pipe while the runner waits in the pool."""

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.stderr_tail: list[
            str
        ] = []  # the last 60 lines, reported if the runner dies without a terminal event
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        for raw in self.proc.stderr:
            self.stderr_tail.append(raw.decode("utf-8", errors="replace"))
            del self.stderr_tail[:-60]

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.alive():
            self.proc.kill()


# Keyed by (checkpoint, runner script, model picker overrides): a runner gets
# its env when it starts, so one started for another model pick is never used.
_warm: dict[tuple, list[_Runner]] = {}
_warm_busy: dict[tuple, int] = {}  # runs in flight per key
_warm_lock = threading.Lock()


def _runner_spec(
    checkpoint: str, runner: str | None
) -> tuple[tuple, list[str], Path, dict[str, str]]:
    """What a runner for this checkpoint starts with, and the pool key for it."""
    cp = CHECKPOINTS[checkpoint]
    script = RUNNERS / (runner or cp["runner"])
    overrides = model_overrides(checkpoint)
    # UTF-8 both ways on Windows; unbuffered so events arrive while the run is
    # still going. The picker's overrides beat .env because the checkpoints'
    # load_dotenv() never replaces a variable that is already set.
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        **overrides,
    }
    key = (checkpoint, script.name, tuple(sorted(overrides.items())))
    # The lab's own interpreter (the venv); cwd is what makes `import llm` the checkpoint's llm.py.
    return key, [sys.executable, str(script)], REPO / cp["dir"], env


def _top_up(key: tuple, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Start warm runners until the pool covers the runs in flight (1 to WARM_MAX)."""
    while True:
        with _warm_lock:
            pool = _warm.setdefault(key, [])
            pool[:] = [r for r in pool if r.alive()]
            if len(pool) >= min(WARM_MAX, max(1, _warm_busy.get(key, 0))):
                return
        runner = _Runner(
            argv, cwd, env
        )  # outside the lock: starting a process takes a moment
        with _warm_lock:
            _warm.setdefault(key, []).append(runner)


def _take_runner(checkpoint: str, runner: str | None) -> tuple[tuple, _Runner]:
    """A warm runner for this run if one is ready, else a cold one; either way
    a replacement starts in the background."""
    key, argv, cwd, env = _runner_spec(checkpoint, runner)
    taken = None
    with _warm_lock:
        for other in [k for k in _warm if k[:2] == key[:2] and k != key]:
            for stale in _warm.pop(
                other
            ):  # started for a model pick that is no longer current
                stale.kill()
        pool = _warm.setdefault(key, [])
        while pool and taken is None:
            candidate = pool.pop(0)
            if candidate.alive():
                taken = candidate
        _warm_busy[key] = _warm_busy.get(key, 0) + 1
    if taken is None:
        taken = _Runner(argv, cwd, env)
    threading.Thread(target=_top_up, args=(key, argv, cwd, env), daemon=True).start()
    return key, taken


def _release_runner(key: tuple) -> None:
    """A run ended. Once none are in flight, one warm runner per key is enough."""
    with _warm_lock:
        _warm_busy[key] = max(0, _warm_busy.get(key, 0) - 1)
        pool = _warm.get(key, [])
        if not _warm_busy[key]:
            for extra in pool[1:]:
                extra.kill()
            del pool[1:]


def prewarm(checkpoints: tuple[str, ...] = ("00", "01")) -> None:
    """Start a warm runner for each subprocess checkpoint, so the first run of
    the class does not wait on imports either."""
    for checkpoint in checkpoints:
        if (
            checkpoint in CHECKPOINTS
            and (REPO / CHECKPOINTS[checkpoint]["dir"]).is_dir()
        ):
            key, argv, cwd, env = _runner_spec(checkpoint, None)
            threading.Thread(
                target=_top_up, args=(key, argv, cwd, env), daemon=True
            ).start()


def _stop_warm() -> None:
    """Stop every idle runner. Each would also exit by itself on stdin EOF."""
    with _warm_lock:
        for pool in _warm.values():
            for runner in pool:
                runner.kill()
        _warm.clear()


atexit.register(_stop_warm)


def _first_error(events: list[dict]) -> str:
    """The detail of a runner's "error" event, redacted and cut to its tail."""
    for e in events:
        if e.get("type") == "error":
            return redact(str(e.get("detail") or e.get("message")))[-1500:]
    return "no output from runner"


def _spawn(
    checkpoint: str,
    request: dict,
    stop: threading.Event | None = None,
    runner: str | None = None,
) -> Iterator[dict]:
    """Run one runner subprocess (warm from the pool when one is ready); yield
    its NDJSON events (redacted).
    runner: a runner other than the checkpoint's own (03's search-only mode)."""
    key, taken = _take_runner(checkpoint, runner)
    proc, stderr_tail = taken.proc, taken.stderr_tail

    def watchdog() -> None:
        # Kill the runner after RUN_TIMEOUT_S, or as soon as `stop` is set (the page went away).
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while proc.poll() is None:
            if time.monotonic() > deadline or (stop is not None and stop.is_set()):
                proc.kill()
                return
            time.sleep(0.2)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        # lab_sent_at lets the runner report how long this run waited on it to start.
        proc.stdin.write(
            json.dumps(dict(request, lab_sent_at=time.time())).encode("utf-8")
        )
        proc.stdin.close()  # the runner reads stdin to EOF, so closing it starts the run
        saw_terminal = False
        for raw in proc.stdout:
            line = redact(raw.decode("utf-8", errors="replace")).strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = {
                    "type": "stdout",
                    "text": line + "\n",
                }  # not protocol: show it, don't drop it
            # Every runner mode ends with one of these: its result, or an error.
            if event.get("type") in {
                "trace",
                "incidents",
                "quote",
                "search",
                "distance",
                "map",
                "error",
            }:
                saw_terminal = True
            yield event
        proc.wait(timeout=10)
        if not saw_terminal:
            # stdout closed with no result: the runner died (killed, or crashed
            # before its own except). Its stderr says why.
            yield {
                "type": "error",
                "message": f"runner exited with code {proc.returncode}",
                "detail": redact("".join(stderr_tail))[-3000:],
            }
    finally:
        if proc.poll() is None:
            proc.kill()
        _release_runner(key)


# --------------------------------------------------------------------- pricing

_quote_cache: dict[tuple, dict] = {}


def price_quote(models: list[str]) -> dict[str, dict]:
    """USD per 1M tokens, as checkpoint 01's llm._cost_usd prices them.
    Checkpoint 00 has no price table — that's one of its lessons — so its runs
    are priced with the first checkpoint that has one."""
    llm_file = REPO / CHECKPOINTS["01"]["dir"] / "llm.py"
    # Keyed on llm.py's mtime: editing the price table reprices on the next call.
    # A miss spawns the 01 runner in "quote" mode, which costs nothing.
    key = (tuple(sorted(set(models))), llm_file.stat().st_mtime)
    if key not in _quote_cache:
        events = list(_spawn("01", {"mode": "quote", "models": list(key[0])}))
        found = next((e for e in events if e.get("type") == "quote"), None)
        if found is None:
            return {}
        _quote_cache[key] = found["prices"]
    return _quote_cache[key]


def _cost(prices: dict | None, usage: dict | None) -> float | None:
    """USD for one usage dict at per-1M-token prices; None when either is unknown."""
    if not prices or not usage:
        return None
    return (
        usage["prompt_tokens"] * prices["input"]
        + usage["completion_tokens"] * prices["output"]
    ) / 1_000_000


# ---------------------------------------------------------------------- orders


def _mock_base() -> str:
    """The mock order API's base URL, as the checkpoints read it from .env."""
    return (_env().get("MOCK_ORDER_API_URL") or "http://localhost:9000").rstrip("/")


# The local servers (mock API, 02–04, the lab) listen on 127.0.0.1 only. On
# Windows "localhost" resolves to ::1 first, and a refused connection takes
# about 2 s there before the next address is tried, so the lab's own requests
# go to the IPv4 loopback directly. A local server that is up accepts in
# milliseconds, so a short connect timeout only cuts the wait when it is down.
LOCAL_TIMEOUT = httpx.Timeout(1.5, connect=0.3)


def _loopback(url: str) -> str:
    """url with a localhost host swapped for 127.0.0.1; any other host unchanged."""
    return re.sub(r"^(https?://)localhost(?=[:/]|$)", r"\g<1>127.0.0.1", url)


def lookup_order(order_id: str | None) -> dict | None:
    """Order facts for the lab's own annotations (00 never looks the order
    up — that's the point). Mock API first, the seed file as a fallback."""
    if not order_id:
        return None
    base = _mock_base()
    try:
        r = httpx.get(
            _loopback(f"{base}/orders/{order_id}"), timeout=LOCAL_TIMEOUT
        )  # an annotation must not hold up a trace
        if r.status_code == 200:
            o = r.json()
            return {
                "order_id": order_id,
                "total_usd": o.get("total_usd"),
                "source": f"GET {base}/orders/{order_id}",
                "items": [i.get("name") for i in o.get("items", [])],
            }
        if r.status_code == 404:
            return {
                "order_id": order_id,
                "total_usd": None,
                "source": f"GET {base}/orders/{order_id} → 404",
                "items": [],
            }
    except httpx.HTTPError:
        pass
    seed = json.loads(ORDERS_SEED.read_text(encoding="utf-8"))["orders"]
    o = next((x for x in seed if x["order_id"] == order_id), None)
    return {
        "order_id": order_id,
        "total_usd": o.get("total_usd") if o else None,
        "source": "shared/mock_order_api/orders_seed.json (mock API not running)",
        "items": [i["name"] for i in o["items"]] if o else [],
    }


def _as_amount(value: Any) -> float | None:
    """00's refund_amount is whatever the model wrote: 24.99, "24.99",
    "$249.99" or "1,200". Pull the number out. A bool is not an amount,
    although Python counts it as an int."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d[\d,]*\.?\d*", str(value))
    return float(match.group(0).replace(",", "")) if match else None


# -------------------------------------------------------------------- commands
# The commands panel: the PowerShell a student would type to do the same run
# without the lab. Quoting is the hard part: ps_quote() for single-quoted
# literals, _here() for here-strings when the ticket has double quotes, which
# Windows PowerShell 5.1 drops from the arguments of a native command.


def ps_quote(text: str) -> str:
    """PowerShell single-quoted literal. PowerShell treats curly single quotes
    as quote characters too, so all of them are doubled."""
    for q in "'‘’‚‛":
        text = text.replace(q, q + q)
    return "'" + text + "'"


def _here(text: str) -> str:
    """PowerShell single-quoted here-string: the text is taken literally, quotes and all."""
    return "@'\n" + text + "\n'@"


def _service_start_cmd(cp: str, runtime: str | None = None) -> str:
    """The terminal commands that start a 02–04 service: cd, the one-time setup, uvicorn."""
    c = CHECKPOINTS[cp]
    start = f"cd {c['dir'].replace('/', chr(92))}\n"
    if c.get("setup"):
        start += f"{c['setup']}   # once: builds the policy index\n"
    if cp == "02" and runtime:
        start += f'$env:DESKHAND_RUNTIME = "{runtime}"\n'
    start += f"py -m uvicorn app:app --port {c['port']}"
    return start


def commands(
    checkpoint: str,
    t: dict,
    request_id: str | None = None,
    repeat: int | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """Copyable PowerShell for this run, as [{label, cmd, note?}].

    repeat wraps the run in a 1..N loop (the Repeat view). request_id adds a
    search of the log for this run's lines. base_url points the service
    commands at the port the service really answered on."""
    cp = CHECKPOINTS[checkpoint]
    cd = "cd " + cp["dir"].replace("/", "\\")
    out: list[dict] = []
    loop = (
        (lambda cmd: f"1..{repeat} | ForEach-Object {{ {cmd} }}")
        if repeat and repeat > 1
        else (lambda cmd: cmd)
    )
    if checkpoint == "00":
        if '"' in t["text"]:
            # Pass the ticket through an env var and runpy, so no quote has to survive argv.
            run = (
                "$env:TICKET = "
                + _here(t["text"])
                + "\n"
                + loop(
                    "py -c \"import os, sys, runpy; sys.argv = ['bot.py', os.environ['TICKET']]; "
                    "runpy.run_path('bot.py', run_name='__main__')\""
                )
            )
            note = "Same as py bot.py '<ticket>' — the ticket has double quotes, which Windows PowerShell 5.1 would drop from a command-line argument."
        else:
            run = loop("py bot.py " + ps_quote(t["text"]))
            note = "bot.py joins its arguments into the ticket text. Run it from this folder: it globs ../../shared/policies relative to the cwd."
        out.append(
            {
                "label": "Run checkpoint 00 on this ticket",
                "cmd": f"{cd}\n{run}",
                "note": note,
            }
        )
        if t["source"] == "incidents.py":
            out.append(
                {"label": "All three Session 1 demos", "cmd": f"{cd}\npy incidents.py"}
            )
    elif checkpoint == "01":
        classify = "print(triage.classify_ticket(t, str(uuid.uuid4())))"
        if t["source"] == "dataset":
            run = loop(
                "py -c \"import json, uuid, triage; t = next(x for x in json.load(open('../../shared/data/tickets.json', "
                f"encoding='utf-8'))['tickets'] if x['ticket_id'] == '{t['key']}'); {classify}\""
            )
        else:
            run = (
                "$env:TICKET = "
                + _here(t["text"])
                + "\n"
                + loop(
                    "py -c \"import os, uuid, triage; t = {'ticket_id': '"
                    + t["ticket"]["ticket_id"]
                    + "', 'channel': 'lab', 'order_id': None, 'messages': [{'from': 'customer', 'text': os.environ['TICKET']}]}; "
                    + classify
                    + '"'
                )
            )
        out.append(
            {
                "label": "Classify this ticket with checkpoint 01",
                "cmd": f"{cd}\n{run}",
                "note": "The lab ran main.main() with TICKETS_PATH pointed at a one-ticket file; this one-liner calls the same classify_ticket().",
            }
        )
        out.append(
            {
                "label": "The checkpoint's own CLI (all 35 tickets, ~1 cent)",
                "cmd": f"{cd}\npy main.py",
            }
        )
        if request_id:
            out.append(
                {
                    "label": "This run's log lines",
                    "cmd": f"{cd}\nSelect-String -Path logs\\runs.jsonl -SimpleMatch '{request_id}'",
                    "note": "Grepping the log is the lesson — the lab only shows the lines this run appended.",
                }
            )
        out.append(
            {
                "label": "Offline tests (no key, no cost)",
                "cmd": f"{cd}\npy -m pytest -q",
            }
        )
    else:
        base = base_url or f"http://localhost:{cp['port']}"
        if t["source"] == "dataset":
            body = json.dumps({"ticket_id": t["key"]})
            post = f"$r = Invoke-RestMethod -Method Post {base}/tickets -ContentType application/json -Body '{body}'"
        else:
            sender = (
                f"; customer_name = {ps_quote(t['customer_name'])}"
                if t.get("customer_name")
                else ""
            )
            post = (
                "$ticket = " + _here(t["text"]) + "\n"
                f"$r = Invoke-RestMethod -Method Post {base}/tickets -ContentType application/json "
                "-Body (@{ ticket_text = $ticket" + sender + " } | ConvertTo-Json)"
            )
        submit = (
            loop(post + "; $r.request_id")
            if repeat and repeat > 1
            else post + "\n$r.request_id"
        )
        out.append(
            {
                "label": f"Submit this ticket to the checkpoint {checkpoint} service",
                "cmd": submit,
                "note": "The lab does exactly this. The service answers with a request_id and works the ticket in the background.",
            }
        )
        out.append(
            {
                "label": "Watch the live event stream (SSE)",
                "cmd": f"curl.exe -N {base}/runs/$($r.request_id)/events",
                "note": "Raw server-sent events: node.started, node.completed, reply.delta, run.completed. Reconnect with a Last-Event-ID header to replay.",
            }
        )
        out.append(
            {
                "label": "Approve or reject a paused run",
                "cmd": f"Invoke-RestMethod -Method Post {base}/runs/$($r.request_id)/approve -ContentType application/json -Body '{{\"approved\": true}}'",
                "note": "Only valid while the run is awaiting_approval; the service answers 409 otherwise.",
            }
        )
        out.append(
            {
                "label": "The persisted state (machine view)",
                "cmd": f"Invoke-RestMethod {base}/runs/$($r.request_id) | ConvertTo-Json -Depth 8",
            }
        )
        if request_id:
            out.append(
                {
                    "label": "This run's log lines",
                    "cmd": f"{cd}\nSelect-String -Path logs\\runs.jsonl -SimpleMatch '{request_id}'",
                }
            )
        out.append(
            {
                "label": "Offline tests (no key, no cost)",
                "cmd": f"{cd}\npy -m pytest -q",
            }
        )
        if checkpoint == "04":
            out.append(
                {
                    "label": "The dashboard, built from logs/runs.jsonl",
                    "cmd": f"{cd}\npy dashboard.py",
                }
            )
    return out


# ------------------------------------------------------------------ enrichment
# A trace from a runner or from _watch() is raw observation. enrich() adds what
# the lab knows on top: code links, costs priced with checkpoint 01's table,
# order facts (00 never looks the order up), the commands, and the summary.


def _summary(trace: dict) -> dict:
    """One flat row per trace with the same keys for every checkpoint, so the
    headline, Compare and Repeat can line 00 up against 04 field by field.
    A field a checkpoint does not have is None or False."""
    cp = trace["checkpoint"]
    if cp == "00":
        usage = [a.get("usage") or {} for a in trace.get("attempts", [])]
        plan = trace.get("plan") if isinstance(trace.get("plan"), dict) else {}
        return {
            "outcome": trace["outcome"],
            "ok": trace["outcome"] == "parsed",
            "category": plan.get("category"),
            "refund_amount": trace["refund"].get("amount"),
            "refund_fired": trace["refund"].get("fired"),
            "customer_text": trace.get("stdout"),
            "customer_reply": plan.get("customer_reply"),
            "attempts": len(trace.get("attempts", [])),
            "tokens_in": sum(u.get("prompt_tokens") or 0 for u in usage),
            "tokens_out": sum(u.get("completion_tokens") or 0 for u in usage),
            "cost_usd": trace.get("cost_usd"),
            "latency_ms": trace.get("duration_ms"),
            "request_id": None,
            "validated": False,
        }
    if cp == "01":
        calls = [a.get("provider_call") or {} for a in trace.get("attempts", [])]
        result = trace.get("result") or {}
        return {
            "outcome": trace["outcome"],
            "ok": trace["outcome"] == "validated",
            "category": result.get("category"),
            "priority": result.get("priority"),
            "sentiment": result.get("sentiment"),
            "confidence": result.get("confidence"),
            "refund_amount": None,
            "refund_fired": False,
            "customer_text": trace.get("user_message"),
            "attempts": len(trace.get("attempts", [])),
            "tokens_in": sum(
                (c.get("usage") or {}).get("prompt_tokens") or 0 for c in calls
            ),
            "tokens_out": sum(
                (c.get("usage") or {}).get("completion_tokens") or 0 for c in calls
            ),
            "cost_usd": trace.get("billed_cost_usd"),
            "reported_cost_usd": trace.get("reported_cost_usd"),
            "latency_ms": trace.get("duration_ms"),
            "request_id": trace.get("request_id"),
            "validated": trace["outcome"] == "validated",
            "error_code": (trace.get("error") or {}).get("code"),
        }
    state = trace.get("state") or {}
    plan = state.get("resolution_plan") or {}
    triage = state.get("triage") or {}
    facts = trace.get("facts") or {}
    refunds = facts.get("refunds") or []
    return {
        "outcome": trace["outcome"],
        "ok": trace["outcome"] == "completed",
        "category": plan.get("category") or triage.get("category"),
        "priority": triage.get("priority"),
        "sentiment": triage.get("sentiment"),
        "confidence": plan.get("confidence", triage.get("confidence")),
        "actions": [a.get("type") for a in plan.get("actions", [])],
        "refund_amount": plan.get("refund_amount_usd"),
        "refund_fired": bool(refunds),
        "refund_ids": [r.get("refund_id") for r in refunds],
        "order_total": (state.get("order_data") or {}).get("total_usd"),
        "order_found": state.get("order_data") is not None,
        "requires_approval": state.get("requires_approval"),
        "approval_status": state.get("approval_status"),
        "validated": bool(plan) and not state.get("validation_errors"),
        "repairs": state.get("repair_attempts", 0),
        "citations": plan.get("citations") or [],
        "policy_sections": len(state.get("policy_chunks") or []),
        "guard_input_tripped": facts.get("guard_input") is not None,
        "guard_output_tripped": facts.get("guard_output") is not None,
        "customer_text": state.get("customer_reply_draft")
        or state.get("user_visible_error"),
        "attempts": facts.get("llm_calls"),
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": state.get("cost_usd"),
        "latency_ms": trace.get("duration_ms"),
        "request_id": trace.get("request_id"),
        "error_code": state.get("internal_error_code"),
        "nodes_run": len(trace.get("nodes") or []),
    }


def enrich(trace: dict, t: dict) -> dict:
    """Add the lab's annotations to a raw trace, in place, and return it."""
    cp = trace["checkpoint"]
    trace["cites"] = citations(cp)
    if cp == "00":
        model = trace.get("model") or ""
        prices = price_quote([model]).get(model) if model else None
        for a in trace.get("attempts", []):
            a["cost_usd"] = _cost(prices, a.get("usage"))
            if a.get("exception"):
                rel = _repo_rel(a["exception"].get("file"))
                a["exception"]["cite"] = (
                    cite_line(rel, a["exception"].get("line")) if rel else None
                )
        costs = [
            a["cost_usd"]
            for a in trace.get("attempts", [])
            if a.get("cost_usd") is not None
        ]
        # No attempts means no call was made ($0); attempts without usage are unknown (None).
        trace["cost_usd"] = (
            sum(costs) if costs else (0.0 if not trace.get("attempts") else None)
        )
        trace["pricing"] = {
            "model": model,
            "per_million": prices,
            "source": cite("checkpoints/01-triage/llm.py", "PRICE_TABLE: dict"),
            "note": "v1 has no price table; priced with checkpoint 01's llm._cost_usd",
        }
        if trace["cost_usd"] is not None:
            trace["projection"] = {
                "tickets_per_month": TICKETS_PER_MONTH,
                "usd_per_month": trace["cost_usd"] * TICKETS_PER_MONTH,
            }
        order = lookup_order(t.get("order_id"))
        if order:
            order["order_id_from_text"] = bool(t.get("order_id_from_text"))
        amount = _as_amount(trace["refund"].get("amount"))
        trace["refund"]["amount_parsed"] = amount
        trace["refund"]["order"] = order
        total = (order or {}).get("total_usd")
        trace["refund"]["exceeds_order_total"] = (
            amount is not None
            and total is not None
            and amount > total + 1e-9  # float slack: the exact total is not "over"
        )
        if trace.get("resolve_line"):
            trace["cites"]["resolve_def"] = cite_line(
                _repo_rel(trace.get("bot_file")) or ANCHORS["00"][0][1],
                trace["resolve_line"],
            )
    elif cp == "01":
        trace["log_file"] = _repo_rel(trace.get("log_path"))
    trace["commands"] = commands(
        cp, t, trace.get("request_id"), base_url=trace.get("base_url")
    )
    trace["summary"] = _summary(trace)
    return trace


# ------------------------------------------------------------------ run store
# In memory only: a lab restart forgets every trace. The 02–04 services keep
# their runs in SQLite, which is what Follow reads back.

RUNS: "OrderedDict[str, dict]" = (
    OrderedDict()
)  # lab run_id -> enriched trace, for GET /api/runs/{run_id}
# (checkpoint, ticket key) and (checkpoint, "*") -> the last observed cost.
# /api/estimate prefers these to its rough formulas.
OBSERVED_COST: dict[tuple[str, str], float] = {}
MAX_RUNS_KEPT = 300  # oldest dropped first
LIVE: dict[
    str, dict
] = {}  # lab run_id -> {checkpoint, base, request_id} while a 02–04 run is open
# LIVE is how POST /api/runs/{run_id}/approve finds the service and request_id
# behind a lab run_id. /api/model refuses to switch while it is not empty.


def _keep(run_id: str, trace: dict) -> None:
    RUNS[run_id] = trace
    while len(RUNS) > MAX_RUNS_KEPT:
        RUNS.popitem(last=False)


# ------------------------------------------------------- managed services
# The mock order API and the 02–04 services, started by the lab as child
# uvicorn processes on 127.0.0.1. A student can also start one in a terminal.
# Either way the lab finds a service by GET /openapi.json and its FastAPI
# title ("Nimbus Mock Order API", "... checkpoint 02 ..."), never by port
# alone. A managed service's output goes to a 300-line buffer that the Setup
# panel reads. atexit kills the children, so stopping the lab stops them too.

MANAGED: dict[
    str, dict
] = {}  # "mock" | "02" | "03" | "04" -> {proc, port, log, runtime, started}
_managed_lock = (
    threading.Lock()
)  # one start or stop at a time; the readiness wait runs outside it


def _service_spec(name: str) -> dict:
    """dir, port and expected title of a service the lab can manage; 404 for
    any other name. The mock's port comes from MOCK_ORDER_API_URL."""
    if name == "mock":
        port = (
            int(_mock_base().rsplit(":", 1)[-1])
            if re.search(r":\d+$", _mock_base())
            else 9000
        )
        return {"dir": MOCK_DIR, "port": port, "title": "Nimbus Mock Order API"}
    cp = CHECKPOINTS.get(name)
    if cp is None or cp["kind"] != "http":
        raise HTTPException(404, f"{name} is not a service")
    return {
        "dir": REPO / cp["dir"],
        "port": cp["port"],
        "title": f"checkpoint {name}",
        "cp": cp,
    }


def _probe_sync(url: str, timeout: float = 0.8) -> dict | None:
    """GET a JSON document; None if nothing answers or the answer is not 200 JSON."""
    try:
        r = httpx.get(
            _loopback(url),
            timeout=httpx.Timeout(timeout, connect=min(timeout, LOCAL_TIMEOUT.connect)),
        )
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _reap(name: str) -> None:
    """Record the exit code of a managed process that has died (a crash run ends this way)."""
    info = MANAGED.get(name)
    if info and info["proc"] is not None and info["proc"].poll() is not None:
        info["exit_code"] = info["proc"].returncode


def _pump_output(name: str, proc: subprocess.Popen, log: deque) -> None:
    """Copy a service's output into its log, redacted, until it exits. One
    thread per service, so the pipe never fills and stalls the service."""
    for raw in proc.stdout:
        log.append(redact(raw.decode("utf-8", errors="replace").rstrip()))


# ------------------------------------------------------- start-time flags
# The switches the checkpoints and the mock API read from their environment.
# The lab passes a chosen flag ONLY as the environment of the one process it
# starts (never .env, never its own environment). Each flag is offered only
# where the code really reads it: the variable name is looked up in the
# checkpoint's source, so the list follows the checkpoints as they change.
# Anything else is rejected with 422.

SERVICE_FLAGS: dict[str, dict[str, str]] = {
    # request field -> the variable, the file that reads it, what the badge says
    "crash_after_refund": {
        "var": "DESKHAND_CRASH_AFTER_REFUND",
        "file": "nodes.py",
        "label": "crash after refund",
    },
    "kill_switch": {
        "var": "DESKHAND_KILL_SWITCH",
        "file": "graph_lg.py",
        "label": "kill switch on",
    },
    "budget_usd": {
        "var": "DESKHAND_BUDGET_USD_PER_RUN",
        "file": "budget.py",
        "label": "budget",
    },
}
BUDGET_RANGE = (
    0.0,
    5.0,
)  # exclusive low, inclusive high: a typo can't lift the cap to $500
_KILL_ON = {"1", "true", "on", "yes"}  # graph_lg.kill_switch_engaged()'s own set


def chaos_modes() -> list[str]:
    """The mock API's CHAOS_MODES, read from its source (never imported)."""
    try:
        for line in (MOCK_DIR / "app.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("CHAOS_MODES"):
                modes = ast.literal_eval(line.split("=", 1)[1].strip())
                return ["none"] + sorted(m for m in modes if m != "none")
    except (OSError, ValueError, SyntaxError):
        pass
    return ["none", "error", "flaky", "inconsistent", "timeout"]


def service_flags(name: str) -> list[str]:
    """The flag fields this checkpoint's code actually reads."""
    cp = CHECKPOINTS.get(name)
    if cp is None or cp["kind"] != "http":
        return []
    out = []
    for field, f in SERVICE_FLAGS.items():
        try:
            if any(
                f["var"] in line for line in _source_lines(f"{cp['dir']}/{f['file']}")
            ):
                out.append(field)
        except OSError:
            continue
    return out


class StartRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid"
    )  # an unknown flag is a 422, not silently dropped
    runtime: str | None = None  # 02 only: "plain" | "langgraph"
    chaos: str | None = None  # mock only: one of chaos_modes()
    crash_after_refund: bool = False  # 02–04: DESKHAND_CRASH_AFTER_REFUND=1
    kill_switch: bool = False  # 04: DESKHAND_KILL_SWITCH=1
    budget_usd: float | None = None  # 04: DESKHAND_BUDGET_USD_PER_RUN


def flag_env(name: str, body: StartRequest) -> dict[str, str]:
    """Whitelist a start request -> the env vars to set on that one process."""
    out: dict[str, str] = {}
    if body.runtime is not None and (
        name != "02" or body.runtime not in ("plain", "langgraph")
    ):
        raise HTTPException(
            422, "runtime is for checkpoint 02 only: plain or langgraph"
        )
    if name == "mock":
        if body.crash_after_refund or body.kill_switch or body.budget_usd is not None:
            raise HTTPException(422, "the mock order API only takes chaos")
        if body.chaos is not None:
            if body.chaos not in chaos_modes():
                raise HTTPException(
                    422, "chaos must be one of " + ", ".join(chaos_modes())
                )
            if body.chaos != "none":
                out["MOCK_CHAOS"] = body.chaos
        return out
    if body.chaos is not None:
        raise HTTPException(422, "chaos is a mock order API flag")
    allowed = service_flags(name)
    if body.crash_after_refund:
        if "crash_after_refund" not in allowed:
            raise HTTPException(
                422, f"checkpoint {name} does not read DESKHAND_CRASH_AFTER_REFUND"
            )
        out["DESKHAND_CRASH_AFTER_REFUND"] = "1"
    if body.kill_switch:
        if "kill_switch" not in allowed:
            raise HTTPException(
                422, f"checkpoint {name} does not read DESKHAND_KILL_SWITCH"
            )
        out["DESKHAND_KILL_SWITCH"] = "1"
    if body.budget_usd is not None:
        if "budget_usd" not in allowed:
            raise HTTPException(
                422, f"checkpoint {name} does not read DESKHAND_BUDGET_USD_PER_RUN"
            )
        lo, hi = BUDGET_RANGE
        if not (math.isfinite(body.budget_usd) and lo < body.budget_usd <= hi):
            raise HTTPException(422, f"budget_usd must be above {lo} and at most {hi}")
        out["DESKHAND_BUDGET_USD_PER_RUN"] = repr(
            round(body.budget_usd, 6)
        )  # a plain literal budget.py's float() reads back
    return out


def effective_flags(
    name: str, env: dict[str, str], lab_set: dict[str, str]
) -> list[dict]:
    """What the started process will really see, for the badge: the lab's
    choice, else a variable inherited from the lab's own shell, else .env
    (the checkpoints load it; the mock API does not)."""
    if name == "mock":
        names = [("MOCK_CHAOS", "chaos")]
    else:
        names = [(SERVICE_FLAGS[f]["var"], f) for f in service_flags(name)]
    dotenv = {} if name == "mock" else _env()
    out = []
    for var, field in names:
        if var in lab_set:
            value, source = lab_set[var], "lab"
        elif env.get(var):
            value, source = env[var], "shell"
        elif dotenv.get(var):
            value, source = dotenv[var], ".env"
        else:
            continue
        if field == "chaos":
            if value == "none":
                continue
            label = "chaos: " + value
        elif field == "kill_switch":
            if value.strip().lower() not in _KILL_ON:
                continue
            label = SERVICE_FLAGS[field]["label"]
        elif field == "budget_usd":
            label = f"budget ${value}/run"
        else:  # crash: any non-empty value trips `if os.getenv(...)`
            label = SERVICE_FLAGS[field]["label"]
        out.append(
            {
                "field": field,
                "var": var,
                "value": value,
                "source": source,
                "label": label,
            }
        )
    return out


def start_service(
    name: str, runtime: str | None = None, flags: dict[str, str] | None = None
) -> dict:
    """Start one service as a child process and wait until it answers.

    A service already up (started by the lab, or in a terminal on that port)
    is reported as already_running and gets no flags. The child's environment
    is the lab's, plus the model picker's overrides, 02's runtime and the
    whitelisted flags; .env is never touched. 03/04 without qdrant_data/ run
    ingest.py first. Blocking: the endpoint calls it with asyncio.to_thread."""
    spec = _service_spec(name)
    flags = dict(flags or {})
    with _managed_lock:
        _reap(name)
        info = MANAGED.get(name)
        if info and info["proc"] and info["proc"].poll() is None:
            return {
                "status": "already_running",
                "port": spec["port"],
                "pid": info["proc"].pid,
                "note": "already running"
                + (
                    " — the flags were NOT applied: Stop it, then Start with them"
                    if flags
                    else ""
                ),
            }
        # Something else (a terminal the student started) already serves this port.
        doc = _probe_sync(f"http://127.0.0.1:{spec['port']}/openapi.json")
        if doc:
            title = (doc.get("info") or {}).get("title", "")
            return {
                "status": "already_running",
                "port": spec["port"],
                "pid": None,
                "note": f"port {spec['port']} already serves “{title}” (started outside the lab)"
                + (" — the flags were NOT applied" if flags else ""),
            }

        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        if name != "mock":
            env.update(model_overrides(name))
        if name == "02":
            # Set explicitly, so the lab knows which runtime it started: the request, shell, .env, else plain.
            env["DESKHAND_RUNTIME"] = (
                runtime
                or env.get("DESKHAND_RUNTIME")
                or _env().get("DESKHAND_RUNTIME")
                or "plain"
            )
        env.update(flags)
        shown = effective_flags(name, env, flags)
        log: deque = deque(maxlen=300)
        for f in shown:
            log.append(
                f"[lab] started with {f['var']}={f['value']} (from {'the Setup panel' if f['source'] == 'lab' else f['source']})"
            )

        if name in ("03", "04") and not (spec["dir"] / "qdrant_data").exists():
            log.append(
                "[lab] no qdrant_data/ yet — running `py ingest.py` first (embeds the 6 policy docs once)"
            )
            ingest = subprocess.run(
                [sys.executable, "ingest.py"],
                cwd=spec["dir"],
                env=env,
                capture_output=True,
                text=True,
                timeout=INGEST_TIMEOUT_S,
            )
            for line in (ingest.stdout + ingest.stderr).splitlines()[-40:]:
                log.append(redact(line))
            if ingest.returncode != 0:
                MANAGED[name] = {
                    "proc": None,
                    "port": spec["port"],
                    "log": log,
                    "runtime": None,
                    "model": None,
                    "started": time.time(),
                    "exit_code": ingest.returncode,
                    "flags": shown,
                    "flag_env": flags,
                }
                raise HTTPException(500, "ingest.py failed — see the service log")

        proc = subprocess.Popen(
            # Local only, like the lab: a service spends API money too.
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--port",
                str(spec["port"]),
                "--host",
                "127.0.0.1",
            ],
            cwd=spec["dir"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        threading.Thread(
            target=_pump_output, args=(name, proc, log), daemon=True
        ).start()
        MANAGED[name] = {
            "proc": proc,
            "port": spec["port"],
            "log": log,
            "runtime": env.get("DESKHAND_RUNTIME") if name == "02" else None,
            "model": current_choice() if name != "mock" else None,
            "started": time.time(),
            "exit_code": None,
            # flags: what the badge shows; flag_env: what to pass again on a model restart
            "flags": shown,
            "flag_env": flags,
        }

    # Ready means /openapi.json answers: the app imported and uvicorn is serving.
    deadline = time.monotonic() + SERVICE_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise HTTPException(
                500,
                f"the service exited with code {proc.returncode} — see the service log",
            )
        if _probe_sync(f"http://127.0.0.1:{spec['port']}/openapi.json", timeout=0.5):
            return {
                "status": "started",
                "port": spec["port"],
                "pid": proc.pid,
                "runtime": MANAGED[name]["runtime"],
                "flags": shown,
            }
        time.sleep(0.4)
    raise HTTPException(
        504, "the service did not answer within the start timeout — see the service log"
    )


def _listening_pid(port: int) -> int | None:
    """The process listening on 127.0.0.1:<port> (or all interfaces), if any."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            # Columns: Proto, Local Address, Foreign Address, State, PID.
            for line in out.splitlines():
                parts = line.split()
                if (
                    len(parts) == 5
                    and parts[3] == "LISTENING"
                    and parts[1].rsplit(":", 1)[-1] == str(port)
                    and parts[1].rsplit(":", 1)[0]
                    in ("127.0.0.1", "0.0.0.0", "[::]", "[::1]")
                ):
                    return int(parts[4])
            return None
        out = subprocess.run(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        return int(out.split()[0]) if out.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _stop_external(name: str, spec: dict) -> dict:
    """Stop a service started outside the lab (a terminal). Only when the port
    really serves the expected app, judged by its FastAPI title."""
    doc = _probe_sync(f"http://127.0.0.1:{spec['port']}/openapi.json")
    title = ((doc or {}).get("info") or {}).get("title", "")
    expected = "Nimbus Mock Order API" if name == "mock" else f"checkpoint {name}"
    if not doc:
        return {"status": "not_running"}
    if expected not in title:
        return {
            "status": "not_managed",
            "note": f"port {spec['port']} serves “{title}”, not {spec['title']}; stop it in its own terminal",
        }
    pid = _listening_pid(spec["port"])
    if pid is None:
        return {
            "status": "not_managed",
            "note": "could not find the process on port "
            + str(spec["port"])
            + "; stop it in its own terminal",
        }
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError as exc:
        return {
            "status": "not_managed",
            "note": f"could not stop pid {pid}: {exc}; stop it in its own terminal",
        }
    return {
        "status": "stopped",
        "pid": pid,
        "note": f"stopped {spec['title']} (pid {pid}), which was started outside the lab",
    }


def stop_service(name: str) -> dict:
    """Stop a service: terminate, then kill after 8s. One the lab did not
    start goes to _stop_external()."""
    spec = _service_spec(name)
    with _managed_lock:
        info = MANAGED.get(name)
        if not info or not info.get("proc") or info["proc"].poll() is not None:
            return _stop_external(name, spec)
        proc = info["proc"]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        info["exit_code"] = proc.returncode
        return {"status": "stopped", "exit_code": proc.returncode}


def _stop_all_managed() -> None:
    """Kill every child the lab started, so none keeps a port or the Qdrant lock."""
    for name, info in list(MANAGED.items()):
        proc = info.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


atexit.register(_stop_all_managed)


@app.post("/api/services/{name}/start")
async def api_start_service(name: str, body: StartRequest | None = None) -> dict:
    # Validate everything first, then start in a worker thread: the start
    # blocks for seconds, and the event loop keeps serving other runs meanwhile.
    _service_spec(name)
    body = body or StartRequest()
    flags = flag_env(name, body)  # 422 on anything not whitelisted for this service
    return await asyncio.to_thread(start_service, name, body.runtime, flags)


@app.get("/api/services/flags")
def api_service_flags() -> dict:
    """Which start-time flags each service offers (read from the code)."""
    return {
        "mock": {"chaos": chaos_modes()},
        **{cp: {"flags": service_flags(cp)} for cp in ("02", "03", "04")},
        "budget_range": BUDGET_RANGE,
    }


@app.post("/api/services/{name}/stop")
async def api_stop_service(name: str) -> dict:
    _service_spec(name)
    return await asyncio.to_thread(stop_service, name)


@app.get("/api/services/{name}/log")
def api_service_log(name: str) -> dict:
    """The Setup panel's log view. A service started in a terminal has none here."""
    _service_spec(name)
    info = MANAGED.get(name)
    if not info:
        return {"managed": False, "lines": []}
    _reap(name)
    return {
        "managed": True,
        "lines": list(info["log"]),
        "exit_code": info.get("exit_code"),
        "runtime": info.get("runtime"),
        "pid": info["proc"].pid if info.get("proc") else None,
    }


class ModelRequest(BaseModel):
    choice: str  # "gpt" | "haiku" | "sonnet"


@app.post("/api/model")
async def set_model(body: ModelRequest) -> dict:
    """Switch the lab's model. Runners pick it up on the next run; services the
    lab started are restarted on it (a service only reads .env at import)."""
    if body.choice not in MODEL_OPTIONS:
        raise HTTPException(422, "choice must be one of " + ", ".join(MODEL_OPTIONS))
    if LIVE:
        raise HTTPException(
            409,
            "a run is still open (maybe paused at the approval gate) — finish it first",
        )
    MODEL_CHOICE["id"] = body.choice
    restarted, failed = [], {}
    for name in ("02", "03", "04"):
        info = MANAGED.get(name) or {}
        proc = info.get("proc")
        if not (proc and proc.poll() is None) or info.get("model") == body.choice:
            continue
        runtime = info.get("runtime")
        try:
            await asyncio.to_thread(stop_service, name)
            # same runtime and the same flags: a model switch must not quietly drop a crash switch
            await asyncio.to_thread(start_service, name, runtime, info.get("flag_env"))
            restarted.append(name)
        except HTTPException as e:
            failed[name] = e.detail
    return {"choice": body.choice, "restarted": restarted, "failed": failed}


# ------------------------------------------------------- service runs (02–04)
# A 02–04 run is plain HTTP against the checkpoint's own API, the same calls
# the commands panel prints:
#   POST /tickets             -> request_id; the service works in the background
#   GET  /runs/{id}/events    -> SSE; the service stores each event before sending it
#   POST /runs/{id}/approve   -> answers the approval gate
#   GET  /runs/{id}           -> the persisted state, read once at the end
# The lab adds no logic to the run. After the terminal event it reads the
# lines this run appended to logs/runs.jsonl (from a byte offset taken before
# POST /tickets) and pulls facts out of them for the panels.


def _service_base(checkpoint: str) -> str | None:
    """Where this checkpoint's service answers right now: the managed process,
    else any probed port whose FastAPI title names the checkpoint."""
    cp = CHECKPOINTS[checkpoint]
    candidates = [cp["port"]] + [p for p in SERVICE_PORTS if p != cp["port"]]
    for port in candidates:
        doc = _probe_sync(f"http://127.0.0.1:{port}/openapi.json", timeout=0.6)
        title = ((doc or {}).get("info") or {}).get("title") or ""
        if f"checkpoint {checkpoint}" in title:
            return f"http://127.0.0.1:{port}"
    return None


def _log_path(checkpoint: str) -> Path:
    return REPO / CHECKPOINTS[checkpoint]["dir"] / "logs" / "runs.jsonl"


def _read_log_since(path: Path, offset: int, request_id: str) -> list[dict]:
    """The JSON log lines after byte `offset` that carry this request_id.
    Other runs (a terminal, another tab) append to the same file."""
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh.read().decode("utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("request_id") == request_id:
                out.append(rec)
    return out


def _facts_from_log(log_lines: list[dict], events: list[dict]) -> dict:
    """What the log and the events say happened, pulled out for the panels."""
    facts: dict[str, Any] = {
        "refunds": [],
        "plan_invalid": [],
        "node_errors": [],
        "order": None,
        "policy": None,
        "guard_input": None,
        "guard_output": None,
        "triage_repairs": 0,
        "resumed": False,
    }
    for rec in log_lines:
        ev = rec.get("event")
        if ev == "refund.issued":
            facts["refunds"].append(
                {k: rec.get(k) for k in ("refund_id", "amount_usd", "idempotency_key")}
            )
        elif ev == "plan.invalid":
            facts["plan_invalid"].append(
                {
                    "violations": rec.get("violations") or [],
                    "repair_attempts": rec.get("repair_attempts"),
                }
            )
        elif ev == "node.error":
            facts["node_errors"].append(
                {k: rec.get(k) for k in ("node", "internal_error_code", "detail")}
            )
        elif ev in ("order.fetched", "order.not_found", "order.no_reference"):
            facts["order"] = {"event": ev, "order_id": rec.get("order_id")}
        elif ev == "policy.retrieved":
            facts["policy"] = {
                "sections": rec.get("sections") or [],
                "top_score": rec.get("top_score"),
            }
        elif ev == "guardrail.input_tripped":
            facts["guard_input"] = {
                "reason": rec.get("reason"),
                "code": rec.get("internal_error_code"),
                "guard": rec.get("guard"),
                "source": rec.get("source", "ticket"),
            }
        elif ev == "guardrail.output_tripped":
            facts["guard_output"] = {"violations": rec.get("violations") or []}
        elif ev == "triage.repair":
            facts["triage_repairs"] += 1
        elif ev == "run.resumed":
            facts["resumed"] = True
    # LLM calls: one per triage attempt, one per plan attempt, one reply draft,
    # plus the injection classifier when it ran. Derived from node names.
    nodes_run = [
        e["data"].get("node") for e in events if e.get("event") == "node.completed"
    ]
    calls = 0
    for n in nodes_run:
        if n in (
            "triage",
            "generate_resolution_plan",
            "repair_plan",
            "draft_customer_reply",
        ):
            calls += 1
    calls += facts["triage_repairs"]
    if facts["guard_input"] is not None:
        calls += 1
    facts["llm_calls"] = calls
    return facts


def _node_timeline(events: list[dict], order: list[str]) -> list[dict]:
    """One row per node.started, closed by its node.completed."""
    rows: list[dict] = []
    open_by_name: dict[str, dict] = {}
    for e in events:
        name, data = e.get("event"), e.get("data") or {}
        if name == "node.started":
            row = {
                "node": data.get("node"),
                "seq": e.get("seq"),
                "latency_ms": None,
                "cost_usd": None,
                "cost_delta_usd": None,
                "error": False,
            }
            rows.append(row)
            open_by_name[row["node"]] = row
        elif name == "node.completed":
            row = open_by_name.pop(data.get("node"), None)
            if row is None:
                row = {"node": data.get("node"), "seq": e.get("seq"), "error": False}
                rows.append(row)
            row.update(
                latency_ms=data.get("latency_ms"),
                cost_usd=data.get("cost_usd"),
                cost_delta_usd=data.get("cost_delta_usd"),
            )
    for row in rows:
        row["error"] = row["node"] == "handle_failure"
    # A node that raised routes to handle_failure; mark the one before it.
    for i, row in enumerate(rows):
        if row["node"] == "handle_failure" and i > 0:
            rows[i - 1]["raised"] = True
    return rows


async def _post_approval(base: str, request_id: str, approved: bool) -> dict:
    """Forward a decision to the service's own approve endpoint. The service
    refuses (409) a run that is not waiting at the gate."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{base}/runs/{request_id}/approve", json={"approved": approved}
        )
        if r.status_code != 200:
            raise HTTPException(
                r.status_code, f"the service refused the decision: {r.text[:300]}"
            )
        return r.json()


RECENT: "deque[dict]" = deque(
    maxlen=25
)  # request_ids this lab submitted, newest last (for Follow after an F5)
REQUEST_ID_RE = re.compile(
    r"^[A-Za-z0-9_-]{4,64}$"
)  # pasted text goes into a URL path: nothing else allowed


def _remember_request(checkpoint: str, request_id: str, label: str) -> None:
    """Put this request_id at the newest end of RECENT, once."""
    for item in list(RECENT):
        if item["request_id"] == request_id:
            RECENT.remove(item)
    RECENT.append(
        {
            "checkpoint": checkpoint,
            "request_id": request_id,
            "label": label,
            "at": time.time(),
        }
    )


async def _run_service(
    checkpoint: str,
    t: dict,
    run_id: str,
    approval: str,
    started: float,
    guard: str | None = None,
) -> AsyncIterator[dict]:
    """Submit one ticket to a 02–04 service, then watch it like any follower."""
    base = _service_base(checkpoint)
    if base is None:
        yield {
            "type": "error",
            "message": f"checkpoint {checkpoint} service is not running",
            "detail": "Start it from the Setup panel, or in a terminal:\n"
            + _service_start_cmd(checkpoint),
            "needs_service": checkpoint,
        }
        return
    log_path = _log_path(checkpoint)
    log_offset = (
        log_path.stat().st_size if log_path.exists() else 0
    )  # before the POST: only this run's lines come after
    if t["source"] == "dataset":
        body = {"ticket_id": t["key"]}
    else:
        body = {"ticket_text": t["text"]}
        if t.get("customer_name"):
            body["customer_name"] = t[
                "customer_name"
            ]  # optional field of TicketRequest: who it is from
    if checkpoint == "04" and guard:
        body["guard"] = guard  # 04 picks its input guard per run: heuristic | nemo

    # read=60s: an SSE stream can go quiet. A ReadTimeout in _watch() reconnects; it does not end the run.
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=60.0)) as client:
        try:
            r = await client.post(f"{base}/tickets", json=body)
        except httpx.HTTPError as exc:
            yield {
                "type": "error",
                "message": "POST /tickets failed",
                "detail": str(exc),
            }
            return
        if r.status_code != 200:
            yield {
                "type": "error",
                "message": f"POST /tickets → HTTP {r.status_code}",
                "detail": r.text[:1500],
            }
            return
        request_id = r.json()["request_id"]
        _remember_request(checkpoint, request_id, t["label"])
        LIVE[run_id] = {
            "checkpoint": checkpoint,
            "base": base,
            "request_id": request_id,
        }  # approve can reach it from here on
        yield {
            "type": "request_id",
            "request_id": request_id,
            "base_url": base,
            "submitted": {"url": f"{base}/tickets", "body": body},
        }
        async for event in _watch(
            client,
            checkpoint,
            base,
            request_id,
            run_id,
            approval,
            started,
            t,
            log_path,
            log_offset,
        ):
            yield event


async def _watch(
    client: httpx.AsyncClient,
    checkpoint: str,
    base: str,
    request_id: str,
    run_id: str,
    approval: str,
    started: float,
    t: dict,
    log_path: Path,
    log_offset: int,
    gate_decided: bool = False,
) -> AsyncIterator[dict]:
    """Stream /runs/{id}/events until a terminal event, then build the trace.

    The service persists every event before sending it, so this survives the
    service dying or restarting: a connection error starts a reconnect window
    (RECONNECT_WINDOW_S) instead of ending the run, and the reconnect sends
    Last-Event-ID so the service replays only what was missed.
    gate_decided: following a run whose approval gate was already answered,
    so a replayed approval.required is history, not a question."""
    events: list[dict] = []
    last_seq = 0
    terminal = False
    awaiting = False
    approval_record: dict = {"required": False, "decision": None, "by": None}
    # Moved to APPROVAL_TIMEOUT_S while parked at the gate, and pushed back when
    # the service returns. While the service is down, RECONNECT_WINDOW_S rules instead.
    deadline = time.monotonic() + RUN_TIMEOUT_S
    reconnects = 0
    down_since: float | None = (
        None  # monotonic time of the first failed connection, None while up
    )
    # One pass per connection: open the stream from last_seq, read events until
    # it ends. Leaves on a terminal event, a hard error, or a timeout.
    while not terminal:
        if down_since is None and time.monotonic() > deadline:
            yield {
                "type": "error",
                "message": "the run did not finish in time",
                "detail": f"no terminal event after {int(time.monotonic() - started)}s (request_id {request_id})",
            }
            break
        headers = {"Last-Event-ID": str(last_seq)} if last_seq else {}
        try:
            async with client.stream(
                "GET", f"{base}/runs/{request_id}/events", headers=headers
            ) as stream:
                # A service still starting up can answer 5xx: count it as still down.
                if stream.status_code >= 500 and down_since is not None:
                    raise httpx.TransportError(
                        f"HTTP {stream.status_code} while the service starts"
                    )
                if stream.status_code != 200:
                    detail = (await stream.aread()).decode("utf-8", errors="replace")[
                        :500
                    ]
                    yield {
                        "type": "error",
                        "message": f"GET /runs/{request_id}/events → HTTP {stream.status_code}",
                        "detail": detail
                        + (
                            "\nA service that restarted with an empty database no longer knows this run."
                            if stream.status_code == 404
                            else ""
                        ),
                    }
                    break
                if down_since is not None:
                    yield {
                        "type": "service.up",
                        "after_s": round(time.monotonic() - down_since, 1),
                        "last_seq": last_seq,
                        "text": f"the service is back after {time.monotonic() - down_since:.0f}s: reconnected with "
                        f"Last-Event-ID: {last_seq}, the service replays what was missed",
                    }
                    down_since = None
                    deadline = max(deadline, time.monotonic() + RUN_TIMEOUT_S)
                elif reconnects:
                    yield {
                        "type": "lab",
                        "text": f"reconnected with Last-Event-ID: {last_seq} — the service replays what was missed",
                    }
                # A minimal SSE parser. "field: value" lines fill `cur`; a blank
                # line dispatches it as one event; lines starting with ":" are
                # comments (keep-alive pings). `id` is the service's event seq,
                # which a reconnect sends back as Last-Event-ID.
                cur: dict[str, str] = {}
                async for line in stream.aiter_lines():
                    line = line.rstrip("\r")
                    if line == "":
                        if "event" in cur:
                            try:
                                data = json.loads(cur.get("data") or "{}")
                            except json.JSONDecodeError:
                                data = {"raw": cur.get("data")}
                            seq = (
                                int(cur["id"])
                                if cur.get("id", "").isdigit()
                                else last_seq + 1
                            )
                            last_seq = max(last_seq, seq)
                            ev = {"seq": seq, "event": cur["event"], "data": data}
                            events.append(ev)
                            yield {"type": "sse", **ev}
                            name = cur["event"]
                            # The approval policy: "ask" waits for Approve / Reject on
                            # the page (POST /api/runs/{run_id}/approve); "approve" and
                            # "reject" answer the gate at once from here.
                            if name == "approval.required":
                                approval_record["required"] = True
                                if gate_decided:
                                    pass  # a replay of a gate someone already answered
                                else:
                                    awaiting = True
                                    deadline = time.monotonic() + APPROVAL_TIMEOUT_S
                                    if approval in ("approve", "reject"):
                                        decided = approval == "approve"
                                        try:
                                            await _post_approval(
                                                base, request_id, decided
                                            )
                                            approval_record.update(
                                                decision="approved"
                                                if decided
                                                else "rejected",
                                                by="lab (auto)",
                                            )
                                            yield {
                                                "type": "approval.decided",
                                                "approved": decided,
                                                "by": "lab (auto)",
                                            }
                                        except HTTPException as exc:
                                            yield {
                                                "type": "lab",
                                                "text": f"auto-approval failed: {exc.detail}",
                                                "level": "bad",
                                            }
                                    else:
                                        yield {
                                            "type": "approval.waiting",
                                            "request_id": request_id,
                                            "timeout_s": APPROVAL_TIMEOUT_S,
                                        }
                            elif (
                                name == "run.status"
                                and awaiting
                                and data.get("status") != "awaiting_approval"
                            ):
                                # The gate was answered (here, on the page, or by a curl): back to the run deadline.
                                awaiting = False
                                deadline = time.monotonic() + RUN_TIMEOUT_S
                                if approval_record["decision"] is None:
                                    live = LIVE.get(run_id) or {}
                                    approval_record.update(
                                        decision=live.get("decision"),
                                        by=live.get("decided_by"),
                                    )
                            elif name in ("run.completed", "run.failed"):
                                terminal = True
                        cur = {}
                        continue
                    if line.startswith(":"):
                        continue
                    field, _, value = line.partition(":")
                    value = (
                        value[1:] if value.startswith(" ") else value
                    )  # SSE drops one leading space
                    if (
                        field == "data" and "data" in cur
                    ):  # multi-line data joins with "\n"
                        cur["data"] += "\n" + value
                    else:
                        cur[field] = value
                if not terminal:
                    # The service closed the stream without a terminal event (a graceful restart?).
                    reconnects += 1
                    await asyncio.sleep(0.5)
        except httpx.ReadTimeout:
            reconnects += 1  # nothing for 60s: reconnect and replay (a parked approval gate does this)
        except httpx.HTTPError as exc:
            # Refused, reset, cut mid-stream: the service died, was stopped, or is restarting.
            # The run is saved in its database, so keep knocking for a while.
            now = time.monotonic()
            if down_since is None:
                down_since = now
                yield {
                    "type": "service.down",
                    "request_id": request_id,
                    "last_seq": last_seq,
                    "retry_s": RECONNECT_WINDOW_S,
                    "detail": str(exc) or type(exc).__name__,
                    "checkpoint": checkpoint,
                }
            elif now - down_since > RECONNECT_WINDOW_S:
                yield {
                    "type": "error",
                    "message": f"the checkpoint {checkpoint} service did not come back within {RECONNECT_WINDOW_S}s",
                    "detail": f"The run is saved in the service's database as request {request_id}. Start the service again "
                    "(Setup), then paste that request id into “Follow a request ID” on the Run view: "
                    "the lab replays it from event 0 and carries on.",
                    "needs_service": checkpoint,
                    "request_id": request_id,
                }
                break
            await asyncio.sleep(1.0)
            continue
        if (
            reconnects > 40
        ):  # closed streams and read timeouts, not down time: that has its own window
            yield {
                "type": "error",
                "message": "gave up reconnecting to the event stream",
            }
            break

    if not terminal:
        LIVE.pop(run_id, None)
        return

    # The run is over. Collect the evidence: the persisted state, then the
    # log lines, then the facts the panels show.

    await asyncio.sleep(
        0.3
    )  # finalize's node.completed and its log line land just after run.completed
    try:
        r = await client.get(f"{base}/runs/{request_id}")
        state = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        state = {}
    # Drain any events that arrived after the terminal one (node.completed for the last node).
    try:
        r = await client.get(
            f"{base}/runs/{request_id}/events",
            headers={"Last-Event-ID": str(last_seq)},
            timeout=httpx.Timeout(2.0, read=1.5),
        )
    except httpx.HTTPError:
        pass
    live = LIVE.pop(run_id, {})
    if approval_record["required"] and approval_record["decision"] is None:
        approval_record.update(
            decision=state.get("approval_status"), by=live.get("decided_by")
        )

    log_lines = _read_log_since(log_path, log_offset, request_id)
    # Node events published after run.completed are in SQLite, not in our stream; the log has them.
    for rec in log_lines:
        if rec.get("event") == "node.completed" and not any(
            e["event"] == "node.completed" and e["data"].get("node") == rec.get("node")
            for e in events
        ):
            events.append(
                {
                    "seq": (events[-1]["seq"] + 1) if events else 1,
                    "event": "node.completed",
                    "data": {
                        k: rec.get(k)
                        for k in ("node", "latency_ms", "cost_usd", "cost_delta_usd")
                    },
                }
            )
    facts = _facts_from_log(log_lines, events)
    managed = MANAGED.get(checkpoint) or {}
    managed_up = bool(managed.get("proc") and managed["proc"].poll() is None)
    trace = {
        "type": "trace",
        "checkpoint": checkpoint,
        "request_id": request_id,
        "base_url": base,
        "runtime": managed.get("runtime") if managed_up else None,
        # start-time flags of the service as it is now (a crash run ends on the restarted, plain one)
        "service_flags": (managed.get("flags") or []) if managed_up else None,
        "state": state,
        "events": events,
        "nodes": _node_timeline(events, node_order(checkpoint)),
        "node_order": node_order(checkpoint),
        "approval": approval_record,
        "approval_policy": approval,
        "log_lines": log_lines,
        "log_file": _repo_rel(str(log_path)),
        "facts": facts,
        "outcome": state.get("status")
        or (
            "completed"
            if events and events[-1]["event"] == "run.completed"
            else "failed"
        ),
        "duration_ms": int((time.time() - started) * 1000),
        "ticket": t["ticket"],
        "ticket_text": t["text"],
        "order_lab": lookup_order(t.get("order_id"))
        if checkpoint and t.get("order_id")
        else None,
    }
    yield trace


def _ticket_from_state(state: dict, request_id: str) -> dict:
    """A followed run's ticket, rebuilt from its persisted state."""
    tid = state.get("ticket_id") or ""
    t = None
    # Free-text runs get "adhoc-<request_id>" from the service; only a dataset id resolves.
    if tid and not tid.startswith("adhoc-"):
        try:
            t = resolve_ticket(TicketRef(ticket_id=tid))
        except HTTPException:
            t = None
    if t is None:
        text = state.get("ticket_text") or ""
        name = state.get("customer_name")
        match = re.search(r"\bO-\d{4,6}\b", text)
        t = {
            "key": tid or f"request:{request_id}",
            "label": "free text" + (f" from {name}" if name else ""),
            "text": text,
            "customer_name": name,
            "source": "custom",
            "ticket": {
                "ticket_id": tid or request_id,
                "customer_name": name or "(unknown)",
                "order_id": None,
                "channel": "lab",
                "messages": [{"from": "customer", "text": text}],
            },
            "order_id": match.group(0) if match else None,
            "order_id_from_text": bool(match),
        }
    t["label"] = f"{t['label']} · followed {request_id}"
    return t


# ------------------------------------------------------------------------- API
# Run-shaped endpoints (/api/run, /api/follow) answer with NDJSON
# (application/x-ndjson): one JSON event per line, sent as it happens, so the
# page can draw the pipeline live. core.js runStream() splits on "\n" and
# treats the "trace" line as the result. Everything else answers plain JSON.


class RunRequest(TicketRef):
    checkpoint: str
    inject_truncate: int = 0  # 01 only: lab fault injection, 0 = off
    approval: str = (
        "ask"  # 02–04: ask | approve | reject (what to do at the approval gate)
    )
    guard: str | None = (
        None  # 04 only: heuristic | nemo (None = the service's DESKHAND_GUARD)
    )


def _line(event: dict) -> bytes:
    """One NDJSON line. default=str keeps a stray datetime or Path from breaking the stream."""
    return (json.dumps(event, default=str, ensure_ascii=False) + "\n").encode("utf-8")


def _finish_trace(
    trace: dict,
    t: dict,
    run_id: str,
    checkpoint: str,
    started: float,
    guard: str | None = None,
) -> dict:
    """Enrich a raw trace, keep it for #run/<id>, and remember its cost for
    the next estimate. The async paths call it in a worker thread: it reads
    source files and can spawn the 01 quote runner."""
    trace = enrich(trace, t)
    trace.update(
        run_id=run_id,
        ticket_key=t["key"],
        label=t["label"],
        ticket_source=t["source"],
        started_at=started,
    )
    if checkpoint == "04":
        # The guard the service actually used: the trip event says, else the request.
        tripped = (trace.get("facts") or {}).get("guard_input") or {}
        trace["guard"] = tripped.get("guard") or guard or "heuristic"
    _keep(run_id, trace)
    cost = trace["summary"].get("cost_usd")
    if cost is not None:
        OBSERVED_COST[(checkpoint, t["key"])] = cost
        OBSERVED_COST[(checkpoint, "*")] = cost
    return trace


@app.post("/api/run")
async def run(body: RunRequest) -> StreamingResponse:
    """Run one checkpoint on one ticket; stream NDJSON events.

    00/01: a plain generator over the runner's events (Starlette iterates it
    in a thread pool). 02–04: an async generator over the service's SSE.
    Both open with "start" and end with the enriched "trace" or an "error"."""
    cp = CHECKPOINTS.get(body.checkpoint)
    if cp is None:
        raise HTTPException(404, f"unknown checkpoint {body.checkpoint}")
    if body.approval not in ("ask", "approve", "reject"):
        raise HTTPException(422, "approval must be ask, approve or reject")
    if body.guard not in (None, "heuristic", "nemo"):
        raise HTTPException(422, "guard must be heuristic or nemo")
    guard = body.guard if body.checkpoint == "04" else None
    t = await asyncio.to_thread(resolve_ticket, body)
    run_id = uuid.uuid4().hex[
        :10
    ]  # the lab's id; a 02–04 run also gets the service's request_id
    started = time.time()
    # Sent before any work: the page can draw the pipeline, code links and commands at once.
    start_event = {
        "type": "start",
        "run_id": run_id,
        "checkpoint": body.checkpoint,
        "ticket_key": t["key"],
        "label": t["label"],
        "ticket_text": t["text"],
        "ticket": t["ticket"],
        "commands": commands(body.checkpoint, t),
        "cites": citations(body.checkpoint),
        "node_order": node_order(body.checkpoint),
    }

    def finish(trace: dict) -> dict:
        return _finish_trace(trace, t, run_id, body.checkpoint, started, guard)

    if cp["kind"] == "subprocess":
        if body.checkpoint == "00":
            request = {"mode": "run", "ticket_text": t["text"]}
        else:
            # Clamped to 0–2: with MAX_REPAIR_ATTEMPTS = 1, two truncated calls exhaust the repair.
            request = {
                "mode": "run",
                "ticket": t["ticket"],
                "inject": {"truncate_attempts": max(0, min(2, body.inject_truncate))},
            }

        def stream() -> Iterator[bytes]:
            stop = threading.Event()
            yield _line(start_event)
            try:
                for event in _spawn(body.checkpoint, request, stop):
                    if event.get("type") == "trace":
                        yield _line(finish(event))
                    else:
                        yield _line(event)
            finally:
                stop.set()  # client went away -> the watchdog kills the runner

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    async def astream() -> AsyncIterator[bytes]:
        yield _line(start_event)
        try:
            async for event in _run_service(
                body.checkpoint, t, run_id, body.approval, started, guard
            ):
                if event.get("type") == "trace":
                    yield _line(await asyncio.to_thread(finish, event))
                else:
                    yield _line(event)
        finally:
            LIVE.pop(
                run_id, None
            )  # however the stream ends, the run is no longer approvable from here

    return StreamingResponse(astream(), media_type="application/x-ndjson")


class ApproveRequest(BaseModel):
    approved: bool


@app.post("/api/runs/{run_id}/approve")
async def approve(run_id: str, body: ApproveRequest) -> dict:
    """Approve / Reject from the page for a run parked at the gate. Forwarded
    to the service; who decided is noted in LIVE for the trace."""
    live = LIVE.get(run_id)
    if live is None:
        raise HTTPException(
            404, "no open run with that id (it finished, or the lab restarted)"
        )
    result = await _post_approval(live["base"], live["request_id"], body.approved)
    live["decision"] = "approved" if body.approved else "rejected"
    live["decided_by"] = "you"
    return {"run_id": run_id, **result}


class FollowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checkpoint: str
    request_id: str


@app.post("/api/follow")
async def follow(body: FollowRequest) -> StreamingResponse:
    """Attach to a run the lab did not start, or lost (an F5, a lab restart,
    a crash): GET /runs/{id} for the ticket, then the same event stream from
    event 0, rendered exactly like a run started here. The gate, if still
    open, gets Approve / Reject; no auto policy, nothing is submitted."""
    checkpoint = body.checkpoint
    cp = CHECKPOINTS.get(checkpoint)
    if cp is None or cp["kind"] != "http":
        raise HTTPException(
            422, "follow works on the services: checkpoint 02, 03 or 04"
        )
    request_id = body.request_id.strip()
    if not REQUEST_ID_RE.match(request_id):
        raise HTTPException(422, "a request_id is 4 to 64 letters, digits, - or _")
    run_id = uuid.uuid4().hex[:10]
    started = time.time()

    async def astream() -> AsyncIterator[bytes]:
        base = await asyncio.to_thread(_service_base, checkpoint)
        if base is None:
            yield _line(
                {
                    "type": "error",
                    "message": f"checkpoint {checkpoint} service is not running",
                    "detail": "Start it in Setup, then follow again. The run is saved in the service's database, so nothing is lost.",
                    "needs_service": checkpoint,
                }
            )
            return
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, read=60.0)
            ) as client:
                try:
                    r = await client.get(f"{base}/runs/{request_id}")
                except httpx.HTTPError as exc:
                    yield _line(
                        {
                            "type": "error",
                            "message": f"GET /runs/{request_id} failed",
                            "detail": str(exc),
                        }
                    )
                    return
                if r.status_code == 404:
                    yield _line(
                        {
                            "type": "error",
                            "message": f"the checkpoint {checkpoint} service has no run {request_id}",
                            "detail": "A request id belongs to the service that accepted it. Was it submitted to another checkpoint?",
                        }
                    )
                    return
                if r.status_code != 200:
                    yield _line(
                        {
                            "type": "error",
                            "message": f"GET /runs/{request_id} → HTTP {r.status_code}",
                            "detail": r.text[:1500],
                        }
                    )
                    return
                state = r.json()
                t = await asyncio.to_thread(_ticket_from_state, state, request_id)
                yield _line(
                    {
                        "type": "start",
                        "run_id": run_id,
                        "checkpoint": checkpoint,
                        "followed": True,
                        "ticket_key": t["key"],
                        "label": t["label"],
                        "ticket_text": t["text"],
                        "ticket": t["ticket"],
                        "commands": commands(checkpoint, t, request_id),
                        "cites": citations(checkpoint),
                        "node_order": node_order(checkpoint),
                    }
                )
                _remember_request(
                    checkpoint, request_id, t["label"].split(" · followed ")[0]
                )
                LIVE[run_id] = {
                    "checkpoint": checkpoint,
                    "base": base,
                    "request_id": request_id,
                }
                yield _line(
                    {
                        "type": "request_id",
                        "request_id": request_id,
                        "base_url": base,
                        "followed": True,
                        "status": state.get("status"),
                        "next_node": state.get("next_node"),
                    }
                )
                # Replayed from event 0, an answered gate's approval.required must not ask again.
                decided = state.get("approval_status") in ("approved", "rejected")
                # Log offset 0: the run may predate this lab, so search the whole log for its request_id.
                async for event in _watch(
                    client,
                    checkpoint,
                    base,
                    request_id,
                    run_id,
                    "ask",
                    started,
                    t,
                    _log_path(checkpoint),
                    0,
                    gate_decided=decided,
                ):
                    if event.get("type") == "trace":
                        event["followed"] = True
                        yield _line(
                            await asyncio.to_thread(
                                _finish_trace,
                                event,
                                t,
                                run_id,
                                checkpoint,
                                started,
                                state.get("guard"),
                            )
                        )
                    else:
                        yield _line(event)
        finally:
            LIVE.pop(run_id, None)

    return StreamingResponse(astream(), media_type="application/x-ndjson")


@app.get("/api/follow/recent")
def recent_requests() -> dict:
    """The request ids this lab server submitted, newest first: after an F5
    the page has forgotten them, the lab has not."""
    return {"recent": list(reversed(RECENT))}


# ------------------------------------------------- policy index (checkpoint 03)
# The Session 7–8 retrieval demos on checkpoint 03's own code, without the
# planner: search, distance and map, each one cp03search.py runner process.


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    k: int | None = None  # None = retrieve.TOP_K, as the service uses it
    doc_ids: list[str] | None = None  # Session 8: filter inside the search


class DistanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrases: list[str]


class MapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = None


_DOC_ID_RE = re.compile(r"doc_id:\s*([\w-]+)")


@app.get("/api/policy-docs")
def policy_docs() -> dict:
    """The doc_ids in shared/policies (line 1 of each file), for the lab's
    "only this document" filter. Read from the files, never hardcoded."""
    docs = []
    for path in sorted((REPO / "shared" / "policies").glob("*.md")):
        head = path.read_text(encoding="utf-8", errors="replace")[:300]
        match = _DOC_ID_RE.search(head)
        if match:
            docs.append({"doc_id": match.group(1), "file": path.name})
    return {"docs": docs}


def _require(cp: str) -> None:
    """A clear 404 for a checkpoint this copy of the repo doesn't have yet."""
    if cp not in CHECKPOINTS:
        raise HTTPException(404, f"checkpoint {cp} isn't in this copy of the repo yet")


async def _index_mode(mode: str, request: dict, what: str) -> dict:
    """Run one cp03search.py mode that opens the policy index. Qdrant's local
    mode lets one process open qdrant_data/ at a time, so a running 03
    service is reported, not fought; so is any other holder of the lock."""
    _require("03")
    folder = REPO / CHECKPOINTS["03"]["dir"]
    if not (folder / "qdrant_data").exists():
        return {
            "ok": False,
            "code": "no_index",
            "message": "Checkpoint 03 has no policy index yet. Build it once (well under a cent):",
            "cmd": "cd checkpoints\\03-rag\npy ingest.py",
        }
    if await asyncio.to_thread(_service_base, "03"):
        info = MANAGED.get("03") or {}
        managed = bool(info.get("proc") and info["proc"].poll() is None)
        return {
            "ok": False,
            "code": "service_running",
            "managed": managed,
            "message": "The checkpoint 03 service is running, and it holds the policy index: Qdrant's local mode "
            f"lets one process open checkpoints/03-rag/qdrant_data/ at a time. Stop the service to {what}, "
            "and start it again before the next run.",
        }
    started = time.perf_counter()
    events = await asyncio.to_thread(
        lambda: list(_spawn("03", dict(request, mode=mode), runner="cp03search.py"))
    )
    found = next((e for e in events if e.get("type") == mode), None)
    if found is None:
        err = next((e for e in events if e.get("type") == "error"), {}) or {}
        if err.get("code") == "index_locked":
            return {
                "ok": False,
                "code": "index_locked",
                "message": "Another process has checkpoint 03's policy index open (an eval, a test, ingest.py, or a "
                "service started outside the lab on another port). Qdrant's local mode allows one process "
                f"per folder: wait for it to finish or stop it, then {what} again.",
            }
        return {
            "ok": False,
            "code": "failed",
            "message": err.get("message") or f"the {mode} runner failed",
            "detail": _first_error(events),
        }
    found["duration_ms"] = int((time.perf_counter() - started) * 1000)
    found["ok"] = True
    return found


@app.post("/api/search")
async def search(body: SearchRequest) -> dict:
    """Checkpoint 03's own retrieve.retrieve() on a bare query, in a runner
    subprocess (the same call as `py retrieve.py "<query>"`): one embedding
    call, no triage, no planner. `doc_ids` is retrieve()'s own filter, run
    inside the store's search; the runner then also searches unfiltered so
    the lab can show what dropping rows afterwards would have kept."""
    query = body.query.strip()
    if not query or len(query) > 2000:
        raise HTTPException(422, "a query is 1 to 2000 characters")
    if body.k is not None and not 1 <= body.k <= 20:
        raise HTTPException(422, "k is 1 to 20")
    doc_ids = [d.strip() for d in (body.doc_ids or []) if d.strip()]
    if any(not re.fullmatch(r"[\w-]{1,64}", d) for d in doc_ids):
        raise HTTPException(422, "a doc_id is letters, digits, - and _")
    found = await _index_mode(
        "search", {"query": query, "k": body.k, "doc_ids": doc_ids or None}, "search"
    )
    if not found["ok"]:
        return found
    found["cites"] = {
        k: v
        for k, v in citations("03").items()
        if k in ("top_k", "build_query", "embed", "retrieve_policy")
    }
    found["cites"]["retrieve"] = cite("checkpoints/03-rag/retrieve.py", "def retrieve(")
    found["cites"]["doc_filter"] = cite(
        "checkpoints/03-rag/retrieve.py", "def _doc_filter("
    )
    found["cmd"] = (
        "cd checkpoints\\03-rag\npy retrieve.py "
        + "".join("--doc " + d + " " for d in doc_ids)
        + ps_quote(query)
    )
    return found


@app.post("/api/distance")
async def distance(body: DistanceRequest) -> dict:
    """Cosine similarity between 2 to 4 phrases, through checkpoint 03's
    llm.embed_texts (one call). No index is opened, so it works while the 03
    service runs."""
    _require("03")
    phrases = [p.strip() for p in body.phrases if p.strip()]
    if not 2 <= len(phrases) <= 4:
        raise HTTPException(422, "give 2 to 4 phrases")
    if any(len(p) > 200 for p in phrases):
        raise HTTPException(422, "a phrase is at most 200 characters")
    started = time.perf_counter()
    events = await asyncio.to_thread(
        lambda: list(
            _spawn(
                "03", {"mode": "distance", "phrases": phrases}, runner="cp03search.py"
            )
        )
    )
    found = next((e for e in events if e.get("type") == "distance"), None)
    if found is None:
        err = next((e for e in events if e.get("type") == "error"), {}) or {}
        return {
            "ok": False,
            "code": "failed",
            "message": err.get("message") or "the distance runner failed",
            "detail": _first_error(events),
        }
    found["duration_ms"] = int((time.perf_counter() - started) * 1000)
    found["cites"] = {"embed": citations("03").get("embed")}
    found["ok"] = True
    return found


@app.post("/api/map")
async def map_index(body: MapRequest) -> dict:
    """Every section of the policy index on two PCA axes (numpy in the
    runner), plus, with a query, its point and its real top-k. Opens the
    index, so the same lock rules as /api/search."""
    query = (body.query or "").strip() or None
    if query and len(query) > 2000:
        raise HTTPException(422, "a query is at most 2000 characters")
    found = await _index_mode("map", {"query": query}, "draw the map")
    if not found["ok"]:
        return found
    found["cites"] = {
        k: v for k, v in citations("03").items() if k in ("top_k", "embed")
    }
    found["cites"]["retrieve"] = cite("checkpoints/03-rag/retrieve.py", "def retrieve(")
    return found


# ------------------------------------------------------------------- estimates


class EstimateRequest(TicketRef):
    checkpoints: list[str]
    runs: int = 1


@app.post("/api/estimate")
def estimate(body: EstimateRequest) -> dict:
    """A low–high cost for N runs, for the confirm dialog before anything is
    spent. A cost already observed for this ticket (or, on 02–04, for this
    checkpoint) wins; otherwise a rough token count from character lengths,
    priced with checkpoint 01's table for the model the processes will use."""
    if body.runs > MAX_REPEAT:
        raise HTTPException(422, f"at most {MAX_REPEAT} runs per batch")
    t = resolve_ticket(body)
    env = effective_env()
    provider = (env.get("LLM_PROVIDER") or "azure").strip().lower()
    model = {
        "azure": env.get("AZURE_OPENAI_DEPLOYMENT"),
        "anthropic": env.get("ANTHROPIC_MODEL") or "claude-opus-5-5",
    }.get(provider, env.get("OPENAI_MODEL"))
    if provider in COMPATIBLE:
        model = compatible_model(provider, env)
    prices = price_quote([model]).get(model or "", {}) if model else {}
    v1_model = _v1_llm(effective_env("00"))["model"]
    v1_prices = price_quote([v1_model]).get(v1_model or "", {}) if v1_model else {}
    rows = []
    for cp in body.checkpoints:
        if cp not in CHECKPOINTS:
            continue
        cp_prices = v1_prices if cp == "00" else prices
        seen = OBSERVED_COST.get((cp, t["key"]))
        if seen is None and cp in ("02", "03", "04"):
            seen = OBSERVED_COST.get((cp, "*"))
            basis_seen = "last run on this checkpoint"
        else:
            basis_seen = "last run of this ticket"
        # The rough formulas: characters / 4 (4.6 for 00's policy prose) is a
        # token count; the added characters are the prompt around the ticket;
        # 1500 tokens stand for 03/04's retrieved sections. Low to high spans
        # the retries: 00 tries up to 3 times, 01 repairs once.
        if seen is not None:
            # One observed run, widened: the next one can retry or repair more.
            low, high, basis = seen, seen * (3 if cp == "00" else 2), basis_seen
        elif cp == "00":
            policy_chars = sum(p.stat().st_size for p in POLICIES_DIR.glob("*.md"))
            per_attempt = (
                _cost(
                    cp_prices,
                    {
                        "prompt_tokens": (policy_chars + len(t["text"]) + 400) / 4.6,
                        "completion_tokens": 200,
                    },
                )
                or 0.0
            )
            low, high, basis = (
                per_attempt,
                per_attempt * 3,
                "rough: (policies + ticket chars) / 4.6 tokens, 1–3 attempts",
            )
        elif cp == "01":
            per_attempt = (
                _cost(
                    prices,
                    {
                        "prompt_tokens": (len(t["text"]) + 1200) / 4,
                        "completion_tokens": 30,
                    },
                )
                or 0.0
            )
            low, high, basis = (
                per_attempt,
                per_attempt * 2,
                "rough: (system prompt + ticket chars) / 4 tokens, 1–2 attempts",
            )
        else:
            policy_tokens = 1500 if cp in ("03", "04") else 0
            per_call = (
                _cost(
                    prices,
                    {
                        "prompt_tokens": (len(t["text"]) + 1600) / 4 + policy_tokens,
                        "completion_tokens": 220,
                    },
                )
                or 0.0
            )
            low, high, basis = (
                per_call * 3,
                per_call * 6,
                "rough: triage + plan + reply (3 calls), up to 2 repairs",
            )
        rows.append(
            {
                "checkpoint": cp,
                "per_run_low": low,
                "per_run_high": high,
                "basis": basis,
                "model": v1_model if cp == "00" else model,
            }
        )
    total_high = sum((r["per_run_high"] or 0) for r in rows) * body.runs
    total_low = sum((r["per_run_low"] or 0) for r in rows) * body.runs
    return {
        "rows": rows,
        "runs": body.runs,
        "total_low": total_low,
        "total_high": total_high,
        "model": model,
        "priced": bool(prices and prices.get("input")),
        "commands": {
            cp: commands(cp, t, repeat=body.runs)[:1]
            for cp in body.checkpoints
            if cp in CHECKPOINTS
        },
    }


# ------------------------------------------------------------- the Logs page
# GET /api/logs: every checkpoint's logs/runs.jsonl as one stream, the way a
# production log explorer shows a service (spec: specs/logs-page.md).
# Read only, and only these files: the endpoint takes no path. Lines are
# found by byte offset, so the cursor is the whole state and a lab restart
# loses nothing. A line without its newline yet (a run mid-write) waits for
# the next poll.

LOG_TAIL_MAX = 5000  # most lines the first load may ask for
LOG_AFTER_MAX = 2000  # most lines one after= or request_id= reply carries
LOG_SCAN_MAX_BYTES = (
    20_000_000  # request_id= reads at most this much of each file's end
)
_LOG_BLOCK = 64 * 1024  # tail reads backwards in blocks this size

# The level is the lab's reading of the event name; obslog writes none.
# Checked in this order. An event not listed is INFO, so a new one shows.
_LEVEL_ERROR = {"run.failed", "node.error", "triage.failed"}
_LEVEL_WARN = {
    "recovery.resumed_runs",
    "triage.repair",
}  # plus any line with an error code
_LEVEL_DEBUG = {"node.started", "node.completed", "run.status"}


def _log_level(rec: dict | None) -> str:
    """ERROR, WARN, INFO or DEBUG for one parsed line (None: not JSON)."""
    if rec is None:
        return "ERROR"
    event = rec.get("event")
    if event in _LEVEL_ERROR:
        return "ERROR"
    if rec.get("internal_error_code") or event in _LEVEL_WARN:
        return "WARN"
    if event in _LEVEL_DEBUG:
        return "DEBUG"
    return "INFO"


def _log_sources() -> list[str]:
    """The checkpoints that write a runs.jsonl (00 has no log: its lesson)."""
    return [cp for cp in ("01", "02", "03", "04") if cp in CHECKPOINTS]


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _complete_end(fh, size: int) -> int:
    """The offset just past the file's last newline."""
    pos = size
    while pos > 0:
        step = min(_LOG_BLOCK, pos)
        fh.seek(pos - step)
        i = fh.read(step).rfind(b"\n")
        if i != -1:
            return pos - step + i + 1
        pos -= step
    return 0


def _tail_start(fh, end: int, n: int) -> int:
    """Where the last n lines before `end` begin, reading backwards in blocks,
    so a 50 MB log costs the same as a 50 KB one."""
    pos, seen = end, 0
    while pos > 0:
        step = min(_LOG_BLOCK, pos)
        fh.seek(pos - step)
        block = fh.read(step)
        i = len(block)
        while True:
            i = block.rfind(b"\n", 0, i)
            if i == -1:
                break
            at = pos - step + i
            if at == end - 1:
                continue  # the newline that ends the last line
            seen += 1
            if seen == n:
                return at + 1
        pos -= step
    return 0


def _raw_lines(
    fh, start: int, end: int, limit: int | None = None
) -> tuple[list[tuple[int, int, bytes]], bool]:
    """(offset, end offset, bytes) for each line in [start, end), and whether
    the read got to `end` (False when `limit` lines stopped it first)."""
    fh.seek(start)
    pos, out = start, []
    while pos < end:
        if limit is not None and len(out) >= limit:
            return out, False
        raw = fh.readline()
        if not raw:
            break
        out.append((pos, pos + len(raw), raw))
        pos += len(raw)
    return out, True


def _log_line(src: str, offset: int, raw: bytes, secrets: list[str]) -> dict | None:
    """One line as the page gets it, redacted. None for a blank line."""
    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    for secret in secrets:  # redact()'s rule, with .env read once per request
        text = text.replace(secret, "[redacted]")
    if not text.strip():
        return None
    try:
        rec = json.loads(text)
    except json.JSONDecodeError:
        rec = None
    if not isinstance(rec, dict):
        rec = None
    ts = rec.get("ts") if rec is not None and isinstance(rec.get("ts"), str) else None
    return {
        "id": f"{src}:{offset}",
        "src": src,
        "ts": ts,
        "level": _log_level(rec),
        "rec": rec if rec is not None else {"raw": text},
    }


def _parse_file(
    src: str, raws: list[tuple[int, int, bytes]], secrets: list[str]
) -> list[tuple[tuple, int, dict]]:
    """Parsed lines with their sort key and end offset. A line with no ts
    sorts with the line before it in its own file, so it stays with its
    neighbours; ties keep service order, then file order."""
    out, last_ts, rank = [], "", _log_sources().index(src)
    for offset, end_off, raw in raws:
        line = _log_line(src, offset, raw, secrets)
        if line is None:
            continue
        last_ts = line["ts"] or last_ts
        out.append(((last_ts, rank, offset), end_off, line))
    return out


def _encode_cursor(offsets: dict[str, int]) -> str:
    return ",".join(f"{cp}:{offsets[cp]}" for cp in _log_sources())


def _decode_cursor(cursor: str) -> dict[str, int]:
    offsets: dict[str, int] = {}
    try:
        for part in cursor.split(","):
            cp, offset = part.split(":")
            if cp not in _log_sources() or int(offset) < 0:
                raise ValueError(part)
            offsets[cp] = int(offset)
    except ValueError:
        raise HTTPException(
            400, "unreadable cursor: pass back the cursor a previous reply gave"
        )
    return offsets


def _source_info(cp: str) -> dict:
    folder = CHECKPOINTS[cp]["dir"]
    path = _log_path(cp)
    return {
        "id": cp,
        "label": Path(folder).name,
        "path": f"{folder}/logs/runs.jsonl",
        "exists": path.exists(),
        "size_bytes": _log_size(path),
    }


def _logs_tail(n: int, secrets: list[str]) -> dict:
    parsed, cursor = [], {}
    for cp in _log_sources():
        path = _log_path(cp)
        cursor[cp] = 0
        if not path.exists():
            continue
        with path.open("rb") as fh:
            end = _complete_end(fh, _log_size(path))
            raws, _ = _raw_lines(fh, _tail_start(fh, end, n), end)
        cursor[cp] = end
        parsed += _parse_file(cp, raws, secrets)
    parsed.sort(key=lambda p: p[0])
    return {
        "lines": [p[2] for p in parsed[-n:]],
        "cursor": _encode_cursor(cursor),
        "more": False,
    }


def _logs_after(cursor: dict[str, int], secrets: list[str]) -> dict:
    limit = LOG_AFTER_MAX
    per_file, resets = {}, []
    for cp in _log_sources():
        path = _log_path(cp)
        offset, size = cursor.get(cp, 0), _log_size(path)
        if offset > size:  # cleared or deleted while we tailed it
            offset = 0
            resets.append(cp)
        if not path.exists():
            per_file[cp] = (offset, offset, [], True)
            continue
        with path.open("rb") as fh:
            end = _complete_end(fh, size)
            raws, reached = _raw_lines(fh, offset, end, limit + 1)
        per_file[cp] = (offset, end, _parse_file(cp, raws, secrets), reached)

    merged = sorted(
        (p for (_, _, parsed, _) in per_file.values() for p in parsed),
        key=lambda p: p[0],
    )
    sent = merged[:limit]
    sent_ids = {p[2]["id"] for p in sent}
    more = len(merged) > limit or any(
        not reached for (_, _, _, reached) in per_file.values()
    )

    # Each file's cursor stops after the last line sent from it.
    new_cursor = {}
    for cp, (offset, end, parsed, reached) in per_file.items():
        mine = [p for p in parsed if p[2]["id"] in sent_ids]
        if reached and len(mine) == len(parsed):
            new_cursor[cp] = end
        elif mine:
            new_cursor[cp] = mine[-1][1]
        else:
            new_cursor[cp] = offset
    return {
        "lines": [p[2] for p in sent],
        "cursor": _encode_cursor(new_cursor),
        "more": more,
        "resets": resets,
    }


def _logs_for_run(request_id: str, secrets: list[str]) -> dict:
    needle = json.dumps(request_id).encode("utf-8")  # as it appears in the line, quoted
    parsed, truncated = [], False
    for cp in _log_sources():
        path = _log_path(cp)
        if not path.exists():
            continue
        with path.open("rb") as fh:
            end = _complete_end(fh, _log_size(path))
            start = max(0, end - LOG_SCAN_MAX_BYTES)
            if start > 0:
                truncated = True
                fh.seek(start - 1)
                if fh.read(1) != b"\n":
                    fh.readline()  # skip the partial line the window starts in
                start = fh.tell()
            raws, _ = _raw_lines(fh, start, end)
        hits = [r for r in raws if needle in r[2]]
        parsed += [
            p
            for p in _parse_file(cp, hits, secrets)
            if p[2]["rec"].get("request_id") == request_id
        ]
    parsed.sort(key=lambda p: p[0])
    return {
        "lines": [p[2] for p in parsed[:LOG_AFTER_MAX]],
        "more": len(parsed) > LOG_AFTER_MAX,
        "truncated": truncated,
        "request_id": request_id,
    }


@app.get("/api/logs")
def api_logs(
    tail: int = 1000, after: str | None = None, request_id: str | None = None
) -> dict:
    """The newest `tail` lines, or the lines since `after`, or one whole run.
    Every reply lists lines oldest first; the page reverses them."""
    secrets = _secrets()
    if request_id is not None:
        body = _logs_for_run(request_id, secrets)
    elif after is not None:
        body = _logs_after(_decode_cursor(after), secrets)
    else:
        body = _logs_tail(max(1, min(tail, LOG_TAIL_MAX)), secrets)
    body["sources"] = [_source_info(cp) for cp in _log_sources()]
    return body


# ------------------------------------------------------------ read-only lookups


@app.get("/api/cites/{checkpoint}")
def get_cites(checkpoint: str) -> dict:
    return citations(checkpoint)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    trace = RUNS.get(run_id)
    if trace is None:
        raise HTTPException(
            404, "unknown run_id (runs live in lab server memory until it restarts)"
        )
    return trace


@app.get("/api/incidents")
def incident_board() -> dict:
    """The board, with `available` false on a fix whose checkpoint this copy
    of the repo doesn't have yet (the students' copy grows week by week)."""
    return {
        "incidents": [
            {
                **inc,
                "retired": {
                    **inc["retired"],
                    "available": inc["retired"]["checkpoint"] in CHECKPOINTS,
                },
            }
            for inc in INCIDENT_BOARD
        ]
    }


@app.get("/api/tickets")
def tickets() -> dict:
    """Everything the pickers need: the dataset tickets (with the flat text 00
    reads), the Session 1 demos and the checkpoint catalogue. A failure to
    read incidents.py is reported in the answer, not raised."""
    try:
        demo = incidents()
        demo_error = None
    except HTTPException as exc:
        demo, demo_error = [], exc.detail
    return {
        "tickets": [
            {**t, "text": _flatten(t), "message_count": len(t["messages"])}
            for t in _tickets()
        ],
        "incidents": demo,
        "incidents_error": demo_error,
        "incidents_cite": cite(
            "checkpoints/00-broken-bot/incidents.py", "INCIDENTS = ["
        ),
        "checkpoints": [
            {
                "id": k,
                "name": v["name"],
                "kind": v["kind"],
                "dir": v["dir"],
                "blurb": v["blurb"],
                "sessions": v["sessions"],
                "port": v.get("port"),
                "nodes": node_order(k),
                "start": _service_start_cmd(k) if v["kind"] == "http" else None,
            }
            for k, v in CHECKPOINTS.items()
        ],
        "max_repeat": MAX_REPEAT,
    }


# ---------------------------------------------------------------------- health


async def _probe(client: httpx.AsyncClient, url: str) -> dict | None:
    """_probe_sync() for the event loop, so /api/health can probe every port at once."""
    try:
        r = await client.get(url)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


@app.get("/api/health")
async def health() -> dict:
    """The page polls this for the header and the Setup panel: the LLM config
    (whether keys are set, never their values), the mock API, every probed
    service port, the managed processes and their flags, the model picker,
    and whether server.py changed since this process started."""
    env = effective_env()
    provider = (env.get("LLM_PROVIDER") or "azure").strip().lower()
    if provider == "azure":
        llm = {
            "provider": "azure",
            "deployment": env.get("AZURE_OPENAI_DEPLOYMENT") or None,
            "api_version": env.get("AZURE_OPENAI_API_VERSION") or None,
            "embedding_deployment": env.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
            or None,
            "strong_deployment": env.get("AZURE_OPENAI_DEPLOYMENT_STRONG") or None,
            "key_set": _is_set(env.get("AZURE_OPENAI_API_KEY")),
            "endpoint_set": _is_set(env.get("AZURE_OPENAI_ENDPOINT")),
        }
    elif provider == "anthropic":
        llm = {
            "provider": "anthropic",
            "deployment": env.get("ANTHROPIC_MODEL") or "claude-opus-5-5",
            "strong_deployment": env.get("ANTHROPIC_MODEL_STRONG") or None,
            "embedding_provider": env.get("EMBEDDING_PROVIDER") or "openai",
            "key_set": _is_set(env.get("ANTHROPIC_API_KEY")),
            "endpoint_set": True,
        }
    elif provider in COMPATIBLE:
        llm = {
            "provider": provider,
            "deployment": compatible_model(provider, env),
            "strong_deployment": env.get(COMPATIBLE[provider]["model_var"] + "_STRONG")
            or None,
            "key_set": compatible_key_set(provider, env),
            "endpoint_set": True,
        }
    else:
        llm = {
            "provider": provider,
            "deployment": env.get("OPENAI_MODEL") or None,
            "strong_deployment": env.get("OPENAI_MODEL_STRONG") or None,
            "key_set": _is_set(env.get("OPENAI_API_KEY")),
            "endpoint_set": True,
        }
    llm["env_file"] = ".env" if ENV_PATH.exists() else None
    llm["ready"] = bool(llm["key_set"] and llm["endpoint_set"] and llm["deployment"])
    llm["budget_per_run"] = (
        env.get("DESKHAND_BUDGET_USD_PER_RUN")
        or os.getenv("DESKHAND_BUDGET_USD_PER_RUN")
        or "0.25"
    )
    llm["runtime_env"] = env.get("DESKHAND_RUNTIME") or "plain"

    mock_base = _mock_base()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(0.8, connect=LOCAL_TIMEOUT.connect)
    ) as client:
        probes = await asyncio.gather(
            _probe(client, _loopback(f"{mock_base}/openapi.json")),
            *[
                _probe(client, f"http://127.0.0.1:{p}/openapi.json")
                for p in SERVICE_PORTS
            ],
        )
    mock_doc, service_docs = probes[0], probes[1:]
    for name in list(MANAGED):
        _reap(name)
    mock_managed = MANAGED.get("mock")
    mock = {
        "up": bool(mock_doc),
        "url": mock_base,
        "title": (mock_doc or {}).get("info", {}).get("title"),
        "start": "cd shared\\mock_order_api\npy -m uvicorn app:app --port 9000",
        "check": f"curl.exe -s {mock_base}/orders/O-48291",
        "managed": bool(
            mock_managed
            and mock_managed.get("proc")
            and mock_managed["proc"].poll() is None
        ),
        "chaos_modes": chaos_modes(),
    }
    mock["flags"] = ((mock_managed or {}).get("flags") or []) if mock["managed"] else []

    # checkpoint id -> the ports whose FastAPI title names it (one service can be up twice)
    found: dict[str, list[int]] = {}
    for port, doc in zip(SERVICE_PORTS, service_docs):
        title = ((doc or {}).get("info") or {}).get("title") or ""
        match = re.search(r"checkpoint (\d\d)", title)
        if match:
            found.setdefault(match.group(1), []).append(port)

    services = []
    v1 = _v1_llm(effective_env("00"))
    choice = current_choice()
    if choice != "gpt" and v1["ready"]:
        v1["note"] = (
            "bot.py only knows Azure and OpenAI, so checkpoint 00 stays on GPT ("
            + (v1["model"] or "?")
            + ")"
        )
    for cp_id, cp in CHECKPOINTS.items():
        if cp["kind"] != "http":
            entry = {
                "checkpoint": cp_id,
                "kind": "subprocess",
                "up": None,
                "ready": llm["ready"],
                "note": "no server — the lab runs it as a subprocess on demand",
            }
            if cp_id == "00":
                entry.update(
                    ready=v1["ready"], model=v1["model"], provider_note=v1["note"]
                )
                if not v1["ready"]:
                    entry["not_ready"] = (
                        ("set OPENAI_API_KEY in .env: " + v1["note"])
                        if v1["note"]
                        else "fill .env first"
                    )
            services.append(entry)
            continue
        ports = found.get(cp_id, [])
        info = MANAGED.get(cp_id) or {}
        proc = info.get("proc")
        managed_up = bool(proc and proc.poll() is None)
        needs_index = (
            cp.get("setup") and not (REPO / cp["dir"] / "qdrant_data").exists()
        )
        services.append(
            {
                "checkpoint": cp_id,
                "kind": "http",
                "up": bool(ports),
                "ports": ports,
                "url": f"http://localhost:{ports[0]}" if ports else None,
                "port": cp["port"],
                "start": _service_start_cmd(cp_id),
                "managed": managed_up,
                "runtime": info.get("runtime") if managed_up else None,
                # a service started in a terminal reads .env, whatever the picker says
                "model_choice": info.get("model") if managed_up else None,
                "model_mismatch": bool(ports)
                and MODEL_CHOICE["id"] is not None
                and not (managed_up and info.get("model") == choice),
                "exit_code": info.get("exit_code") if info and not managed_up else None,
                # start-time flags: what the running process got, or what the last one died with
                "flags": (info.get("flags") or []) if managed_up else [],
                "last_flags": (info.get("flags") or [])
                if info and not managed_up
                else [],
                "flag_fields": service_flags(cp_id),
                "needs_index": bool(needs_index),
                "needs_mock": True,
                "ready": bool(ports) and bool(mock_doc),
            }
        )
    try:
        # server.py edited under a running lab: the page offers the restart command.
        stale = Path(__file__).stat().st_mtime != SERVER_MTIME
    except OSError:
        stale = False
    model = {
        "choice": choice,
        "picked": MODEL_CHOICE["id"] is not None,
        "options": _model_options(_env()),
    }
    return {
        "llm": llm,
        "model": model,
        "mock_api": mock,
        "services": services,
        "lab_port": LAB_PORT,
        "probed_ports": SERVICE_PORTS,
        "server": {
            "stale": stale,
            "restart": "py lab\\server.py",
            "python": sys.executable,
        },
    }


# ---------------------------------------------------------------------- the UI
# Plain static files: no build step, no CDN, works offline in class.


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    print(f"Deskhand Lab on http://localhost:{LAB_PORT}  (Ctrl+C to stop)")
    prewarm()
    try:
        # 127.0.0.1 only: anyone who can reach this port can spend the API key.
        uvicorn.run(app, host="127.0.0.1", port=LAB_PORT, log_level="warning")
    finally:
        _stop_all_managed()  # Ctrl+C: stop what the lab started (atexit is the backstop)
        _stop_warm()
