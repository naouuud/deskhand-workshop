# Deskhand Lab

A browser UI that **runs each checkpoint and explains what it did**. Pick a
ticket, pick a checkpoint, press Run. You get one headline sentence, the
pipeline that produced it, and the evidence underneath.

The lab is a **harness, not a reimplementation**. Every number on the page
comes from the real checkpoint code, run unmodified and observed from the
outside. Delete `lab/` and nothing in `checkpoints/` changes.

All five checkpoints run from the lab:

| Checkpoint                        | How the lab runs it                                         |
| --------------------------------- | ----------------------------------------------------------- |
| 00 broken bot, 01 triage          | in their own subprocess, through `lab/runners/`             |
| 02 service, 03 RAG, 04 production | through their HTTP service, which the lab can start for you |

## Start it (Windows)

From the repo root, venv active, `.env` filled in (see the root README):

```powershell
py lab\server.py
```

Open **<http://localhost:8010>**. The server binds to `127.0.0.1` only:
every run spends real money on your API key.

Press **Setup** (top right) to see what is up: the LLM configuration (names
only, never keys), the mock order API, and the three services. Each one has
a **Start** button, and the command to start it in a terminal instead.
Services the lab starts stop when the lab stops. The mock order API always
has a **Stop** button while it is up, even when you started it in a terminal
(the lab checks the port really serves the mock API before ending that
process). Checkpoint 02 offers
**plain** or **LangGraph** at start time (the Session 4 / Session 5 runtimes).

**Start-time flags.** While a service is stopped, its Setup row offers the
switches that service's code reads, and Start passes the ticked ones as the
environment of that one process (never `.env`, never the lab's own
environment):

| Service        | Flag in Setup                                    | Variable                        | What it does                                                                                              |
| -------------- | ------------------------------------------------ | ------------------------------- | --------------------------------------------------------------------------------------------------------- |
| mock order API | chaos: none, flaky, error, timeout, inconsistent | `MOCK_CHAOS`                    | flaky fails half the calls with 500, error all of them, timeout hangs 30s, inconsistent drops `total_usd` |
| 02, 03, 04     | crash after refund                               | `DESKHAND_CRASH_AFTER_REFUND=1` | `execute_actions` exits the process right after the refund POST, before saving state                      |
| 04             | kill switch                                      | `DESKHAND_KILL_SWITCH=1`        | every run stops before its next node with `KILL_SWITCH_ENGAGED`                                           |
| 04             | budget $                                         | `DESKHAND_BUDGET_USD_PER_RUN`   | the per-run cap; `0.001` trips `BUDGET_EXCEEDED` on the first call                                        |

The server whitelists them: the chaos modes are read from the mock's
`CHAOS_MODES`, a checkpoint flag is offered only if its variable appears in
that checkpoint's code, the budget must be above 0 and at most 5, and any
other field is a 422. A running service shows its flags as orange badges in
Setup, on its checkpoint card and in the run report; the Setup dot turns
orange too, so a crash switch left on is hard to miss. A flag inherited from
`.env` or the lab's shell is badged with its source. Flags last for one
process: Stop, then Start with nothing ticked, and the service is plain again.
A model switch restarts a service with the same flags. A service started in a
terminal shows no badges (the lab cannot see its environment).

| Env var             | Default               | Meaning                                                                  |
| ------------------- | --------------------- | ------------------------------------------------------------------------ |
| `LAB_PORT`          | `8010`                | where the lab listens                                                    |
| `LAB_SERVICE_PORTS` | `8000,8002,8003,8004` | ports probed for checkpoint services (identified by their FastAPI title) |
| `LAB_HAIKU_MODEL`   | `claude-haiku-4-5`    | the model id behind **Claude Haiku** in the model picker                 |
| `LAB_SONNET_MODEL`  | `claude-sonnet-5`     | the model id behind **Claude Sonnet** in the model picker                |

**Model picker** (top bar): **GPT** (your Azure or OpenAI config from `.env`),
**Claude Haiku** or **Claude Sonnet** (need `ANTHROPIC_API_KEY`). With
`LLM_PROVIDER` set to gemini, openrouter, ollama or lmstudio
(see `LLM_PROVIDERS.md`), the first entry is that provider instead of GPT. The lab never
edits `.env`: it passes `LLM_PROVIDER` / `ANTHROPIC_MODEL` to the processes it
starts, and the checkpoints' `load_dotenv()` leaves variables that are already
set alone. Runners use the new model on the next run; services the lab started
are restarted on it. A service you started in a terminal keeps reading `.env`
(Setup flags it). On Claude, 03/04 still embed with the GPT provider (Anthropic
has no embeddings API), 04 plans on the same model, and checkpoint 00 stays on
GPT because `bot.py` only knows Azure and OpenAI.

The lab starts 02, 03 and 04 on ports 8002, 8003 and 8004, so Compare can
reach all three at once. A service you started yourself on port 8000 is found
too.

No new dependencies and no build step: `lab/static/` is plain HTML, CSS and
JavaScript.

## Teaching with it

The page is built for projecting:

- **Three steps, top to bottom.** 1 pick a ticket (left), 2 pick a
  checkpoint (five cards, each with a one-line summary and its status),
  3 run. The cost of the run is shown next to the button before you press it.
- **One headline per run.** The first thing on screen is a sentence a
  student can read aloud: _“Refund of $48.50 issued on order O-77201 (total
  $64.50), under the $50 auto-approve limit. Delivered 21 days ago, where the
  14-day and 30-day windows disagree: both sections were retrieved and the
  prompt says to escalate, but the model refunded anyway.”_ That last
  sentence appears only when it is true: both window sections retrieved, the
  plan refunds without escalating, and the order's `delivered_at` puts the
  ticket more than 14 and at most 30 days after delivery. Inside 14 days the
  two sections agree and the report says so; with no delivery date it asks
  which window the plan applied.
- **The pipeline.** For 02–04, the nodes light up live as the service runs
  them; a paused approval gate, a repair loop and a failure are visible at a
  glance. For 00 and 01 the attempts play the same role.
- **Evidence underneath.** A few panels say what mattered at this
  checkpoint (what the customer got, the plan, the order and the money, the
  policy retrieved, the guardrails). Raw material (events, log lines,
  persisted state, the full prompt, terminal commands) is collapsed.
- **Two toggles, both off by default.** **Teaching notes** adds a short
  explanation under every panel. **Code links** adds `file:line` chips that
  open the responsible code in VS Code; the lines are found by anchor text
  when the page asks, so they stay right while you edit. A link can open
  with either on: `http://localhost:8010/?notes=1&cites=1`.
- **Simple / Detailed.** Every report has a switch at the top. **Simple** is
  the same run in plain words for a non-technical audience: what happened,
  what the bot did step by step, what the customer got, the decision, and a
  safety checklist (was the order looked up, was the refund capped, did a
  person approve). No nodes, schemas or request ids. **Detailed** is the full
  report. The choice is remembered; `?simple=1` opens a link in Simple.
- **Approval gate.** On 02–04 you choose what happens when a run pauses:
  **ask me** shows Approve / Reject buttons on the page (the run really
  waits, in the service's database, until you click); auto-approve and
  auto-reject let Compare and Repeat run unattended. The approval card shows
  what the decision is about: what the customer asked for, the order total,
  what the plan would refund, whose order it is, why the gate stopped (the
  rule behind each reason), what the customer wrote, the AI's own reasoning,
  and what Approve and Reject will each do. All of it comes from the
  `approval.required` event; the final report repeats it.
- **Follow a request ID.** With 02, 03 or 04 picked, the Run view has an
  _or follow a request ID_ box: paste a `request_id` (or click one of the
  _recent_ ones this lab submitted, which survive an F5) and press **Follow**.
  The lab reads `GET /runs/{id}`, then replays `/runs/{id}/events` from
  event 0 and renders the run as if it had started here: status, pipeline,
  the approval card if the gate is still open, the reply, the report. Nothing
  is submitted. Use it after an F5, a lab restart, or for a run sent with curl.
- **The service went down.** If a service dies or restarts mid-run (crash
  after refund, Stop, the kill-switch restart), the run does not error: the
  headline turns orange, _service down · reconnecting_, and the lab retries
  for 60 seconds. Start the service again in Setup and the run continues from
  the last event it saw (`Last-Event-ID`). After 60 seconds it gives up, and
  the request id is waiting in the Follow box.
- **Your own ticket, with a sender.** _Write your own ticket_ has an optional
  **From** field. 02–04 receive it as `customer_name` next to `ticket_text`,
  so the order-ownership rule runs on free text; left empty, the key is not
  sent and the rule is skipped, as before. 00 and 01 ignore it. Works in Run,
  Compare and Repeat.
- **Search only (03).** With 03 picked, _or search the policy index only_
  runs checkpoint 03's own `retrieve.retrieve()` on a bare query (one
  embedding call, no triage, no planner) and shows the top sections with
  scores and their text. The 03 service holds the index while it runs, so the
  lab says so and offers **Stop checkpoint 03, then search**, then a button to
  start it again. The **only** picker restricts the search to one policy
  document through `retrieve()`'s own `doc_ids` filter, which Qdrant applies
  inside the search (Session 8, "filter first"). The report then also shows
  the unfiltered top-k with the foreign rows struck out: what dropping rows
  afterwards would have kept, and what had already left the store.
- **How close are these phrases? (03).** Two to four phrases through
  checkpoint 03's `llm.embed_texts` in one call, then every pair's cosine
  similarity, best pair first (Session 7, "close means pointing the same
  way"). No index is opened, so it works while the 03 service runs.
- **The map (03).** Every section of the policy index on two axes: the
  runner reads the stored vectors and keeps the two directions of largest
  variation (PCA with numpy, no new dependency), one colour per document.
  With a query, the lab embeds it once, places it on the same axes and rings
  its real top 6, found in the full space with the store's own search. The
  caption says how much variation the two axes keep: it is a sketch, not the
  search. Same lock rule as Search only.

### The views

**Run** — one checkpoint, one ticket, the full report.

**Compare** — the same ticket through several checkpoints at once, one row
per question you would ask in a postmortem: did anyone check the order, who
approved the refund, was it validated, is it grounded in policy, did the
guard trip, what did it cost. 00 answers most rows with “none”.

**Repeat** — one ticket N times on one checkpoint, with the outcomes diffed
(incident #8: _same ticket, different resolutions_). Three runs at a time;
you confirm the cost first.

**Incidents** — the ten-problem list from the Session 1 scoreboard. Each
card picks a ticket that tends to reproduce the incident and runs it on 00,
or on the checkpoint that retires it. A good way to open a session.

### Tickets worth projecting

| Ticket                      | Why                                                                                                                                                                                          |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Incident C (Session 1 demo) | 00 believes the customer and “issues” a $249.99 refund on a $24.99 Final Sale order; 02 looks the order up, sees an ask above the total and escalates it to a person instead of shrinking it |
| T-1001                      | the capstone headphones ticket: a replacement, so 02–04 pause at the approval gate                                                                                                           |
| T-1010, T-1026              | the planted policy contradiction: order O-77201 was delivered 21 days ago, between the two windows; 03 retrieves both and tends to refund anyway (README “Try this” #1)                      |
| T-1004                      | a $45 refund with no gate: the crash-after-refund demo (start 02 with the flag, run it, start 02 again plain)                                                                                |
| T-1006, T-1018, T-1025      | injection: 04 stops them at the input guard; 00 does whatever they say                                                                                                                       |
| T-1011, T-1022, T-1030      | no order reference: rule 1 forbids a refund                                                                                                                                                  |
| T-1017, T-1024, T-1031      | nine-message threads: watch the prompt size on 00                                                                                                                                            |

## What it costs

Every run is a real call to the deployment in `.env`. Each report shows its
cost, and the header keeps a running total for the page. Compare and Repeat
always open a confirmation dialog with an estimate before they spend
anything. Rough figures on `gpt-4.1-mini`:

| Run                                                                    | Cost            |
| ---------------------------------------------------------------------- | --------------- |
| checkpoint 00, one ticket (1–3 calls, ~2,400 prompt tokens each)       | ~$0.001–0.004   |
| checkpoint 01, one ticket (1–2 calls)                                  | ~$0.0002–0.0004 |
| checkpoints 02–04, one ticket (triage + plan + reply, up to 2 repairs) | ~$0.001–0.003   |
| Repeat 00 × 5                                                          | ~$0.02          |

Checkpoint 00 has no price table (that's one of its lessons), so its calls are
priced with checkpoint 01's `PRICE_TABLE`. Checkpoints 02–04 price their own
calls; the lab reports `cost_usd` from their persisted state.

## How it observes without changing anything

**00 and 01** run in a subprocess with cwd set to the checkpoint folder.
Each checkpoint has its own `llm.py`, `models.py`, `errors.py`, and so on,
all with the same module names, so two checkpoints are never imported into
one process. The runner reads one JSON request on stdin and streams NDJSON
events back; anything the checkpoint prints is captured and forwarded.

Every run gets a fresh process, but not a cold one. Importing the provider
SDKs takes seconds (2–3 s on a Windows laptop with Defender scanning each
file), so the lab keeps the next runner started: it has
imported only the third-party packages in its `PRELOAD` list and waits on
stdin. A run takes it and a replacement starts behind it (up to three while
Repeat or Compare has runs in flight). The checkpoint's own modules and
`.env` still load after the request arrives, so an edited checkpoint or a new
model pick takes effect on the next run. The headline's **startup** stat says
`pre-loaded`, or how long a cold start made the run wait; **wall time** never
includes it.

- `runners/cp00.py` imports `bot` unmodified and calls `bot.main()` with
  `sys.argv = ["bot.py", ticket]`. `bot.client` is swapped for a proxy that
  forwards every call to the real client (adding `stream_options` to read
  token counts), and `sys.settrace` records each exception the bare
  `except` swallows, the value `resolve()` returned, and whether the
  `refund issued!` line executed. `bot.py` is never edited.
- `runners/cp01.py` runs `main.main()` unmodified with `main.TICKETS_PATH`
  pointed at a temp file holding the one chosen ticket. Wrappers around
  `llm._client`, `llm.structured_completion`, `triage.classify_ticket` and
  `errors.user_message` record what happened; each one calls straight
  through. Fault injection (“truncate attempt 1”) only lowers `max_tokens`
  on the real provider call, so the truncation, the SDK error and the repair
  are real.

**02, 03 and 04** are services, and the lab is one more HTTP client of them,
exactly like the 02 console or curl:

1. `POST /tickets` with `ticket_id` (dataset) or `ticket_text` (demo or
   custom text, plus `customer_name` when the From field is filled) →
   `request_id`. **Follow** skips this step and starts from `GET /runs/{id}`.
2. `GET /runs/{id}/events`: the server-sent events, forwarded to the page as
   they arrive. If the stream goes quiet for a minute (a parked approval
   gate), the lab reconnects with `Last-Event-ID` and the service replays
   what was missed, which is the mechanism the checkpoint teaches. If the
   connection breaks (the service died or restarted), it keeps retrying for
   60 seconds and resumes the same way.
3. `POST /runs/{id}/approve` when the run pauses: from the Approve / Reject
   buttons, or automatically under the auto policies.
4. `GET /runs/{id}` at the end: the persisted `TicketResolutionState`.
5. The lines this run appended to that checkpoint's `logs/runs.jsonl`,
   filtered by `request_id`. Refunds, rule violations, node errors and
   guardrail trips are read from there.

The node list on the pipeline is read from each checkpoint's `nodes.NODES`
registry, not copied.

Lab runs write to the checkpoints' real logs and databases, so the grep
commands the lab shows work in the terminal afterwards.

**Secrets.** `/api/health` reports whether a key is _set_, never its value.
Anything a runner or a managed service prints is scrubbed of every `.env`
value whose name contains KEY, SECRET, TOKEN or PASSWORD before it reaches
the browser.

**One process per policy index.** Qdrant's local mode (`qdrant_data/`)
allows one process per folder. If an eval, a test or a second service has
checkpoint 03's or 04's index open, a run fails in `retrieve_policy` with
`INTERNAL_UNEXPECTED`; the report says so in plain words. Stop the other
process or wait for it, then run again. Search only (03) is the same rule from
the other side: it will not start while the 03 service is up, and says so.

`runners/cp03search.py` imports checkpoint 03's `retrieve` module unmodified
and calls `retrieve.retrieve(query)`, the function behind both the
`retrieve_policy` node and `py retrieve.py "<query>"`. Its other modes use
the same checkpoint code: `distance` calls `llm.embed_texts` once for the
phrases; `map` reads the vectors straight from the Qdrant collection, runs a
numpy PCA, and searches the query with the store's own call.

## Deliberately left to the terminal

The lab doesn't diff checkpoints, run `pytest`, or search `logs/runs.jsonl`
across runs. Those are the lesson, not friction (same rule as the 02 console).
Every report ends with **Do the same in the terminal**: copyable PowerShell
that does what the lab just did (`py bot.py '…'`, a `classify_ticket`
one-liner, `Invoke-RestMethod` against the service, `curl -N` on the event
stream, `Select-String` for this `request_id`, `py -m pytest -q`).

```powershell
git diff --no-index checkpoints/01-triage checkpoints/02-service
cd checkpoints\02-service; py -m pytest -q
Select-String -Path checkpoints\02-service\logs\runs.jsonl -SimpleMatch '<request_id>'
```

## HTTP contract

The UI is one client. Everything it does, `curl` can do:

| Endpoint                                                                                                           | Purpose                                                                                                                                                                                                           |
| ------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/health`                                                                                                  | LLM config (names only), mock API, services + start commands                                                                                                                                                      |
| `GET /api/tickets`                                                                                                 | dataset + Session 1 demos + checkpoint catalogue (with node lists)                                                                                                                                                |
| `GET /api/incidents`                                                                                               | the incident board                                                                                                                                                                                                |
| `POST /api/run` `{checkpoint, ticket_id \| incident \| text, customer_name?, inject_truncate?, approval?}`         | one run → NDJSON stream: `start`, then runner events (00/01) or `request_id`, `sse`, `approval.waiting`, `approval.decided`, `service.down`, `service.up` (02–04), then `trace`                                   |
| `POST /api/follow` `{checkpoint, request_id}`                                                                      | attach to an existing 02–04 run: the same stream, replayed from event 0                                                                                                                                           |
| `GET /api/follow/recent`                                                                                           | request ids this lab server submitted, newest first                                                                                                                                                               |
| `POST /api/search` `{query, k?, doc_ids?}`                                                                         | checkpoint 03's `retrieve()` on a bare query; `doc_ids` is its filter inside the search, and the reply then carries `unfiltered` too; `{ok: false, code: "service_running"}` while the 03 service holds the index |
| `POST /api/distance` `{phrases: [2..4]}`                                                                           | cosine similarity of every pair, through 03's `llm.embed_texts` (one call, no index)                                                                                                                              |
| `POST /api/map` `{query?}`                                                                                         | every section's vector on two PCA axes, plus the query's point and real top-k; same lock rule as search                                                                                                           |
| `GET /api/policy-docs`                                                                                             | the `doc_id`s in `shared/policies` (for the "only" picker)                                                                                                                                                        |
| `POST /api/runs/{run_id}/approve` `{approved}`                                                                     | answer a paused 02–04 run                                                                                                                                                                                         |
| `POST /api/estimate` `{checkpoints, runs, ticket…}`                                                                | cost range for the confirm dialog + the repeat-loop command                                                                                                                                                       |
| `GET /api/runs/{run_id}`                                                                                           | a finished trace (the UI's `#run/<id>` links; memory only, lost on restart)                                                                                                                                       |
| `GET /api/cites/{checkpoint}`                                                                                      | resolved `file:line` citations                                                                                                                                                                                    |
| `POST /api/model` `{choice: gpt \| haiku \| sonnet}`                                                               | switch the model; restarts the services the lab started                                                                                                                                                           |
| `POST /api/services/{02\|03\|04\|mock}/start` `{runtime?, chaos?, crash_after_refund?, kill_switch?, budget_usd?}` | start a service (02: `plain` or `langgraph`) with whitelisted flags; builds the policy index first if missing                                                                                                     |
| `GET /api/services/flags`                                                                                          | which flags each service offers (read from the code)                                                                                                                                                              |
| `POST /api/services/{name}/stop`                                                                                   | stop a service (one started in a terminal too, if the port serves the expected app)                                                                                                                               |
| `GET /api/services/{name}/log`                                                                                     | the last lines a managed service printed                                                                                                                                                                          |

```powershell
# One NDJSON line per event; the last one is the trace. Works in PowerShell 5.1 and 7.
$body = @{ checkpoint = "02"; ticket_id = "T-1001"; approval = "approve" } | ConvertTo-Json
Invoke-RestMethod -Method Post http://localhost:8010/api/run -ContentType application/json -Body $body
```

## Files

```
lab/
  server.py            FastAPI app: health, tickets, run streaming, services, enrichment, commands
  runners/labproto.py  runner <-> server wire protocol (NDJSON on the real stdout)
  runners/cp00.py      observes checkpoint 00
  runners/cp01.py      observes checkpoint 01 (+ PRICE_TABLE quotes for 00)
  runners/cp03search.py  checkpoint 03's index without the planner: search (with a doc filter), phrase distances, the map
  static/index.html    the page
  static/lab.css       styles (same palette as the 02 console and the slides)
  static/core.js       state, helpers, server calls, headline / stepper / panel building blocks
  static/setup.js      health, the Setup popover, service start/stop, the checkpoint cards
  static/picker.js     the ticket picker and the selected-ticket strip
  static/cp00.js       checkpoint 00 live view and report
  static/cp01.js       checkpoint 01 live view and report
  static/cpsvc.js      checkpoints 02–04 live view and report
  static/simple.js     the Simple view: the same run in plain words, and the Simple / Detailed switch
  static/views.js      Run, Compare, Repeat, Incidents; routing; boot
```
