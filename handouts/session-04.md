# Session 4, try this tonight: run the service, then crash it

You watched Deskhand run as a service: one saved record per run, a pause for a person on
Dana's $79.99 headphones, a server killed right after Lena's $45 refund that carried on
where it stopped, and a refresh that lost nothing. Here is the same thing with your own
hands on the terminals. About 45 minutes. Everything happens in `checkpoints/02-service`.
Prefer buttons? `py lab\server.py` and http://localhost:8010 run every step for you.

## 1. Bring it up, then read it (10 min)

Two terminals, both from the repo root:

```
# Terminal 1: the mock order API
cd shared\mock_order_api
py -m uvicorn app:app --port 9000

# Terminal 2: Deskhand
cd checkpoints\02-service
py -m uvicorn app:app --port 8000
```

Read `graph.py` first (about 170 lines, the whole runner), then `nodes.py`. Find the eleven
nodes and the line that saves the state after each one. Deskhand needs `LLM_PROVIDER` and
its keys in the `.env` at the repo root.

## 2. Submit T-1001, watch it, approve it (10 min)

```
# Terminal 3 (PowerShell): submit, then stream
$r = Invoke-RestMethod -Method Post http://localhost:8000/tickets `
       -ContentType application/json -Body '{"ticket_id": "T-1001"}'
curl.exe -N http://localhost:8000/runs/$($r.request_id)/events

# Terminal 4: approve it (paste the request_id)
$id = "<request_id>"
Invoke-RestMethod -Method Post http://localhost:8000/runs/$id/approve `
       -ContentType application/json -Body '{"approved": true}'
```

The stream waits at `approval.required` until you approve. Use `curl.exe`, not `curl`: in
Windows PowerShell `curl` doesn't stream. If the plan chose only `escalate_to_human`,
nothing needs approval and the run completes. That's not a bug.

## 3. Read the state, then make a run fail (10 min)

`Invoke-RestMethod http://localhost:8000/runs/$id` shows the saved state. Find `status`,
`cost_usd` and `node_latency_ms`: which node was slowest? Now stop the mock API (Ctrl+C in
terminal 1) and submit T-1004: step 2's two commands, with `"T-1004"`. The stream ends with
`run.failed` and one calm sentence. The engineer's view is in the log, from
`checkpoints\02-service`: `Get-Content logs\runs.jsonl -Tail 8` shows `node.error` with
`TOOL_ORDER_API_ERROR`. Start the mock API again.

## 4. Kill it mid-run (10 min)

In `nodes.py`, add `time.sleep(10)` as the first line of `draft_customer_reply`, and
restart Deskhand. Submit T-1004 and stream it. When `node.completed` for `execute_actions`
appears, press Ctrl+C in terminal 2. Then read what was saved, from
`checkpoints\02-service`:

```
py -c "import store; s, n = store.load_state('<request_id>'); print(s.status, n)"
```

Expect `executing draft_customer_reply`: the refund is done, the reply isn't.

## 5. Resume it and count the refunds (5 min)

Restart Deskhand. It picks the run up at `draft_customer_reply` on its own. Reattach without
the events you already saw:

```
curl.exe -N -H "Last-Event-ID: <last id>" http://localhost:8000/runs/<request_id>/events
```

Terminal 1 shows one `POST /refunds`, not two. Search `logs\runs.jsonl` for the request ID
and spot `run.resumed`. Then remove the `time.sleep(10)`.

## Before Wednesday

Bring the request ID of the run you killed. Wednesday crashes inside the refund step
itself, where the saved row can't help. More exercises: `checkpoints/02-service/README.md`
("Try this" 1 and 5).

Want the whole workflow explained file by file? `handouts/session-04-walkthrough.md`
follows one ticket through the code, node by node, and ends with experiments for each
incident on the scoreboard.
