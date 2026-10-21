# Deskhand Workshop

Teaching repository for the SE Factory **Applied AI Engineering** bootcamp (4 weeks, 12 sessions).

**Deskhand** is an AI support-ticket-resolution system for a fictional e-commerce store,
**Nimbus Electronics**. Customers file tickets in natural language; the system produces a
machine-readable resolution plan (category, actions, refund amount, approval flag), a
customer-facing reply draft, and an internal note for the support agent — grounded in the
store's policy documents and the customer's order data.

The pedagogy: you inherit a **broken naive bot** (checkpoint 00) and rebuild it, week by
week, into a **production-grade workflow** (checkpoint 04). Every session retires one or
more items from the v1 incident list in the capstone brief.

## What's in your copy so far

This repo grows after each session, so it never shows you more than you need yet. Run `git pull` after class to get the latest.

| Arrived    | What came in                                                                                        |
| ---------- | --------------------------------------------------------------------------------------------------- |
| Sun Oct 18 | Setup files, the shared data (tickets, policies, mock order API), checkpoint 00-broken-bot          |
| Mon Oct 19 | Session 1 slides and handout, checkpoint 01-triage, the lab (lab/)                                  |
| Wed Oct 21 | Session 2 slides and handout, the Session 1 measuring script, free LLM providers (LLM_PROVIDERS.md) |

Next update: Fri Oct 23, after class.

## Repo map

```
deskhand-workshop/
  shared/
    data/tickets.json        60 sample support tickets (the shared classroom dataset)
    data/check_tickets.py    the dataset's own checks (schema, tags, orders, golden coverage)
    policies/                6 Nimbus policy documents (the shared RAG corpus)
    mock_order_api/          FastAPI mock of the order system, with chaos modes (port 9000)
  checkpoints/
    00-broken-bot/           Session 1  — Deskhand v1, deliberately naive (the "before" picture)
    01-triage/               Sessions 2–3 — validated triage, repair loop, request-ID logging, tests
    02-service/              Sessions 4–6 — FastAPI workflow: state machine, streaming, tools, approval gate, the agent version
    03-rag/                  Sessions 7–9 — RAG over policies, citations, golden ticket set
    04-production/           Sessions 10–12 — evals as release gate, guardrails, budgets, dashboard
  slides/                    Interactive HTML decks, one per session (open in a browser, works offline)
  handouts/                  One page per session: "try this tonight", the lab steps to run at home,
                             plus build-your-own.md, an optional self-checked capstone build
  lab/                       A browser harness that runs each checkpoint and explains what it did
```

## Setup

New to Python virtual environments, APIs or LLM keys? Start with the prep kit,
<https://github.com/hasankanaan26/prep-kit>: eight short steps that end with a setup check. It's its own repo, meant to sit
next to this one (`projects/prep-kit` and `projects/deskhand-workshop`); from this folder,
run `python ..\prep-kit\code\check_setup.py`.

Python 3.12 required (the repo is developed and tested on 3.12; 3.11 is untested since
the LangChain 1.x move).

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in one provider block:

```bash
# Windows:  copy .env.example .env
# macOS/Linux:  cp .env.example .env
```

`LLM_PROVIDER` chooses which block is used — `azure` (Azure OpenAI), `openai`,
`anthropic` (Claude; it has no embeddings API, so checkpoints 03 and 04 also need
`EMBEDDING_PROVIDER` set to another provider), `gemini`, `openrouter`, or the two that
run on your own laptop, `ollama` and `lmstudio`. [LLM_PROVIDERS.md](LLM_PROVIDERS.md)
sets up each one, including the free options.
On Azure, every `model=` in the code is a **deployment name**, not a model name:
set `AZURE_OPENAI_DEPLOYMENT` for chat and `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`
(a second, separate deployment) for the RAG index from checkpoint 03 on.
Everything provider-specific lives in each checkpoint's `llm.py` — one file. Week 1
calls the provider SDKs directly; from checkpoint 02 on, `llm.py` is built on LangChain
chat models and the rest of the checkpoint never sees a provider.

### The frameworks, and where each one enters

The repo builds each thing bare first, then shows what a framework does for you. Week 1
is framework-free on purpose.

| Framework                               | Enters at                       | Used for                                                                                                  | Still yours                                                               |
| --------------------------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| LangChain (core, openai, anthropic)     | 02                              | chat models, structured output, the two tools (`@tool`)                                                   | pricing, the error taxonomy, what counts as malformed output              |
| LangGraph                               | 02 (opt-in), 03+ (only runtime) | the graph runtime: `Command`, `interrupt()`, SQLite checkpoints                                           | the nodes, the business rules, idempotency keys, the event log behind SSE |
| LangChain text splitters + Qdrant store | 03                              | `##` section chunks, the local vector store                                                               | the policy header, stable ids, the embedding bill (tiktoken)              |
| RAGAS                                   | 03                              | context precision / recall by section id, faithfulness of the rationale                                   | the `reference_contexts` labels, which inputs the judge sees              |
| DeepEval                                | 04                              | the golden set as pytest (`assert_test`), three strict `GEval` judge metrics                              | the five checks, the thresholds, the release decision                     |
| LangSmith                               | 04                              | one trace per run, found by `request_id`, with no code change                                             | `logs/runs.jsonl` and `dashboard.py`                                      |
| LangChain prebuilt agent                | 02, `agent_demo.py` only        | showing what Deskhand deliberately is not                                                                 | the decision to stay a workflow                                           |
| LangGraph, as an agent loop             | 02, `agent_bounded.py` only     | the same loop with a step budget, an `interrupt()` gate before every write, and a derived idempotency key | the three layers; which tool runs next stays the model's call             |

Use the frameworks narrowly: no chains, prompt templates or runnables beyond what the
rows above need. Deskhand is a fixed graph. Code decides when a tool runs; the model
never does, especially for refunds.

`.env` is gitignored — never commit keys.

## Running the mock order API

Several checkpoints call the Nimbus order system. It's a local mock — start it in its own
terminal and leave it running:

```bash
cd shared/mock_order_api
uvicorn app:app --port 9000
```

It serves order lookups and refund issuance, and has **chaos modes** (timeouts, 500s,
flaky failures, inconsistent data) used in the failure-injection labs. See
[shared/mock_order_api/README.md](shared/mock_order_api/README.md).

## Checkpoint philosophy

Each checkpoint folder is a **self-contained runnable project**. Code is duplicated
between checkpoints **on purpose**: to see exactly what a week added, diff two folders:

```bash
git diff --no-index checkpoints/01-triage checkpoints/02-service
```

Each checkpoint's README states what it demonstrates, which sessions it maps to, what
changed vs the previous checkpoint, how to run it, and a few "try this" exercises.

| Checkpoint                                  | Sessions | What you get                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [00-broken-bot](checkpoints/00-broken-bot/) | 1        | Deskhand v1: one giant prompt, `json.loads` and hope. Violates every convention in this repo — deliberately.                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| [01-triage](checkpoints/01-triage/)         | 2–3      | Typed `TriageResult`, structured outputs, one bounded repair re-prompt, JSONL logging with request IDs, offline tests.                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| [02-service](checkpoints/02-service/)       | 4–6      | FastAPI service: `TicketResolutionState` node runner, SQLite persistence, SSE streaming, human approval gate. The provider boundary moves onto LangChain chat models, and the order-lookup and idempotent refund calls become LangChain tools. Two runtimes for the same nodes: the hand-written `graph.py` (Session 4) and LangGraph's `graph_lg.py` (Session 5 lab). `agent_demo.py` hands the same tools to LangChain's prebuilt agent, to show why Deskhand isn't one; `agent_bounded.py` puts the budget, the gate and the derived key back around that loop. |
| [03-rag](checkpoints/03-rag/)               | 7–9      | LangGraph is the only runtime. Policy ingestion with LangChain's `##` splitter into a local Qdrant store, retrieval node, plans must cite policy, the planted contradiction (and the measurement that a prompt is not a guard), 20-ticket golden set with `reference_contexts`, RAGAS retrieval metrics.                                                                                                                                                                                                                                                           |
| [04-production](checkpoints/04-production/) | 10–12    | The golden set as a DeepEval test file and a rate-based release gate, three strict `GEval` judge metrics, LangSmith tracing by `request_id`, injection guardrails, budget caps + model routing, cost/latency dashboard, chaos lab guide.                                                                                                                                                                                                                                                                                                                           |

## Shared data notes

- `shared/data/tickets.json` — 60 tickets (T-1036 onward are dataset v2: injection
  variants with no trigger phrase, hidden in the order record, role-spoofed or
  obfuscated; identity mismatches; benign look-alikes; partial, boundary and
  duplicate refunds; threads, quoted replies, non-English, over-length) spanning happy paths, ambiguous asks, angry
  customers, prompt-injection attempts, out-of-policy demands, missing order refs, long
  threads, and two tickets that trigger a planted policy contradiction. A few tickets
  reference order numbers the mock API doesn't know (typos, ancient orders) — that 404 is
  a feature, not a bug.
- `shared/policies/` — six markdown policy docs. Read them closely in Week 3; they do not
  all agree with each other.

## Where to start

Session 1 starts at [checkpoints/00-broken-bot](checkpoints/00-broken-bot/) and the
[slides](slides/). The capstone brief arrives in `capstone/` at the end of Week 2.
