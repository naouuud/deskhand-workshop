# Session 4 code walkthrough: how the Deskhand service works

This is the long version of Session 4, written to read next to the code. In class you
watched Deskhand run as a service: one saved record per run, a pause for a person, a crash
that carried on where it stopped, and a refresh that lost nothing. Here you follow one
ticket through every file that makes that happen, and then you break things on purpose.

Everything is in `checkpoints/02-service`. Have the files open as you read. Code is
referred to by function name (`graph.run`, `nodes.validate_plan`), not by line number,
so this stays right when the code changes.

Plan on about an hour to read it all, plus however long you spend on the experiments at
the end.

**Contents**

1. [The picture: what each file does](#1-the-picture-what-each-file-does)
2. [The state object](#2-the-state-object-modelspy)
3. [The runner: a dict and a while loop](#3-the-runner-a-dict-and-a-while-loop-graphpy)
4. [The workflow, node by node](#4-the-workflow-node-by-node-nodespy)
5. [The status state machine](#5-the-status-state-machine)
6. [Rules and the approval gate](#6-rules-and-the-approval-gate-rulespy)
7. [When things go wrong: errors, retries, repairs](#7-when-things-go-wrong-errors-retries-repairs)
8. [Saving after every node, and recovering from a crash](#8-saving-after-every-node-and-recovering-from-a-crash-storepy)
9. [The front door: the HTTP API](#9-the-front-door-the-http-api-apppy)
10. [The event stream](#10-the-event-stream-eventspy-and-apprun_events)
11. [One run, end to end: T-1001](#11-one-run-end-to-end-t-1001)
12. [Files you can skip for now](#12-files-you-can-skip-for-now)
13. [Experiments: the incidents, and things to try](#13-experiments-the-incidents-and-things-to-try)
14. [Check yourself](#14-check-yourself)

---

## 1. The picture: what each file does

Deskhand was a script up to now: you ran it, it printed an answer, it exited. A support
agent doesn't use a terminal, so today it became a **service**. It takes a ticket over
HTTP, answers at once with an ID, does the work in the background, and lets a screen
follow along.

```
 agent's screen (the lab, the console, or curl)
        |                         ^
        | POST /tickets           | events (SSE)
        v                         |
 +--------------------------------------------+
 | app.py        FastAPI: the HTTP front door |
 +--------------------------------------------+
        | starts a background thread
        v
 +--------------------------------------------+       +----------------------+
 | graph.py      the runner: a while loop     | ----> | store.py   runs.db   |
 |   calls one node at a time from nodes.py   |       |   runs   (the state) |
 |   saves the state after every node         |       |   events (the log    |
 +--------------------------------------------+       |   behind the stream) |
        |                |               |            +----------------------+
        v                v               v
     llm.py           rules.py       order API              obslog.py
  (the model)      (plain-code      (port 9000,          logs/runs.jsonl
                     checks)      lookups, refunds)     (one line per event)
```

| File | Its one job |
| --- | --- |
| [`app.py`](../checkpoints/02-service/app.py) | The HTTP API: five endpoints. Accepts tickets, serves the saved state and the event stream, takes approvals. |
| [`graph.py`](../checkpoints/02-service/graph.py) | The runner. A `while` loop that calls one node, saves the state, calls the next. About 170 lines, and it is the whole engine. |
| [`nodes.py`](../checkpoints/02-service/nodes.py) | The eleven nodes, the steps of the workflow. Each one does one job and returns the name of the next node. Also the two order-API tools. |
| [`models.py`](../checkpoints/02-service/models.py) | The shapes: `TriageResult`, `ResolutionPlan`, and `TicketResolutionState`, the state object that carries a run. |
| [`store.py`](../checkpoints/02-service/store.py) | SQLite. Two tables: one row per run (the state), and every event of every run. |
| [`events.py`](../checkpoints/02-service/events.py) | The seven event types a screen can receive, and `publish`, which saves an event before anyone sees it. |
| [`rules.py`](../checkpoints/02-service/rules.py) | Business rules in plain Python. The model proposes a plan; these `if` statements decide whether it is allowed and whether a person must sign it. |
| [`errors.py`](../checkpoints/02-service/errors.py) | The 13 error codes, and the safe sentence the customer sees for each one. |
| [`llm.py`](../checkpoints/02-service/llm.py) | Every model call goes through here: structured output, streaming, cost, and turning provider errors into our codes. |
| [`obslog.py`](../checkpoints/02-service/obslog.py) | Appends one JSON line per event to `logs/runs.jsonl`. This file is how you debug a run. |
| [`runtime.py`](../checkpoints/02-service/runtime.py) | Picks the runner. Today that's always `graph.py` (the default, `plain`). |

A good order to read them in: `models.py`, `graph.py`, `nodes.py`, `store.py`, `app.py`,
`events.py`, `rules.py`. You can read `llm.py` and `errors.py` whenever a node sends you
there.

The analogy from class: a restaurant order. The waiter (`app.py`) gives you a number
straight away, the kitchen (`graph.py` and `nodes.py`) cooks in the back, and the screen
above the counter (the event stream) shows "preparing", then "ready". Nobody stands at
the counter holding the line open.

---

## 2. The state object (`models.py`)

Open `models.py` and find `TicketResolutionState`. This one Pydantic object holds
**everything one run knows**. Every node gets it, reads what it needs, writes its result
into it, and hands it on. After every node, the whole object is saved to SQLite as JSON.

The rule that makes it work: **no globals**. If something isn't in the state, the next
node can't see it, and a restarted run won't have it either.

Read the fields in groups, in the order the run fills them:

| Group | Fields | Filled by |
| --- | --- | --- |
| Who and what | `request_id`, `ticket_id`, `ticket_text`, `customer_name` | `app.submit_ticket`, before the run starts |
| Cleaned input | `normalized_ticket` | `normalize_ticket` |
| What the model said | `triage` (a `TriageResult`) | `triage` |
| What the order system said | `order_data`, `customer_match` | `fetch_order_data` |
| The plan | `resolution_plan` (a `ResolutionPlan`) | `generate_resolution_plan`, `repair_plan` |
| Checking the plan | `validation_errors`, `repair_attempts` | `validate_plan`, `repair_plan` |
| The gate | `requires_approval`, `approval_reasons`, `approval_status` | `validate_plan`, `approval_gate`, `POST /approve` |
| The outputs | `customer_reply_draft`, `internal_note` | `draft_customer_reply`, `execute_actions`, `finalize` |
| Where the run is | `status` | several nodes, through `nodes._set_status` |
| When it fails | `internal_error_code`, `user_visible_error` | `graph.run`, `handle_failure` |
| What it cost | `cost_usd`, `node_latency_ms` | every LLM node, and `graph.run` |

Fields start empty (`None`, `[]`, `0`) and fill in as the nodes run. That's why a saved
copy tells you exactly how far a run got: if `resolution_plan` is set but
`customer_reply_draft` is `None`, the run died somewhere between planning and the reply.

Three details worth noticing:

- **`status` is a `Literal` with seven values.** That's our enum: Pydantic rejects any other
  value when the state is built or loaded from the database. (A plain assignment like
  `state.status = "done"` isn't checked, which is why the nodes go through `_set_status`.)
- **Two error fields, two readers.** `internal_error_code` is a fixed code for engineers
  (`REPAIR_EXHAUSTED`); `user_visible_error` is a calm sentence for the customer. Neither
  is ever the raw exception text.
- **`customer_reply_draft` and `internal_note` are separate.** Text for the customer and
  text for staff never share a field, so the model's internal reasoning can't leak into a
  reply by accident.

Now look at `ResolutionPlan`, the planner's output. Every field is typed, `ActionParams` has
`extra="forbid"` (an unknown key fails instead of being dropped), and `actions` can only be
one of five types. Schema validation guarantees the **shape** of a plan. Whether the plan
is **allowed** is a separate question, and `rules.py` answers it (section 6).

---

## 3. The runner: a dict and a while loop (`graph.py`)

This is the most important file in the checkpoint, and it's short. Here is `graph.run`,
simplified:

```python
node = "normalize_ticket"
while node is not None and node != nodes.PAUSE:
    emit("node.started", {"node": node})
    try:
        next_node = nodes.NODES[node](state, emit)   # one job; returns the next name
    except Exception as exc:
        state.internal_error_code = <the code from the exception, or INTERNAL_UNEXPECTED>
        next_node = "handle_failure"
    state.node_latency_ms[node] = <how long it took>
    store.save_state(state, next_node=...)           # the save after every node
    emit("node.completed", {"node": node, "latency_ms": ..., "cost_usd": ...})
    node = next_node
```

That's the whole runtime. Read it line by line in the real file and you'll see:

1. **`nodes.NODES` is a plain dict** from a name to a function (bottom of `nodes.py`). The
   runner looks a node up by the name the previous node returned. So a node's name is
   part of the contract: rename one and you must rename every `return` of it.
2. **Each node returns the next node's name.** The node's *code* chooses the edge, with
   ordinary `if` statements. The model fills in fields of the state; it never picks where
   the run goes next. That's what makes this a workflow and not an agent.
3. **Three ways to stop the loop.** A node returns `None` (the run is finished:
   `finalize` and `handle_failure` do this), or `nodes.PAUSE` (a person has to decide:
   `approval_gate` does this). A `PAUSE` ends the *thread* without ending the *run*. The
   run waits in the database until someone calls `POST /runs/{id}/approve`.
4. **Any exception goes to `handle_failure`.** If the exception is a `DeskhandError` it
   carries its own code (`TOOL_ORDER_API_TIMEOUT`, say); anything else is a bug and gets
   `INTERNAL_UNEXPECTED`. Either way the customer gets a safe sentence, never a stack
   trace. If `handle_failure` itself fails, the loop stops instead of calling it forever.
5. **The save happens after every node, before the next one starts.** It stores the whole
   state *and* the name of the next node. So if the process dies, it loses at most the
   node that was running at the time. Section 8 is all about this line.
6. **`node.completed` is sent after the save.** A screen never sees a node as done before
   its result is on disk.

Below `run` are four small functions that start it in different ways:

| Function | Who calls it | What it does |
| --- | --- | --- |
| `run_in_thread` | `POST /tickets` | Starts `run` on a background thread and returns at once. |
| `resume_after_approval` | `POST /runs/{id}/approve` | Approved: start again at `execute_actions`. Rejected: skip to `draft_customer_reply`. |
| `resume` | `resume_stuck_runs` | Start at the saved `next_node`. |
| `resume_stuck_runs` | `app.py` at startup | Finds every unfinished run in the database and resumes it. |

---

## 4. The workflow, node by node (`nodes.py`)

Here's the graph. Every arrow is a `return "..."` somewhere in `nodes.py`.

```mermaid
flowchart TD
    N[normalize_ticket] --> T[triage]
    T --> F[fetch_order_data]
    F --> G[generate_resolution_plan]
    G --> V[validate_plan]
    V -->|plan broke a rule| R[repair_plan]
    R --> V
    V -->|needs a person| A[approval_gate]
    V -->|no approval needed| E[execute_actions]
    A -.->|PAUSE, then approved| E
    A -.->|PAUSE, then rejected| D[draft_customer_reply]
    E --> D
    D --> Z[finalize]
    X[handle_failure]
    N & T & F & G & V & R & A & E & D -.->|any error| X
```

(If the diagram shows as text, read it as: normalize → triage → fetch order → plan →
validate; validate goes to repair and back, or to the gate, or straight to execute;
execute → reply → finalize. Any error from any node goes to `handle_failure`.)

Four kinds of node:

| Kind | Nodes |
| --- | --- |
| Calls the LLM | `triage`, `generate_resolution_plan`, `repair_plan`, `draft_customer_reply` |
| Calls the order API | `fetch_order_data` (read), `execute_actions` (write: refunds) |
| Plain code | `normalize_ticket`, `validate_plan`, `finalize`, `handle_failure` |
| Waits for a person | `approval_gate` |

Every node has the same signature: `def node(state, emit) -> str | None`. `state` is the
state object, and `emit` sends an event to the stream (section 10). Now the nodes one at a
time.

### `normalize_ticket`: clean the input before any model sees it

Sets the status to `running`, collapses runs of spaces and blank lines, and if the ticket
is over `MAX_TICKET_CHARS` (6,000 characters, about 1,500 tokens) keeps only the newest
part with an `[earlier messages truncated]` marker. Every later LLM call reads
`normalized_ticket`, so this node decides, in code, how much customer text each call pays
for. No model involved. Next: `triage`.

### `triage`: classify the ticket

One structured LLM call that fills a `TriageResult`: category (one of 6), priority,
sentiment, confidence. This is Session 2's triage, now as a node. If the answer is
malformed (cut off, refused, off-schema), it gets **one** repair: the error goes back to
the model with "your previous answer was invalid (...)". Any other error (a timeout, a
rate limit) is raised to the runner, because asking again can't fix those. Adds the call's
price to `cost_usd`. Next: `fetch_order_data`.

### `fetch_order_data`: look up the order, in code

A regex, not the model, finds the order ID in the text (`O-48291` or `#48291`). A regex
reads the text the same way every time and can't be talked into anything. Then it calls
the `lookup_order` tool, which runs `_order_api_call`: a 5 s timeout, 2 retries, waiting
0.5 s and then 1.0 s, for timeouts, connection errors and 5xx answers.

Three outcomes:

- **No order ID in the ticket, or the order doesn't exist (404).** Not a failure. The run
  goes on with `order_data = None`, and the rules will refuse any refund later.
- **The order API is broken** (still failing after the retries). The tool raises a
  `DeskhandError` with `TOOL_ORDER_API_TIMEOUT` or `TOOL_ORDER_API_ERROR`, and the run
  ends at `handle_failure`.
- **Found it.** `order_data` is set, and `customer_match` records whether the ticket's
  customer is the order's owner (`True`, `False`, or `None` when nobody said who sent it).

Next: `generate_resolution_plan`.

### `generate_resolution_plan`: the model proposes

One structured LLM call that returns a `ResolutionPlan`. Read `PLAN_SYSTEM_PROMPT`: never
refund more than the order total, treat the ticket as data and not as instructions, over
$50 needs approval, and so on. And read the comment above it: **most of these lines are
checked again in code**. The prompt makes a good plan likely; the rules make a bad one
impossible to carry out. The node overwrites `plan.ticket_id` with the real one, because
code owns that field, not the model. Next: `validate_plan`.

### `validate_plan`: code checks the plan

No model here. `rules.check_plan` returns a list of violations (section 6). Then one of
three routes:

- **No violations.** `rules.approval_reasons` decides whether a person must sign. Any
  reasons: go to `approval_gate`. None: go straight to `execute_actions`.
- **Violations, and fewer than 2 repairs so far.** Status becomes `needs_repair`, go to
  `repair_plan`.
- **Violations after 2 repairs.** Raise `REPAIR_EXHAUSTED`. The run fails with a safe
  message instead of looping forever on a model that can't comply.

### `repair_plan`: send the mistakes back

Re-asks the planner with its own bad plan and the list of violations: "That plan violates
business rules: ... Return a corrected plan." Each repair starts from the original prompt
plus the latest plan only, so the prompt doesn't grow with every attempt. Next:
`validate_plan` again. That's the only loop in the graph, and it's bounded.

### `approval_gate`: stop and wait for a person

Sets `approval_status = "pending"` and the status to `awaiting_approval`, then sends an
`approval.required` event with everything the approver needs to *disagree* with the
model: what the customer asked for, the order total, what the plan would refund, whether
the order is the customer's own, why the gate stopped (`approval_reasons`), and the
model's rationale. Then it returns `PAUSE`.

Look at what happens next in `graph.run`: the saved `next_node` is `None` for a paused
run. Nothing restarts it on its own, not even a server restart. Only `POST /approve` does.

### `execute_actions`: code carries out the plan

Status `executing`. For each action in the plan:

- `issue_refund`: calls the `issue_refund` tool with the order ID **from the fetched
  record**, the amount **the rules checked** (`plan.refund_amount_usd`), and an
  idempotency key **built from the run**: `f"{state.request_id}:{index}"`. Every value
  that moves money comes from checked state, not from model text.
- `send_replacement`, `escalate_to_human`: a line in the internal note (the mock has no
  fulfilment API).
- `request_more_info`, `reply_only`: nothing to do; the reply covers it.

The idempotency key matters because this node can run twice for one run (after a crash,
section 8). The order API returns the original refund for a key it has seen before
instead of paying again. Wednesday's session is about exactly this. Next:
`draft_customer_reply`.

### `draft_customer_reply`: the only text that streams

The one LLM call whose output is prose for a person, so it's the only one streamed word by
word, as `reply.delta` events. If a person rejected the plan, the prompt says so and tells
the model to promise nothing. Triage and the plan never stream: half a JSON plan on an
agent's screen is incident #3. Next: `finalize`.

### `finalize`: close the run, explicitly

Builds the internal note (triage, plan, approval, rationale, cost), sets the status to
`completed`, and sends `run.completed` with the reply and the plan. Returns `None`: the
loop ends. A screen knows the run is done because of this event, never because a
connection closed.

### `handle_failure`: the safe ending

The runner has already set `internal_error_code`. This node picks the customer's sentence
for that code from `errors.USER_MESSAGES`, sets the status to `failed`, and sends
`run.failed` with only that sentence. The exception text stays in the log. Returns
`None`.

---

## 5. The status state machine

`status` can only take seven values, and it only moves along these arrows:

```mermaid
stateDiagram-v2
    [*] --> queued: POST /tickets
    queued --> running: normalize_ticket
    running --> needs_repair: plan broke a rule
    running --> awaiting_approval: approval_gate
    running --> executing: no approval needed
    needs_repair --> awaiting_approval: repaired, needs a person
    needs_repair --> executing: repaired, no approval needed
    awaiting_approval --> executing: approved
    awaiting_approval --> completed: rejected (reply only, no refund)
    executing --> completed: finalize
    running --> failed: any error
    needs_repair --> failed: REPAIR_EXHAUSTED
    awaiting_approval --> failed: any error
    executing --> failed: any error
    completed --> [*]
    failed --> [*]
```

Two statuses are **terminal**: `completed` and `failed` (`TERMINAL_STATUSES` at the bottom
of `models.py`). Every run ends in one of them, written down. One status is **parked**:
`awaiting_approval`, which waits for a person and is never restarted by a crash recovery.
Everything else is **in progress**, and a crash recovery will pick it up.

A repaired plan doesn't go back to `running`: the status stays `needs_repair` through the
repair and the second check, and moves on only when the plan passes (to the gate or to
execution) or the repairs run out (to `failed`).

Two runs from class, traced through the arrows:

- **T-1001, Dana's $79.99 headphones:** `queued → running → awaiting_approval →
  executing → completed` (if you approve).
- **T-1004, Lena's $45 keyboard:** `queued → running → executing → completed`. Under $50,
  so no gate.

Where the status changes in code: `_set_status` in `nodes.py`. It sends a `run.status`
event only when the value really changes, so a screen sees each move once.

---

## 6. Rules and the approval gate (`rules.py`)

The planner **proposes**, the rules **decide**. The same constraints are in the prompt,
but a prompt is a request the model may ignore, and a ticket can argue with it (some of
the 60 tickets are prompt injections). An `if` statement gives the same answer every time
and can't be talked out of it.

`check_plan(plan, order_data, customer_name=...)` runs every rule and returns all the
violations at once, so one repair sees every problem:

| Rule | What it checks |
| --- | --- |
| 1 | A refund needs real order data, a refund amount, and must not exceed the order total. The order must be the customer's own. A request above the total is escalated, never quietly capped. |
| 2 | Refunds above $50 and any replacement need `requires_approval=true`. A refund different from what the customer asked for also needs it. |
| 3 | Confidence below 0.5 must include `escalate_to_human`. |
| 4 | `reply_only` can't be combined with anything, and refunds and replacements only fit certain categories. |

Notice that the violation strings are written for two readers: a person reading the log,
and the model, because `repair_plan` sends them back word for word. That's why several of
them say what the plan must do instead.

`approval_reasons(plan, customer_match)` runs only on a plan with no violations, and it,
not the model's `requires_approval` flag, decides whether the run stops at the gate. The
model's flag can add a stop, never remove one. Each reason is a sentence the approver
reads ("refund $79.99 is above the $50.00 auto-approve limit").

The limit is a constant, `AUTO_APPROVE_LIMIT_USD = 50.0`. The prompt says "$50.00" too, and
the comment next to the constant warns you to change both together. Experiment 13.6 shows
what happens when you don't.

---

## 7. When things go wrong: errors, retries, repairs

`errors.py` lists 13 codes in four groups (the LLM, the order API, plan checks, everything
else). A code tells the *caller* what to do, which is why there isn't one big `LLM_ERROR`.
The workflow handles each kind of failure differently:

| Kind of failure | What Deskhand does | Where |
| --- | --- | --- |
| Order API timeout, connection error, 5xx | **Retry**: it may work next time. 2 retries, 0.5 s then 1.0 s apart. | `nodes._order_api_call` |
| Order API 4xx (404, say) | **Don't retry**: it's an answer. A 404 means "no order". | `lookup_order` |
| Malformed LLM output | **Repair**: send the error back. Asking the same way repeats the mistake. Triage: 1 repair. | `nodes.triage` |
| Plan breaks a business rule | **Repair**, with the violations. Up to 2 repairs, then `REPAIR_EXHAUSTED`. | `validate_plan`, `repair_plan` |
| LLM rate limit or timeout | **Stop cleanly**: the SDK already retried. Code, `run.failed`, safe message. | `llm._guarded` |
| Anything else (a bug) | **Stop cleanly** with `INTERNAL_UNEXPECTED`. | `graph.run` |

Do the worst-case sum for the order API once: 3 attempts × 5 s + 0.5 s + 1.0 s = **16.5 s**,
then `TOOL_ORDER_API_TIMEOUT`. Experiment 13.5 lets you sit through it.

Every failed run ends the same way: `graph.run` records the code and logs a `node.error`
line with the real exception text, then `handle_failure` sends the customer only the
sentence from `USER_MESSAGES`. The engineer reads the log; the customer reads one calm
sentence.

---

## 8. Saving after every node, and recovering from a crash (`store.py`)

`runs.db` has two tables (`_SCHEMA` in `store.py`):

```
runs    request_id (key) | status | next_node | state_json | updated_at
events  request_id | seq | event | data_json | ts        (key: request_id + seq)
```

**`runs` holds one row per run, overwritten after every node.** `save_state` is an
UPSERT ("insert, or replace if it exists") of the whole state as JSON, plus the name of
the node to run next. It costs about 10 ms. Redoing an LLM node costs seconds and money,
and the model may decide differently the second time. That trade is the whole argument
for saving after every node.

**Recovery is one query.** When the service starts, `app.lifespan` calls
`graph.resume_stuck_runs`, which calls `store.find_resumable`:

```sql
SELECT state_json, next_node FROM runs
WHERE status NOT IN ('completed', 'failed', 'awaiting_approval')
```

Each row it finds is restarted at its `next_node` with its saved state. Finished nodes
don't run again. Paused runs are left alone: they're waiting for a person, not for a
restart. Each resumed run writes a `run.resumed` line to the log.

What a crash costs, then, is **at most the node that was running**. If the server dies
while `generate_resolution_plan` waits for the model, the restart runs
`generate_resolution_plan` again (one planner call paid twice) and nothing before it.

Two more things the save buys you:

- **Debugging.** `GET /runs/{id}` returns the saved row. It's exactly what a recovery
  would resume, so a failed run can be read instead of guessed at.
- **A safe re-run of `execute_actions`.** If the process dies *inside* `execute_actions`,
  after the refund but before the save, recovery runs that node again. The idempotency
  key `request_id:index` is the same the second time, so the order API returns the first
  refund instead of paying twice. That case is Wednesday's.

`store.py` opens a new connection for each call and turns on WAL mode, so the event stream
can read while a run thread writes. You don't need to change any of that; it's there so
several runs and several viewers don't block each other.

---

## 9. The front door: the HTTP API (`app.py`)

Five endpoints. The lab and the console at http://localhost:8000/console use only these,
so anything they do, `curl` can do.

| Endpoint | What it does |
| --- | --- |
| `GET /tickets` | The 60 tickets, for a picker. |
| `POST /tickets` | `{"ticket_id": "T-1001"}` or `{"ticket_text": "...", "customer_name": "..."}`. Returns `{"request_id": "..."}` at once. |
| `GET /runs/{id}` | The saved state right now, plus `next_node`. Safe to refresh any time. |
| `GET /runs/{id}/events` | The event stream (SSE). |
| `POST /runs/{id}/approve` | `{"approved": true}` or `false`. Resumes a paused run. 409 if the run isn't waiting. |

Read `submit_ticket` closely. In order, it:

1. validates the body (`TicketRequest`). A bad body is a 422 and costs no model call;
2. makes a 12-character `request_id`. It's the key in `runs.db`, in every log line, and
   in every refund's idempotency key;
3. builds the first state. For a dataset ticket, `customer_name` comes from
   `tickets.json`, never from text the model reads;
4. **saves the state before starting the thread**, with `next_node = "normalize_ticket"`,
   so `GET /runs/{id}` works immediately and a crash before the first node still leaves
   a row to recover;
5. starts the run in a background thread and returns.

That's why `POST /tickets` answers in milliseconds even though a run takes seconds. The
analogy from class was a CI build: you push, you get a build number at once, and you
watch the log page while the build runs.

`approve_run` checks the run is `awaiting_approval` (409 otherwise), records the decision
in `approval_status`, and calls `graph.resume_after_approval`. It returns *before* any
refund is made; the outcome arrives on the event stream.

---

## 10. The event stream (`events.py` and `app.run_events`)

A screen can learn about a run in two ways. **Polling**: call `GET /runs/{id}` again and
again. **A stream**: one `GET /runs/{id}/events` whose response doesn't end. The server
writes a few lines per event and keeps the connection open. That's Server-Sent Events
(SSE), and it only goes one way, server to screen.

An event on the wire looks like this:

```
id: 5
event: node.completed
data: {"node": "triage", "latency_ms": 812, "cost_usd": 0.00012, "cost_delta_usd": 0.00012}

```

The seven event types, from `events.py`:

| Event | Data | When |
| --- | --- | --- |
| `run.status` | `{status}` | The status changes |
| `node.started` | `{node}` | A node begins |
| `node.completed` | `{node, latency_ms, cost_usd, cost_delta_usd}` | A node ends and its state is saved |
| `approval.required` | the plan summary and the reasons | A person must decide |
| `reply.delta` | `{text}` | More words of the reply |
| `run.completed` | `{reply, plan}` | Done: the checked result |
| `run.failed` | `{user_visible_error}` | Done: the safe message only |

**The streaming contract**, the three rules that retire incident #3:

1. Stream progress, never half-finished machine output. Only the customer's reply
   streams word by word; half a sentence is still readable. The plan arrives once, whole
   and checked, in `run.completed`.
2. Say when a run ends, with an event. A closed connection never means "done".
3. A viewer who leaves loses nothing.

How rule 3 works: **every event is saved before it is sent**. `events.publish` calls
`store.append_event`, which gives the event the next number for that run (1, 2, 3, ...).
The stream endpoint, `app.run_events`, never receives events from the runner directly. It
reads the `events` table every 0.25 s and sends whatever is new. So a live viewer and a
viewer who comes back later read the same rows the same way.

When a browser reconnects, it sends `Last-Event-ID: 7` by itself, and the endpoint starts
from `seq > 7`. With `curl` you send the header yourself:

```
curl.exe -N -H "Last-Event-ID: 7" http://localhost:8000/runs/<request_id>/events
```

The run lives in a background thread and in SQLite, never in the connection. When a
viewer disconnects, the log records `stream.disconnected` and the run carries on. The
stream closes itself only after the run is `completed` or `failed` *and* a read found
nothing new, so the last event is always delivered first.

---

## 11. One run, end to end: T-1001

Now put it together. Dana Whitfield, order O-48291, $79.99 headphones with a cracked case.
Follow along in the files.

| # | What happens | Where | Saved `status` / `next_node` |
| --- | --- | --- | --- |
| 1 | `POST /tickets {"ticket_id": "T-1001"}`. The ticket is loaded from `tickets.json`, the first state is built and saved, a thread starts, `{"request_id": "3f9c1a2b7d4e"}` comes back. | `app.submit_ticket` | `queued` / `normalize_ticket` |
| 2 | The screen opens `GET /runs/3f9c1a2b7d4e/events`. | `app.run_events` | |
| 3 | Whitespace cleaned. Event `run.status: running`. | `normalize_ticket` | `running` / `triage` |
| 4 | Model call: `product_defect`, high priority, frustrated. | `triage` | `running` / `fetch_order_data` |
| 5 | Regex finds `#48291` → `O-48291`. `GET /orders/O-48291` returns $79.99, owner Dana Whitfield. `customer_match = True`. | `fetch_order_data` | `running` / `generate_resolution_plan` |
| 6 | Model call: a plan to refund $79.99, `requires_approval: true`. | `generate_resolution_plan` | `running` / `validate_plan` |
| 7 | 0 violations. Reason: "refund $79.99 is above the $50.00 auto-approve limit". | `validate_plan` | `running` / `approval_gate` |
| 8 | `run.status: awaiting_approval`, then `approval.required`. Returns `PAUSE`. The thread ends. The stream stays open. | `approval_gate` | `awaiting_approval` / *(none)* |
| 9 | Nothing happens until a person decides, through a restart if need be. | | |
| 10 | `POST /runs/3f9c1a2b7d4e/approve {"approved": true}`. A new thread starts at `execute_actions`. | `app.approve_run` | |
| 11 | `run.status: executing`. `POST /refunds` for $79.99 with `Idempotency-Key: 3f9c1a2b7d4e:0`. | `execute_actions` | `executing` / `draft_customer_reply` |
| 12 | Model call, streamed: `reply.delta` × many. | `draft_customer_reply` | `executing` / `finalize` |
| 13 | Internal note written. `run.status: completed`, `run.completed {reply, plan}`. | `finalize` | `completed` / *(none)* |
| 14 | The stream finds nothing new and the run is terminal, so it closes. | `app.run_events` | |

After every row from 3 on, there is a `node.started` before and a `node.completed` after.
Your plan's exact wording, confidence and maybe actions will differ from this table: the
model writes them. The **route** won't, because code chooses it.

Now trace T-1004 (Lena, $45) the same way on paper. Where does it leave the table above?
(Row 7: no reasons, so it goes straight to `execute_actions`.)

---

## 12. Files you can skip for now

`checkpoints/02-service` serves Sessions 4 to 6, so some files belong to later classes.
You can ignore them today:

- `graph_lg.py` and `DESKHAND_RUNTIME=langgraph`: the same nodes on a framework.
- `agent_demo.py`, `agent_bounded.py`: what happens when the model drives instead of the
  code.
- `console/`: an older browser client for the same API. The lab does more.
- In `nodes.py`, the `@tool` decorators and `TOOLS`: today, just read `lookup_order` and
  `issue_refund` as two functions.

---

## 13. Experiments: the incidents, and things to try

Every run spends a little real money on your API key, usually well under a cent. Set up
the way the handout does: the mock order API in terminal 1, Deskhand in terminal 2, from
the repo root:

```
# Terminal 1
cd shared\mock_order_api
py -m uvicorn app:app --port 9000

# Terminal 2
cd checkpoints\02-service
py -m uvicorn app:app --port 8000
```

A shortcut for terminal 3, so the experiments below stay short (PowerShell):

```powershell
function Submit($body) {
  $r = Invoke-RestMethod -Method Post http://localhost:8000/tickets `
         -ContentType application/json -Body $body
  $r.request_id
}
$id = Submit '{"ticket_id": "T-1004"}'
curl.exe -N http://localhost:8000/runs/$id/events      # watch it
Invoke-RestMethod http://localhost:8000/runs/$id        # read what was saved
```

Prefer buttons? `py lab\server.py` and http://localhost:8010 do most of these. **Setup**
starts the services and has the chaos switch, **Follow** reattaches to any request ID,
**Repeat** runs a ticket several times, **Compare** runs one ticket through several
checkpoints, and **Logs** shows `logs/runs.jsonl` live.

When an experiment asks you to edit code, undo the change afterwards: `git diff` shows
what you changed and `git checkout -- <file>` puts it back.

### The incident list, and where 02 stands

These are the ten production incidents from Session 1's scoreboard, with the experiments
below that let you see each one in 02.

| # | Incident | After Session 4 | Try |
| --- | --- | --- | --- |
| 1 | Sometimes it returns invalid resolution schemas. | Partial | 13.3 |
| 2 | Sometimes it sends customers a generic LLM apology instead of an answer. | Retired | 13.4 |
| 3 | Sometimes partial failures are streamed to the support agent's screen. | Retired | 13.2, 13.9 |
| 4 | Retries are not always effective. | | 13.3, 13.5 |
| 5 | The order-lookup API may fail, time out, or return inconsistent data. | | 13.5 |
| 6 | It once suggested a refund larger than the order total. | | 13.6 |
| 7 | Costs spike on long ticket threads. | | 13.7 |
| 8 | The same ticket can produce different resolutions. | | 13.8 |
| 9 | A crash after issuing a refund left the ticket unresolved, and a retry refunded twice. | | Wednesday |
| 10 | Nobody can debug why a specific run failed. | Partial | 13.1, 13.10 |

The blank cells are later sessions' to fill in. The experiments let you look at them now.

### 13.1 Read a run's whole story (incident #10)

Run T-1004 and let it finish. Then, from `checkpoints\02-service`:

```powershell
Select-String -Path logs\runs.jsonl -Pattern $id
```

Every line of that run, in order: `run.accepted`, each `node.started` and
`node.completed`, `order.fetched`, `refund.issued`, `run.completed`. Now open
`Invoke-RestMethod http://localhost:8000/runs/$id` and find `cost_usd` and
`node_latency_ms`. Which node was slowest? Which cost the most? (Hint: the slow ones are
the four that call the model.)

In the lab: **Logs**, click the request ID.

### 13.2 Pause, then come back (the state machine)

Run T-1001. When the stream stops at `approval.required`, **don't approve**. Instead:

1. Read the saved state: `status` is `awaiting_approval`, `next_node` is empty.
2. Stop Deskhand (Ctrl+C in terminal 2) and start it again.
3. Read the state again. Did the restart touch it? Search the log for `run.resumed`
   with this request ID. (There shouldn't be one: `find_resumable` skips paused runs.)
4. Reattach to the stream. It replays every event from 1 and then waits.
5. Approve it, and watch the rest arrive on the stream you just reattached.

Then try the API's edges: once the run has finished, approve it again (409, it's no
longer waiting), and approve a run that never paused, like a finished T-1004 (409 too).

### 13.3 Force a malformed answer, watch the repair (incidents #1 and #4)

In `nodes.triage`, change the **first** `llm.structured_completion(...)` call to:

```python
result, usage = llm.structured_completion(messages, TriageResult, max_output_tokens=10)
```

Restart Deskhand and run T-1004. Ten tokens can't hold a full `TriageResult`, so the model
is cut off. Search the log for `triage.repair`: the second call, which has the normal
limit and the error message, succeeds. That's the repair that v1 didn't have: v1 sent the
same prompt with the same limit three times and got the same broken answer three times.

Now put `max_output_tokens=10` on the **second** call too, and run again. Both fail, and
the run ends with `run.failed` and the generic safe message. Find the `node.error` line:
what's the code? (`LLM_MALFORMED_OUTPUT`.)

This works on GPT, Gemini and the local models. On Claude, `llm.py` raises small limits to
16,000 tokens, so nothing gets cut off.

### 13.4 An answer instead of an apology (incident #2)

Run T-1012 (Janet: "order O-72455 still shows as processing, I'm moving on Friday").
Read `order_data` in the saved state, then the reply in `run.completed`. Does the reply
use what the order system said? v1 had no way to look anything up and answered with
general shipping times.

In the lab, **Compare** T-1012 on 00 and 02 side by side.

Then T-1011 (Farah, no order number at all). `fetch_order_data` logs
`order.no_reference`, `order_data` stays `None`, and rule 1 forbids any refund. What does
the plan do instead?

### 13.5 Make the order API misbehave (incidents #4 and #5)

Stop the mock API and restart it in chaos mode. Try each one with T-1004:

```powershell
# Terminal 1
$env:MOCK_CHAOS = "flaky"          # then "error", "timeout", "inconsistent"
py -m uvicorn app:app --port 9000
# afterwards:
Remove-Item Env:MOCK_CHAOS
```

| Mode | What the mock does | Predict, then check |
| --- | --- | --- |
| `flaky` | Half the calls fail with 500, at random. | Watch terminal 1: some `GET /orders` lines repeat. Did the retries save the run? Run it 3 times. |
| `error` | Every call fails with 500. | How many `GET /orders` lines appear in terminal 1 for one run? Which error code ends it? |
| `timeout` | Every call hangs for 30 s. | How long until `run.failed`? Time it against the 16.5 s sum from section 7. |
| `inconsistent` | The order comes back without `total_usd`. | No crash: the run reaches `validate_plan`. Which rule complains? Does the repair help, when the data itself is missing? |

Also: stop the mock API completely and run T-1004. That's the handout's step 3: a
connection error, retried twice, then `TOOL_ORDER_API_ERROR` and one calm sentence for the
customer.

In the lab: **Setup**, stop the mock API, pick a chaos mode, start it again.

### 13.6 The $50 line, and who decides it (incident #6)

First, a prediction you can check without spending anything. Read `rules.check_plan` and
`rules.approval_reasons`, then decide which of these stop at the gate if the plan refunds
the full amount: T-1004 ($45.00), T-1048 ($50.00), T-1049 ($50.01), T-1001 ($79.99).
Then run the two in the middle.

Now change the limit in code only. In `rules.py`, set `AUTO_APPROVE_LIMIT_USD = 40.0`, but
leave `PLAN_SYSTEM_PROMPT` saying $50. Restart and run T-1004. The model, following the
prompt, plans a $45 refund with no approval. What does `validate_plan` do with that? Look
for `plan.invalid` in the log and count the repairs. Did the model accept the rule from the
violation message even though its prompt said otherwise? Change it back.

The refund-larger-than-the-total case (incident #6 itself) opens Wednesday's session.
Leave it for then if you'd rather vote on it fresh.

### 13.7 Long threads and the cost ceiling (incident #7)

Run T-1017 (a nine-message thread) and T-1057 (one 7,500-character message). For each, read
`normalized_ticket` in the saved state and `cost_usd`. Which one hit the
`[earlier messages truncated]` marker? What did it cut, the oldest or the newest text,
and why that way round? (`normalize_ticket` has the answer in a comment.)

Then lower `MAX_TICKET_CHARS` in `nodes.py` to 500, rerun T-1017, and compare `cost_usd`
and the plan. What did the model lose? Change it back.

### 13.8 Same ticket, different answers (incident #8)

Run T-1003 (Omar: "the lights don't really do the thing they're supposed to") three times.
Compare the three plans: the actions, the confidence, the rationale. Then do the same with
T-1004. Which one varies more, and why would an ambiguous ticket vary more than a clear
one?

Notice what *can't* vary: the route through the graph for a given plan, the $50 line, the
refund amount ceiling. Those are code. In the lab, **Repeat** runs a ticket 3 times and
lines the outcomes up.

### 13.9 Break the streaming contract on purpose (incident #3)

In `nodes.generate_resolution_plan`, just before `return "validate_plan"`, add:

```python
for i in range(0, len(plan.model_dump_json()), 20):
    emit(events.REPLY_DELTA, {"text": plan.model_dump_json()[i:i + 20]})
```

Run T-1004 and watch the stream. That's what v1 put on the support agent's screen: pieces
of machine output mid-generation. Is any single piece safe to act on? Remove the lines.

### 13.10 The refresh test, by hand

Add `time.sleep(10)` as the first line of `draft_customer_reply` and restart Deskhand.
Run T-1004 and stream it. During the pause:

1. Ctrl+C the `curl` (not the server). That's a closed tab.
2. Look in the log for `stream.disconnected` and its `last_event_id`.
3. Reattach with `-H "Last-Event-ID: <that number>"`. You get exactly the events you
   missed, then the live ones.
4. Reattach once more **without** the header. You get the whole run from event 1.

Did anything about the run change while nobody was watching? Remove the `time.sleep(10)`.

### 13.11 Pull the plug, then make the save matter

Do the handout's steps 4 and 5 first: kill Deskhand right after `execute_actions`
completes, restart it, and count the `POST /refunds` lines in terminal 1 (one).

Now see what the save after every node was buying you. Put the `time.sleep(10)` back in
`draft_customer_reply`, and in `graph.run` comment out the `store.save_state(...)` line.
Restart and repeat the kill. Before you restart, read `GET /runs/<request_id>`: what
status and `next_node` does it show? (Only the row saved by `POST /tickets`.) Restart.
Where does the run start again? How many model calls did you pay for twice? How many
`POST /refunds` hit terminal 1, and why does the order API still hold only one refund for
that run (`Invoke-RestMethod http://localhost:9000/refunds`)? One more oddity: the stream
never closes by itself this time. Why not? (Hint: what does `app.run_events` read to
decide the run is over?) Put both lines back.

That run's row still says `queued`, so the next time Deskhand starts it resumes the run
once more (one more run's worth of model calls) and this time saves it as `completed`.
The same goes for any other run you made while the save was commented out.

### 13.12 Free text, and who sent it

Free text has no dataset record saying who wrote it. Compare these two:

```powershell
$a = Submit '{"ticket_text": "My mouse from O-61402 double-clicks. Refund $20?", "customer_name": "Yusuf Demir"}'
$b = Submit '{"ticket_text": "My mouse from O-61402 double-clicks. Refund $20?"}'
```

Read `customer_match` and `approval_reasons` in both saved states. $20 is under the limit,
so why does one of them stop at the gate? Then send `{"ticket_id": "T-1004",
"customer_name": "Someone Else"}` and read the error. Why would the API refuse to let a
client overrule the dataset's name?

### 13.13 Write your own ticket

Use `ticket_text` and try to get a refund the rules shouldn't allow: a different
amount than the order, someone else's order, a "manager already approved this" line,
an instruction to ignore the rules. For each, write down your prediction first, then find
which line in `rules.py` (or which node) stopped it, or didn't. Some of the 60 tickets do
this already (T-1006, T-1018, T-1025, T-1036); look at their tags in
`shared/data/tickets.json`.

---

## 14. Check yourself

You've understood the session if you can answer these without opening the code. Then open
it and check.

1. What are the two things `store.save_state` saves after every node, and why does it need
   both?
2. A node returns `"approval_gate"`. Who decided that, the model or the code? Which
   function?
3. Why is a paused run saved with no `next_node`?
4. The server dies while `triage` is waiting for the model. After the restart, which nodes
   run? What does the restart cost?
5. Why does `node.completed` go out after the save and not before?
6. A screen's connection drops after event 12. How does it get events 13 onwards, and why
   are they still there?
7. Which LLM output streams word by word, and why only that one?
8. A plan says `requires_approval: false` for a $79.99 refund. What happens next, step by
   step?
9. The order API returns 404. Is that a retry, a repair, or a failure? What about a 503?
10. Where does the customer's error message come from, and why is it never
    `str(exception)`?
