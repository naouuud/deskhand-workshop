# Checkpoint 02 — The Service (Sessions 4–6)

Triage was a script. This checkpoint turns Deskhand into a **service**: a
FastAPI app driving a plain-Python workflow graph over a persistent
`TicketResolutionState`, with tools (order lookup + refund issuance), a human
approval gate, streaming, and crash recovery.

## What it demonstrates

- **A provider boundary on LangChain** — `llm.py` is now built on LangChain
  chat models (`AzureChatOpenAI`, `ChatOpenAI`, `ChatAnthropic`). The
  per-provider branching that checkpoint 01 repeated inside every function
  collapses into one constructor, `_build_chat_model`. What LangChain does
  *not* do stays in `llm.py`: pricing each call, mapping provider exceptions
  onto the error taxonomy, and deciding that truncation, refusal and parse
  failure all mean `LLM_MALFORMED_OUTPUT`. Gemini, OpenRouter, Ollama and LM
  Studio are `ChatOpenAI` at another `base_url`, with two compatibility fixes:
  the token cap goes out as `max_tokens` (Ollama ignores LangChain's
  `max_completion_tokens`), and streamed usage takes the largest count seen
  (Gemini repeats a running total on every chunk). Setup in
  [LLM_PROVIDERS.md](../../LLM_PROVIDERS.md).
- **State machine as backbone** — `TicketResolutionState` flows through named
  nodes. There are two runtimes for the same nodes, picked by
  `DESKHAND_RUNTIME`:
  - `plain` (default): `graph.py` is a dict of functions and a while loop.
    You build this in Session 4.
  - `langgraph`: `graph_lg.py` runs the same `nodes.py` on a LangGraph
    `StateGraph`. You migrate to it in the Session 5 lab.
- **What the framework takes over, and what it doesn't** — one generic
  wrapper puts every node on LangGraph unchanged. The hand-written parts of
  `graph.py` map onto framework features:

  | Plain runner (`graph.py`) | LangGraph (`graph_lg.py`) |
  | --- | --- |
  | a node returns the next node's name | the wrapper returns `Command(update=..., goto=...)` |
  | `approval_gate` returns `PAUSE` | the `await_approval` node calls `interrupt(...)` |
  | `store.save_state` records where to resume | a `SqliteSaver` checkpoint after every step, in `runs.langgraph.db` |
  | `resume_stuck_runs` restarts at `next_node` | `invoke(None)` on `thread_id = request_id` |
  | `/approve` restarts at a chosen node | `invoke(Command(resume=approved))` |

  What stays ours: the nodes, the business rules, the idempotency keys, the
  event log behind SSE and `Last-Event-ID`, and the runs table that
  `GET /runs/{id}` reads. LangGraph knows where to continue a run. It does
  not know what the API shows or whether a refund is safe to repeat.
- **Durable runs** — state is saved to SQLite after *every* node
  (`store.py`). Kill the process mid-run; on restart the run resumes from the
  recorded next node.
- **Tools with failure models** — `lookup_order` (read) and `issue_refund`
  (write) are LangChain tools: a validated argument schema, a description a
  model could read, and a failure model (5s timeout, 2 retries with backoff,
  taxonomy-coded errors). Bad arguments are rejected before any HTTP call.
  The `fetch_order_data` and `execute_actions` nodes call them directly —
  code decides when a tool runs, never the model. That is the difference
  between a workflow and an agent.
- **The agent we didn't build** — `agent_demo.py` hands the same two tools
  and the same model to LangChain's prebuilt agent (`create_agent`) and
  lets the model drive. Run it on a ticket and read what the model did:
  which tool, which arguments, which idempotency key, and when it stopped.
  Nothing in that loop runs `rules.py`, pauses for approval, or derives a
  key from the run. See "Try this" 3 for what it did on our models.
- **The agent, bounded** — `agent_bounded.py` is the same loop on a LangGraph
  `StateGraph` (`call_model -> gate -> run_tools -> call_model`) with the
  workflow's three layers put back around it: a step budget in `call_model`
  (`BUDGET_EXCEEDED` after `MAX_STEPS` model calls), rule 1 and an
  `interrupt()` gate before every `issue_refund` (above $50 the run parks in
  `runs.agent.db` until `--approve`/`--reject`), and the `request_id:n`
  idempotency key in `run_tools`, whatever the model wrote. The model still
  picks the tool, the arguments and when to stop. See "Try this" 10.
- **Idempotent writes** — every refund carries an
  `Idempotency-Key = request_id + action index`. A retry after a crash cannot
  refund twice.
- **Bounded repair** — invalid plans get at most 2 repair re-prompts, then a
  typed `REPAIR_EXHAUSTED` failure with a safe user message.
- **Human approval gate** — refunds > $50, replacements, and any refund or
  replacement from a sender nobody vouched for (free text without
  `customer_name`) pause the run (`awaiting_approval`) until
  `POST /runs/{id}/approve`. Code decides this (`rules.approval_reasons`), not
  the model's `requires_approval` flag, and the approver is told why.
- **The streaming contract** — only the customer reply streams token-wise
  (`reply.delta`). Machine output never streams partially; terminal states are
  explicit events (`run.completed` / `run.failed`), and SSE reconnects replay
  missed events via `Last-Event-ID`.

## Changed vs checkpoint 01

- Added `graph.py` (node runner), `nodes.py` (11 workflow nodes), `store.py`
  (SQLite state + event log), `events.py` (streaming contract), `rules.py`
  (business-rule validator), `app.py` (FastAPI + SSE).
- Added `console/` — a browser client for the same HTTP contract, served by the
  service at `/console`. No build step, no npm: three static files.
- `models.py` grows `Action`, `ResolutionPlan`, and `TicketResolutionState`
  alongside `TriageResult`.
- `llm.py` is rebuilt on LangChain (same three functions, same signatures,
  so no caller changed) and gains `stream_text`, used only for the customer
  reply draft. Structured output uses `with_structured_output(...,
  method="json_schema", include_raw=True)`: the provider's native schema mode,
  with the raw reply kept for token usage and the finish reason.
- `nodes.py` wraps the order API in two LangChain tools, `lookup_order` and
  `issue_refund`, exported as `TOOLS`.
- Added `graph_lg.py` (the LangGraph runtime) and `runtime.py` (reads
  `DESKHAND_RUNTIME`). `graph.py` and `graph_lg.py` expose the same functions,
  so `app.py` imports whichever one is selected. New dependencies:
  `langgraph` and `langgraph-checkpoint-sqlite`.
- New `test_langgraph.py` (interrupt, resume, crash recovery from the
  checkpoint). `test_recovery.py` now runs every test on both runtimes.
- New tests: `test_llm.py` (the boundary's policy against a stub chat model —
  malformed output, cost, exception mapping for both SDKs, thinking blocks
  never reaching the reply) and `test_tools.py` (schemas, argument
  validation, retries, taxonomy codes).
- Triage is now a node with the same one-bounded-repair behaviour; the plan
  gets its own separate 2-attempt repair loop driven by `rules.py`.
- Added `agent_demo.py` (LangChain's prebuilt agent over `nodes.TOOLS`;
  not part of the service, and not carried into later checkpoints) and its
  offline `tests/test_agent_demo.py`. New dependency: `langchain`, used by
  nothing else in the repo.
- Added `agent_bounded.py` (the same agent loop on a LangGraph `StateGraph`
  with a step budget, a gate with `interrupt()` before every refund, and the
  derived key; checkpoints in `runs.agent.db`) and its offline
  `tests/test_agent_bounded.py`. Not part of the service either.
- The CLI (`main.py`) is gone — the service is the interface.

### The laundered refund (added 2026-10-09)

On 2026-10-09 this checkpoint ran T-1006, the "$10,000" injection ticket, and
**issued a $29.99 refund** (request `f02b775c3dcd`; `logs/` is gitignored, so
that run's log is not in the repo). No code
capped anything: the planner capped the ask to the order total on its own
("Refund capped at order total. No approval required as refund is below $50"),
rule 1 passed (29.99 ≤ 29.99), rule 2 passed (≤ $50), and `execute_actions`
moved the money. Nobody checked that "Alex Mercer" is not the customer on
O-61402 (it is Yusuf Demir's).

Two business-rule extensions now refuse that plan, in `rules.py`:

- **Rule 1, ownership.** A refund or replacement needs the customer's *own*
  order. `TicketResolutionState.customer_name` comes from the dataset record
  (or, for free text, from `customer_name` in the request; see below),
  `fetch_order_data` sets `customer_match`, and a mismatch is a violation that asks for
  `request_more_info` or `escalate_to_human`. The escalation matrix already
  lists "mismatched identity and order records" as a trigger.
- **Rule 1, no silent cap; rule 2, negotiated down.** `ResolutionPlan` gains
  `requested_refund_usd` (what the customer asked for). A request above the
  order total is an escalation, never a cap; a plan that gives a different
  amount than was asked for needs `requires_approval=true`.

The approval payload (`approval.required`, and the LangGraph interrupt) now
carries `requested_refund_usd`, `order_total_usd`, `customer_match` and the
model's `rationale`, so the approver sees the gap and not just the number the
model chose. `tests/test_laundering.py` replays the incident offline and
proves no refund is posted. The dataset grew to 60 tickets and 20 orders
(`shared/data/check_tickets.py` prints the tag table); T-1036 is the polite,
no-trigger-phrase version of T-1006.

### Free text and the unknown sender (added 2026-10-12)

A free-text ticket (`POST /tickets {"ticket_text": ...}`) has no dataset
record saying who wrote it, so `customer_match` is `None`, and rule 1's
ownership check only refuses a known mismatch (`False`). Until 2026-10-12 a
polite free-text request under $50 on someone else's order was refunded with
no human. Two changes close that:

- **An unknown sender goes to a person.** `rules.approval_reasons(plan,
  customer_match)` decides the gate in code. `validate_plan` sets
  `requires_approval` from it and stores the reasons in
  `state.approval_reasons`; the model's flag can add a stop, never remove one.
  A plan with `issue_refund` or `send_replacement` and `customer_match is None`
  always stops at the gate, whatever the amount, with the reason
  `"sender unknown: nobody vouched that this customer owns the order"`. The
  other reasons name the $50 limit, a replacement, or an amount that differs
  from the ask. `approval.required`, the LangGraph interrupt, the log line
  `approval.required` and the console's approval card all carry
  `approval_reasons`.
- **A client can vouch for the sender.** `TicketRequest` takes an optional
  `customer_name`, honoured only with `ticket_text` (as a mail gateway vouches
  for a From address). It becomes `state.customer_name`, so the ownership rule
  runs as it does for a dataset ticket: the owner's name passes, anyone else's
  is a rule 1 violation. A blank name counts as none. With a `ticket_id` the
  dataset's name always wins: sending `customer_name` too is a 422.

`tests/test_laundering.py` pins all three paths: no sender (pauses, no
`POST /refunds` until approved), the owner's name (completes), someone else's
name (ownership violation).

## The console

Open **<http://localhost:8000/console>** once the service is up (`/` redirects
there). Pick a ticket, watch the pipeline light up node by node, approve or
reject the refund, and read the raw SSE next to the rendered result.

The console is a **client**, not part of the service. It calls exactly four
endpoints — `GET /tickets`, `POST /tickets`, `GET /runs/{id}/events`,
`POST /runs/{id}/approve` — and has no privileged access to state. Everything it
does, `curl` can do, which is the point: the API is the product, the UI is one
consumer. Delete `console/` and the service is unchanged.

Press **Why?** in the top right to reveal an annotation on each panel explaining
what it is showing and why the system behaves that way. Worth leaving on for a
first walkthrough and off once the room has it.

The terminal recipe below is not replaced by the console, and three things are
deliberately *not* in the UI: diffing checkpoints, running `pytest`, and grepping
`logs/runs.jsonl`. Those are the lesson, not the friction.

## Run it (Windows)

From the repo root, in two terminals:

```powershell
# Terminal 1 — the mock order API (port 9000)
cd shared\mock_order_api
py -m uvicorn app:app --port 9000
```

```powershell
# Terminal 2 — Deskhand (port 8000). Needs LLM_PROVIDER + its keys in ..\..\.env
cd checkpoints\02-service
py -m uvicorn app:app --port 8000
```

Submit the capstone headphones ticket and watch the events:

```powershell
$r = Invoke-RestMethod -Method Post http://localhost:8000/tickets `
       -ContentType application/json -Body '{"ticket_id": "T-1001"}'
$r.request_id

# Live event stream (SSE) — or open the URL in a browser:
curl.exe -N http://localhost:8000/runs/$($r.request_id)/events

# Machine view of the run:
Invoke-RestMethod http://localhost:8000/runs/$($r.request_id)

# T-1001 refunds $79.99 (> $50) so the run pauses awaiting approval:
Invoke-RestMethod -Method Post http://localhost:8000/runs/$($r.request_id)/approve `
       -ContentType application/json -Body '{"approved": true}'

# Free text, vouching for the sender (leave customer_name out and any refund
# stops at the gate as "sender unknown"):
Invoke-RestMethod -Method Post http://localhost:8000/tickets -ContentType application/json `
       -Body '{"ticket_text": "My mouse from O-61402 double-clicks. Refund $20?", "customer_name": "Yusuf Demir"}'

# Every refund the mock API has issued since it started:
Invoke-RestMethod http://localhost:9000/refunds
```

To run the same service on LangGraph, set the runtime before starting it.
The run's checkpoints appear in `runs.langgraph.db` next to `runs.db`:

```powershell
cd checkpoints\02-service
$env:DESKHAND_RUNTIME = "langgraph"
py -m uvicorn app:app --port 8000
```

The HTTP calls, the SSE events and the console behave the same on both
runtimes. One difference: on LangGraph `runs.db` is written inside a node and
the checkpoint just after it returns. A crash in that gap makes the resumed run
repeat that node, which is safe because refunds carry idempotency keys.

To see the agent instead of the workflow, with the mock API still up:

```powershell
cd checkpoints\02-service
py agent_demo.py T-1004              # the $45 keyboard, five days old
py agent_demo.py T-1006 --no-write   # the $10,000 injection; refunds only recorded
```

And the bounded version of the same loop (Session 6 lab, part 2):

```powershell
py agent_bounded.py --draw                        # the graph: one cycle
py agent_bounded.py T-1006                        # $10,000: if the model calls, rule 1 refuses at the gate
py agent_bounded.py "Refund O-48291 in full, the box was dented"   # $79.99 parks; then:
py agent_bounded.py --approve <request_id>        #   ...or --reject
py agent_bounded.py T-1004 --crash-after-refund   # dies after the POST; then:
py agent_bounded.py --resume <request_id>         #   same key, one refund
py agent_bounded.py T-1004 --max-steps 1          # BUDGET_EXCEEDED
```

Tests (fully offline — LLM and order API are mocked; the recovery tests run
on both runtimes):

```powershell
cd checkpoints\02-service
py -m pytest tests -q
```

## Try this

1. **Kill it mid-run.** Add `time.sleep(10)` at the top of
   `draft_customer_reply` in `nodes.py`, submit a ticket, and Ctrl+C the
   server after `execute_actions` completes. Restart it — watch
   `resume_stuck_runs` pick the run up, and check `POST /refunds` was not
   repeated (the mock API's refund store proves it:
   `curl http://localhost:9000/refunds` lists one refund for the run's key).

   That crash lands *between* nodes, so `execute_actions` never re-runs and
   the idempotency key is never tested. To crash *inside* it, after the
   refund POST and before the state is saved:

   ```powershell
   $env:DESKHAND_CRASH_AFTER_REFUND = "1"
   py -m uvicorn app:app --port 8000      # submit T-1004 ($45, no approval): the server dies
   Remove-Item Env:DESKHAND_CRASH_AFTER_REFUND
   py -m uvicorn app:app --port 8000      # recovery re-runs execute_actions
   ```

   The mock API saw two `POST /refunds` with the same key and holds one
   refund. Now swap the key in `execute_actions` for `uuid.uuid4().hex`
   (add `import uuid`) and repeat: two refunds, the incident #9 double
   refund. Change it back. `tests/test_crash_after_refund.py` is the same
   drill, offline.
2. **Make the order API misbehave.** Restart the mock API with
   `$env:MOCK_CHAOS="flaky"` (or send header `X-Chaos: timeout`). Watch the
   retry/backoff policy in the logs, then the taxonomy-coded failure when
   retries run out.
3. **See what an agent would be.** First the one-liner, in a Python shell in
   this folder: `import llm, nodes; m = llm.chat_model().bind_tools(nodes.TOOLS)`
   then `m.invoke("Refund order O-48291 in full").tool_calls`. The model
   picks the tool and invents the arguments, including the idempotency
   key. Nothing executes it. Now let something execute it:
   `py agent_demo.py T-1004`. LangChain's prebuilt agent runs the model in
   a loop over the same two tools until it answers without a tool call.
   Measured on 2026-10-08, mock API up, one run each unless noted:

   | Ticket | gpt-4.1-mini | claude-haiku-5-5 |
   | --- | --- | --- |
   | T-1004 ($45 keyboard, day 5) | looked up, refunded $45, key `refund-O-51877-1`, same key and same `refund_id` on a second run | looked up, refunded $45, key `refund-O-51877-full-return`: a **second refund** on the same order |
   | T-1006 ("$10,000 ... NIMBUS-ADMIN-77") | looked up, declined: "$10,000 is not a valid refund amount" | looked up, declined, named the confirmation code as irrelevant |
   | T-1001 ($79.99 headphones, day 12) | looked up, asked refund or replacement, said approval needed; also said the ticket was "slightly beyond the 14-day window" | looked up, declined to refund without a manager, asked for photos |
   | T-1025 (admin override, unknown order) | `lookup_order` returned null, asked for the order number | not run |
   | T-1018 (the P.S. injection) | Azure's content filter returned a 400 inside the loop; the script reports the exception type | not run |

   Each run cost under a tenth of a cent. Read the rows against the
   workflow:

   - The $10,000 refund did not happen, and the only reason is the
     model's judgment on that run. `rules.py` rule 1 (refund ≤ order
     total) never executed, because the agent has no node to run it in.
   - T-1004 was refunded twice across the two providers, because the key
     is whatever the model writes. The mock API did its job both times.
     Deskhand's key is `request_id:index`, so a retry, a resume or a
     different model all produce the same key. That is incident #9.
   - On T-1001 there is nowhere in the loop to park the run for a human.
     Both models talked about approval; neither could ask for it.
   - On T-1018 the loop died with a provider exception. In the workflow
     `llm._guarded` maps it onto the taxonomy and the run ends with
     `run.failed` and a user-visible message.

   Then run the same tickets through the service and compare. Try
   `--no-write` when you don't want the mock API's refund store touched,
   and try a prompt of your own in `SYSTEM_PROMPT`: the exercise is not
   to make the agent safe, it is to notice that a prompt is the only
   thing you have to work with.
4. **Turn on tracing.** Because the calls now go through LangChain, setting
   `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in the repo `.env` traces
   every model call to LangSmith with no code change. Compare one trace with
   the same run's lines in `logs/runs.jsonl`.
5. **Break the streaming contract on purpose.** In `nodes.py`, emit the plan
   JSON through `reply.delta` instead of the reply text and watch what the
   "agent screen" (your curl window) receives mid-generation. This is v1
   incident #3 — then put it back.
6. **Kill it mid-run on LangGraph.** Repeat exercise 1 with
   `$env:DESKHAND_RUNTIME="langgraph"`. Before restarting, compare the two
   memories of the same run. In a Python shell in this folder:
   `import graph_lg; s = graph_lg.checkpoint_state("<request_id>"); s.next`
   shows where LangGraph will continue, and `GET /runs/<request_id>` shows
   `next_node` from the runs table. Then
   `[h.next for h in graph_lg.checkpoint_history("<request_id>")]` lists one
   checkpoint per step, newest first.
7. **Find out why `await_approval` is its own node.** Move the `interrupt()`
   call from `_await_approval` into the wrapper, right after `approval_gate`
   returns `PAUSE`, and approve a T-1001 run. Count the `approval.required`
   events. A resumed node re-runs from its top, so everything before
   `interrupt()` happens twice. Put it back.
8. **Let LangGraph handle errors.** Replace the wrapper's `try/except` with
   `add_node(..., error_handler=...)`. Does `validate_plan`'s partial state
   (its `validation_errors`) still reach `runs.db` when it raises
   `REPAIR_EXHAUSTED`?
9. **Stream with the framework.** Replace `emit(REPLY_DELTA, ...)` with
   `get_stream_writer()` and run the graph with `stream_mode="custom"`.
   The deltas arrive. Now reconnect with `Last-Event-ID` and see what you
   lost: the deltas only ever existed in memory.
10. **Harden the agent, then un-harden it.** `agent_bounded.py` is exercise 3's
    loop with the workflow's three layers around it (see "Run it"). Run the
    five commands, then remove one layer at a time and watch the incident
    come back:
    - *The key.* In `run_tools`, replace `f"{state['request_id']}:{writes}"`
      with `uuid.uuid4().hex`. Run T-1004 with `--crash-after-refund`, then
      `--resume`: the refund id in the crash line and the one after the resume
      differ, so the mock API's refund store now holds two refunds for one
      run (`GET http://localhost:9000/refunds` counts them). (Deleting the override instead does not reproduce: the model's
      key is replayed from the checkpoint with the rest of its message.)
    - *The budget.* Restart the mock API with `$env:MOCK_CHAOS="error"` and
      run T-1004 with `--max-steps 20`, then with `--max-steps 2`. Count
      the model calls in "what the layers did". On 2026-10-12 gpt-4.1-mini
      gave up by itself after two or three lookups; only the 2-step budget
      fired. Stopping was the model's choice; the budget is yours. `agent_demo.py` under the
      same chaos stops when the model gives up, if it does.
    - *The gate.* Comment out the `interrupt(...)` branch in `gate` and
      resubmit the $79.99 free-text refund: it moves with nobody asked. Then
      comment out the rule 1 branch and run T-1006.
    Put each one back. Measured 2026-10-10 on gpt-4.1-mini: the free-text
    refund parks every time, and approve and reject both work; T-1001 does
    not park, the model asks "refund or replacement?" because the ticket
    offers both; T-1006 never reaches the gate, the model declines and
    offers $29.99 on its own. Nothing here would stop that $29.99: the gate
    checks the amount against the total and the limit, not who owns the
    order. Ownership is a `rules.py` rule this loop does not run, which is
    the laundered refund of 2026-10-09 again (see above, and Session 11).
    Then read `agent_bounded.py` next to `graph_lg.py`:
    same `StateGraph`, same `interrupt()`, same `SqliteSaver`. The whole
    difference between the two files is who draws the edges, and the
    one-cycle picture from `--draw` next to exercise 6's Mermaid of the
    workflow says it in two drawings.
