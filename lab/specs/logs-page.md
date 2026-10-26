# spec: a Logs page for Deskhand Lab

Status: **built** (Oct 23, 2026), on the recommended answer to each open decision below
(they were not confirmed before the build). Not built: **Load older** (decision 6). The
chart's clicked bucket is a page-only filter (shown as a chip), not part of the hash.

## Why

A production agentic app is understood through its logs. Today the lab shows the log
lines of **one run**, after it ends, inside its report. Nobody can see the system as a
whole: runs from the lab, a terminal, curl and evals interleaving in one file, a guard
tripping three times in a row, a service restarting and resuming its runs.

The Logs page is that view, built like the log explorers students will use at work
(Datadog Log Explorer, Grafana Loki, the trace lists in Langfuse or LangSmith): a live
tail, levels, services, filters, facets, and a pivot from any line to its whole run.

Every line on the page is a real line from a checkpoint's `logs/runs.jsonl`. Nothing is
generated, sampled or invented.

## What the logs hold today

Four files, one JSON object per line, written by each checkpoint's `obslog.log_event`:
`{ts, request_id, event, ...fields}`. `ts` is UTC ISO-8601. There is **no level and no
message field**.

| Source | Events seen in the current files |
| --- | --- |
| `01-triage` | `run.started`, `triage.completed` (the code also writes `triage.repair` and `triage.failed`) |
| `02-service` | `run.accepted`, `run.status`, `node.started`, `node.completed`, `node.error`, `order.fetched`, `plan.invalid`, `approval.required`, `approval.decided`, `refund.issued`, `run.completed`, `run.failed`, `service.runtime` |
| `03-rag` | 02's events plus `policy.retrieved` |
| `04-production` | 03's events plus `guardrail.input_tripped`, `order.not_found`, `order.no_reference`, `triage.repair`, `run.resumed`, `recovery.resumed_runs` |

Special request ids: `startup` (service start-up lines) and `eval-…` (eval runs, which
never write a terminal event). `reply.delta` is never logged (`events.py` skips it).

## What it does

### Page

- **R1. A fifth tab, Logs**, after Incidents, at `#logs`. It works with any checkpoint
  picked and never starts a run, a service or a model call. Opening it costs nothing.
- **R2. One merged stream** of all four `runs.jsonl` files, sorted by `ts`, **newest
  first**. Each row shows:
  - time: local `HH:MM:SS.mmm`, with the full UTC `ts` in a tooltip
  - level badge (R5)
  - service: `01-triage`, `02-service`, `03-rag` or `04-production`
  - request id: the first 8 characters, the full id in a tooltip; `startup` and `eval-…`
    get a small `system` / `eval` tag
  - event name, in monospace
  - message: a one-line summary built from the fields (R6)
- **R3. Expand a row** to see the whole line as pretty-printed JSON, with a Copy button.
  Long fields (`plan`, `reply`, `rationale`, `reason`) are cut to one line in the row and
  shown in full only here.
- **R4. Volume chart** above the table: lines per time bucket, stacked by level, over the
  selected time range. Plain inline SVG, colors from tokens. Clicking a bar narrows the
  time range to that bucket.

### Levels and messages (derived in the lab, display only)

- **R5. Level** comes from a table in the lab, never from the checkpoints. In this order:

  | Level | When |
  | --- | --- |
  | ERROR | `run.failed`, `node.error`, `triage.failed` (01's end of a failed run), or a line that is not valid JSON |
  | WARN | any line with an `internal_error_code`, plus `recovery.resumed_runs` and `triage.repair` |
  | DEBUG | `node.started`, `node.completed`, `run.status` |
  | INFO | everything else, including event names the table has never seen |

  An unknown event lands in INFO so an event a student adds shows up at once. The
  teaching note on the page says the level is the lab's reading of the event name, not
  something the service wrote.
- **R6. Message** per event. Numbers go through core.js's own formatters: `L.ms`
  (`26ms`, `1.8s`), `L.usd` (4 decimals) for costs, `L.money` (2 decimals) for amounts.
  Every message is one line: the first line of the text, at most 160 characters, ending
  in `…` when cut. A part whose field is missing is left out, because older log lines
  carry fewer fields.

  | Event | Message |
  | --- | --- |
  | `run.accepted` / `run.started` | `ticket T-1001` |
  | `node.started` | `→ guard_input` |
  | `node.completed` | `✓ guard_input 26ms · +$0.0004 (run $0.0011)` |
  | `node.error` | `✗ guard_input GUARDRAIL_INJECTION_SUSPECTED: <detail>` |
  | `triage.completed` | `refund_request · high · 0.92 · attempt 1 · 1.8s · $0.0002` |
  | `triage.repair` | `<detail>` |
  | `order.fetched` | `O-77201 · customer match: yes` (`no`, or `unknown` when null) |
  | `order.not_found` | `O-90112 TOOL_ORDER_NOT_FOUND` |
  | `order.no_reference` | `no order reference` |
  | `policy.retrieved` | `6 sections · top 0.51` |
  | `plan.invalid` | `VALIDATION_BUSINESS_RULE_FAILED after 0 repairs: <first violation>` (`1 repair`) |
  | `approval.required` | `refund · refund $48.50 of $64.50 · confidence 0.71`, or for a replacement `send_replacement, request_more_info · confidence 0.9` |
  | `approval.decided` | `approved` / `rejected` |
  | `refund.issued` | `$45.00 · R-1 · key <idempotency_key>` |
  | `guardrail.input_tripped` | `<guard> GUARDRAIL_INJECTION_SUSPECTED: <reason>` (no guard field: starts at the code) |
  | `run.completed` | `<first line of reply>` |
  | `run.failed` | `<user_visible_error>` |
  | `run.status` | `running` / `awaiting_approval` / `executing` / … |
  | `run.resumed` | `resumed at guard_input` |
  | `recovery.resumed_runs` | `resumed 4 runs` (`1 run`) |
  | `service.runtime` | `runtime langgraph` |
  | unparseable line | the raw text |
  | anything else | its fields as `key=value`, space separated (objects as JSON) |

### Live tail

- **R7. Live by default.** While the tab is visible, the page asks for new lines every
  2 seconds. A header pill says `● Live` or `❚❚ Paused`; the button toggles it, and so
  does Space when focus is not in a text field.
- **R8. No jumping.** If the reader has scrolled down or expanded a row, new lines do not
  move the table. A pill appears instead, `14 new lines ↑`, and clicking it scrolls to the
  top. At the top, new lines slide in (150–250 ms, none under reduced motion).
- **R9. Hidden tab, no polling.** Polling stops while the tab is hidden or another view is
  open, and catches up from its cursor when it comes back.

### Finding things

- **R10. Filters**, all combined with AND:
  - time range: last 15 min, 1 h, 24 h, or everything loaded (**the default**: the log
    files hold runs from earlier days, and a 15-minute window would open empty before
    class). A line with no `ts` passes every range.
  - service: four toggles
  - level: minimum level, **INFO by default** (DEBUG hidden, as in production)
  - free text: case-insensitive match against the raw JSON line
  - request id and event name: set by clicking a value (R11, R12)
- **R11. Facets** in a side column, with counts for the lines that pass the other filters:
  service, level, event, `internal_error_code`. Clicking a value filters to it; clicking
  again removes it. Active filters show as removable chips above the table.
- **R12. Follow a run.** Clicking a request id filters to it **and** asks the server for
  every line of that id in its file, so a run older than the loaded window is complete
  too. Above the table, a run strip shows: service, ticket, first and last `ts`, duration,
  nodes run (the `node.completed` lines), total cost, and the outcome (`completed`,
  `failed`, or `none` for no terminal event). The terminal events are `run.completed` /
  `run.failed` for 02–04 and `triage.completed` / `triage.failed` for 01. Total cost is the last `cost_usd` on a
  `node.completed` line for 02–04 (it is cumulative there) and the sum of
  `triage.completed` `cost_usd` for 01 (one per call there). For 02–04 the strip has
  **Open in Run**, which uses the existing Follow flow (`POST /api/follow`), nothing new.
- **R13. Do the same in the terminal.** The current filter is always shown as copyable
  PowerShell, like every lab report: one line per selected service, in service order,
  each on that service's file (Windows path, backslashes). The forms:
  - no filter: `Get-Content checkpoints\04-production\logs\runs.jsonl -Tail 50`
  - text only, or request id only: `Select-String -Path checkpoints\04-production\logs\runs.jsonl -SimpleMatch '3782e6221b42'`
  - event only, or error code only: `Get-Content <path> | ConvertFrom-Json | Where-Object event -eq 'node.error'`
    (or `Where-Object internal_error_code -eq '…'`)
  - two or more of them: `Get-Content <path> | ConvertFrom-Json | Where-Object { $_.request_id -eq '…' -and $_.event -eq '…' }`
  - text plus any of them: `Get-Content <path> | Select-String -SimpleMatch '<text>' | ForEach-Object Line | ConvertFrom-Json | Where-Object …`

  The order inside a condition is request id, event, error code. A single quote in a
  value is doubled (`''`), PowerShell's escape. Level and time range are the lab's own
  reading and are not in the command; a note under it says so.
- **R14. Links survive projecting.** The filter lives in the hash:
  `#logs?svc=04&level=warn&rid=3782e6221b42&q=refund&live=0`. Reloading or pasting the
  link restores it.

### HTTP contract (lab server, read only)

- **R15. `GET /api/logs?tail=1000`.** The newest `tail` lines across the four files
  (default 1,000, at most 5,000), plus:
  - `sources`: one per file, with `id`, `label`, repo-relative `path`, `exists`,
    `size_bytes`
  - `cursor`: an opaque string holding one byte offset per file

  Each line is `{id, src, ts, level, rec}`. `id` is `<src>:<byte offset of the line's
  first byte>`, stable and unique. `ts` is the line's `ts` string, or null. `rec` is the
  parsed object, or `{"raw": "..."}` for a line that is not JSON. A `tail` outside 1 to
  5,000 is clamped; one that is not a number is a 422.
- **R16. `GET /api/logs?after=<cursor>`.** The lines appended since the cursor (at most
  2,000, the oldest first; when more are waiting the reply says `more: true` and the
  cursor stops after the last line sent), and the new cursor. The same cursor asked
  twice gives the same answer: the server keeps no state. A cursor it cannot read is a
  400. A file that shrank below its offset is read again from 0 and its id is listed in
  `resets`.
- **R17. `GET /api/logs?request_id=<id>`.** Every line whose `request_id` equals the id
  exactly, from all four files, at most 2,000, with `more` as above. Each file is scanned
  over its last 20 MB at most (`LOG_SCAN_MAX_BYTES`); when a file was larger, the reply
  says `truncated: true`.
- **R18. Read only, fixed files.** The endpoint takes no path. It reads only the
  `runs.jsonl` of 01–04 (the ones present in `CHECKPOINTS`), always through `_log_path()`
  at request time, and every line (parsed or raw) goes through the server's existing
  `redact()` before it leaves.
- **R19. Order.** Every reply lists lines oldest first; the page reverses them. Lines are
  ordered by `ts`; a line with no `ts` (or an unparseable one) sorts with the line before
  it in its own file, so it never moves away from its neighbours. Ties keep file order,
  then service order.

### Seams the tests pin

Tests are written before the code, so these names are part of the spec. The server
names are tested now; the `L.logs` list is the intended shape, so later tests can pin it:

- `lab/server.py`: `_log_level(rec)` returns `"ERROR"`, `"WARN"`, `"INFO"` or `"DEBUG"`
  (`rec` is None for an unparseable line). Constants `LOG_TAIL_MAX = 5000`,
  `LOG_AFTER_MAX = 2000`, `LOG_SCAN_MAX_BYTES = 20_000_000`. The endpoint reads the
  constants at request time, so a test can lower them.
- `lab/static/logs.js` touches no DOM while it loads (views.js mounts it when `#logs` is
  routed), so its pure functions run in Node with only `core.js` loaded before it. It
  exposes `L.logs` with:
  - `message(line)`: R6.
  - `defaultState()`: `{svc: ["01","02","03","04"], level: "INFO", range: "all", q: "",
    rid: null, event: null, code: null, live: true}`.
  - `matches(line, state, now)`: R10. `now` is a millisecond timestamp, passed in.
  - `facets(lines, state, now)`: R11, `{svc, level, event, code}`, each a map value →
    count, counted over the lines that pass every filter except that facet's own.
  - `toHash(state)` and `fromHash(hash)`: R14. The default state is exactly `"logs"`.
    Keys: `svc` (comma separated), `level` (lower case), `range`, `q`, `rid`, `event`,
    `code`, `live` (`0` when paused); only keys that differ from the default are
    written. `fromHash` ignores unknown keys and falls back to the default for a bad
    value.
  - `terminal(state, sources)`: R13, an array of strings. `sources` is the `sources` list
    from R15.
  - `runSummary(lines)`: R12, `{src, ticket, first, last, duration_ms, nodes, cost_usd,
    outcome, follow}`; `follow` is false for 01.
  - `merge(lines, incoming, max)`: R8 and the 5,000-line cap. Both arrays are oldest
    first and `incoming` is newer (an `after=` reply). Returns `{lines, dropped}`:
    `incoming` appended, a line whose `id` is already there skipped, and at most `max`
    lines, the oldest dropped first (`dropped` counts them).
- `LOG_AFTER_MAX` is the most lines any one reply carries, for `after=` and
  `request_id=` alike.
- Level colors are four tokens, `--level-error`, `--level-warn`, `--level-info` and
  `--level-debug`. In each theme they resolve to a hex value, directly or through
  `var()` (for example `--level-error: var(--bad)` in the derived block), and each is at
  least 4.5:1 against `--panel` and `--bg` in that theme.

## Limits

- **Nothing in `checkpoints/` changes.** No `level` added to `obslog`, no new events.
  `git diff --stat checkpoints/` is empty after this work. Deleting `lab/` still changes
  nothing in the checkpoints.
- No new dependencies, no npm, no build step, no CDN. Polling, not WebSockets. Works
  offline.
- ES5-style JS in a new `lab/static/logs.js`, loaded after `core.js`, reading `window.L`.
  Every value from a log line is rendered with `textContent` (via `L.el`), never
  `innerHTML`: replies, rationales and guard reasons carry model text and injection
  payloads.
- Colors only from the semantic tokens at the top of `lab.css`. New tokens for the four
  levels and the chart, defined in **both** themes. Text ≥ 4.5:1 contrast in both themes,
  ≥ 12px, visible `:focus-visible` ring, `prefers-reduced-motion` respected.
- The server never writes, truncates, rotates or deletes a log.
- The pure parts on the server (reading a tail, reading after a cursor, the level table)
  are plain functions next to `_read_log_since`, testable without a running service.
- The browser holds at most 5,000 lines. Past that, the oldest are dropped and a line at
  the bottom says so, with **Load older** (should have; see open decision 6).

## Edge cases

- **No log yet** (a checkpoint never run): that service shows `no log yet` in its facet,
  with a zero count. Not an error.
- **Half-written last line** (a run still writing): the server reads only up to the last
  newline and leaves the rest for the next poll. The cursor never points inside a line.
- **Unparseable complete line** (a torn write after a crash): shown as ERROR with the raw
  text. `dashboard.py` skips these; the page shows them, because a torn line is evidence.
- **File shrank or was deleted** while tailing (size below the cursor): the cursor for
  that file resets to 0 and a divider row says `04-production log was reset`.
- **Bad or missing `ts`**: the line keeps its place in its file's order and shows `—` as
  its time.
- **CRLF line ends** (a file edited or written on Windows in text mode): parsed the same
  as LF; the `\r` is not part of the line.
- **Interleaved runs** (two runs on one service, as in the current 04 file): that is the
  point. Rows are not grouped by run. Clicking a request id untangles one.
- **Huge file**: tail reads backwards from the end in blocks, so a 50 MB log costs the
  same as a 50 KB one. The `request_id` scan (R17) reads the whole file; above 20 MB it
  answers with what it found in the last 20 MB and says so.
- **The lab restarts**: the cursor is only byte offsets, so the page carries on with no
  server state to lose.
- **01 is not a service**: its request ids are UUIDs and it has no Follow; the run strip
  drops **Open in Run** for it.

## Out of scope

- Generating traffic. The page shows what runs produced. Repeat, Compare and the terminal
  already make runs.
- Services' printed output (uvicorn access lines, tracebacks). It stays in Setup's
  service log (see open decision 3).
- Charts of cost or latency per node. `dashboard.py` is the lesson for that (Session 12).
- Log shipping, retention, OpenTelemetry, alerts, saved searches.
- Changing the Run view's per-run log panel.

## Done when

1. `py -m pytest -q` inside `lab/` passes, offline, with no service running. The tests
   were written before the code, and are deliberately basic for now:
   - `tests/test_logs_api.py`: the level table (R5), the tail and sources (R15), a torn
     and a half-written line, `after=` (R16) including a cleared log, `request_id=`
     (R17), redaction and read-only (R18).
   - `tests/test_logs_ui.py`: the tab, the hidden view, the script order, no
     `innerHTML` in `logs.js`.

   Not covered by tests yet (checked by hand in steps 3–8): the `L.logs` functions
   (messages, filters, facets, the hash, the terminal line, the run strip), the clamps
   and caps, cross-file ordering of lines with no `ts`, and the level colors' contrast.
2. Every checkpoint's `pytest` gives the same result as before, and
   `git diff --stat checkpoints/` is empty.
3. In a browser, both themes: with 04 running, a run started from the Run tab shows up in
   Logs within 3 seconds, without a reload (R7). Pausing stops it; resuming catches up.
4. Running T-1006 on 04 shows `guardrail.input_tripped` as WARN and `run.failed` as ERROR;
   clicking its request id shows the run strip with outcome `failed` (R5, R12).
5. A ticket run from a terminal with `Invoke-RestMethod` (not the lab) shows up too.
6. With a request id, event or error code filter, level DEBUG and range "everything",
   the terminal line under the table, pasted into PowerShell, returns the same lines the
   table shows (R13).
7. Copying the page URL with filters set and opening it in a new tab restores the same
   view (R14).
8. Tab and Shift+Tab reach every control with a visible focus ring; injection tickets'
   text renders as text.
9. `lab/README.md` is updated: the views list, the HTTP contract table, the Files list,
   and the "Deliberately left to the terminal" paragraph (open decision 1). The teaching
   note and code links (`obslog.py` `def log_event`, `events.py` `def publish`) are
   added for the new page.

## Open decisions

1. **The lab rule this changes.** `lab/README.md` says the lab does not search
   `logs/runs.jsonl` across runs: "those are the lesson, not friction." This page does
   exactly that. *Recommendation:* allow it, and keep the lesson by making R13 the
   terminal line non-optional, so every view on the page names the command that does the
   same. The README paragraph gets rewritten to say so.
2. **When students see it.** `lab/` already ships with push 1, so the next push carries
   the page whatever its session. *Recommendation:* ship it in the push for Session 4
   (the service, where request ids and events first matter). Alternative: hold it back
   until Session 12 so it does not compete with `dashboard.py`. **Decided:** the page was
   built live in Session 3, so it ships with the Session 3 push.
3. **Printed output as a second stream.** Real production logs mix structured app logs
   with access logs and tracebacks. The lab has a managed service's last 300 printed lines
   in memory (Setup). *Recommendation:* not in v1. Those lines exist only for services the
   lab started and vanish on restart, so the page would show different things depending
   on who started what.
4. **Level in the lab vs. in the checkpoints.** Adding `level` to `obslog.log_event` would
   be more realistic but changes four checkpoints and their tests, and backports a feature
   no session teaches. *Recommendation:* the lab's table (R5), with the teaching note.
5. **Newest first or oldest first.** Log explorers put newest at the top; `tail -f` puts it
   at the bottom. *Recommendation:* newest first (R2), since it reads better projected.
6. **Load older.** Must have, or leave it at "last 5,000 lines"? *Recommendation:* should
   have, after the rest works; the current files are 356 to 1,036 lines.
7. **Where this spec lives.** It is in `specs/`, which no release push includes. If you
   want students to see a real spec for a lab feature (it fits Session 3), it could move
   to `lab/` and ship. **Decided:** it lives in `lab/specs/` and ships with the lab, as
   Session 3's example of a real spec.
