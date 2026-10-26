# Session 3, try this tonight: context, the spec, permissions

You watched a coding agent build the lab's Logs page spec first: a rules file it read before
starting, a spec with open decisions (`lab/specs/logs-page.md`), tests written before the code
(`lab/tests/`), and two hooks that checked its every step. Now do the same with your own
agent, whichever brand, on a smaller feature.
About 60 minutes. Everything happens in a copy of triage, so the checkpoint stays clean.

Keep `assistant-log.md` open the whole time. Three lines by the end: one guess you caught,
one rule you added, one thing a safeguard caught. Example: "Said the repair loop uses tenacity.
Caught: opened triage.py, no tenacity anywhere."

## 1. Copy, add the rules file, and test whether the agent reads it (10 min)

From the repo root, venv active, in PowerShell:

```
mkdir work
cp -r checkpoints\01-triage work\my-triage
cd work\my-triage
python -m pytest
```

18 passed. Keep the folder at that depth: a copy on your Desktop passes its tests, then
`main.py` can't find `tickets.json`. (`cp` not found means you are in cmd.exe; use
PowerShell.)

Add `AGENTS.md` with these eight rules. Each one is something the triage code already does, so
the agent can check it against a file:

```
# AGENTS.md (the triage folder)

- Python 3.12, Pydantic v2. Output models live in models.py.
- Only llm.py talks to the model provider. Call llm.structured_completion.
- Errors: raise errors.DeskhandError with a code from errors.py.
  Customers see errors.user_message(code), never str(exc).
- Events: obslog.log_event(event, request_id, **fields).
  No logging module, no print() debugging.
- Run history: logs/runs.jsonl, one JSON line per event.
  Questions about past runs or tickets: answer only from that file,
  quoting its lines. No line for it? Say so. Never guess or re-run.
- Tests run offline: monkeypatch llm.structured_completion.
  Never call a real model from a test.
- Ask before adding a dependency or changing models.py.
- Run `python -m pytest` before you say "done".
```

Then add the pointer file your tool reads:

| Tool | Reads | What to add |
| --- | --- | --- |
| Claude Code | `CLAUDE.md` | one line: `@AGENTS.md` |
| GitHub Copilot | `.github/copilot-instructions.md` | the same eight rules, or a line pointing at `AGENTS.md` |
| Cursor | `.cursor/rules/deskhand.mdc` | the same eight rules |
| Codex | `AGENTS.md` | nothing more |

Start the agent from `work/my-triage` (the folder it starts in is the folder it can see) and
ask:

- "How does the repair loop in triage.py work?" Check every name against the file. Real:
  `MAX_REPAIR_ATTEMPTS = 1`, `LLM_MALFORMED_OUTPUT`, `REPAIR_EXHAUSTED`. A retry library or
  exponential backoff means it guessed.
- "Where is the run history, and what does the last line say?" Only the rules file says where
  the log is. If it opens `logs/runs.jsonl`, the rules file was read.

## 2. Spec, plan, tests, code, diff (20 min)

A feature you did not see built in class. Save this as `spec.md` in `work/my-triage`:

```
# spec.md: triage one ticket from the command line

## What it does
`python main.py --ticket T-1001` triages only that ticket and prints the same
table with one row. Without the flag, main.py behaves exactly as before.

## Limits
- Use argparse from the standard library. No new dependencies.
- Do not change classify_ticket() or models.py.

## Edge cases
- An unknown id prints one line naming the id, makes no model call, and exits
  with code 1. No traceback.
- Put the lookup in a pure function select_tickets(tickets, ticket_id),
  testable without the LLM.

## Done when
tests/test_select.py passes, and every other test gives the same result as before.
```

Then, in this order:

1. **Plan.** Plan mode if your tool has one (Claude Code: Shift+Tab until it says plan), or
   start the prompt with "propose a plan, write no code yet":
   "Read spec.md and propose a plan: which files change, which tests come first, and anything
   in the spec you would push back on. Write no code yet."
   Check the plan against the four parts of the spec. Fix one line if needed, ask again.
2. **Tests first.** "Write tests/test_select.py from spec.md. Don't implement anything. The
   tests must fail." Run `python -m pytest -q`: the new tests are red, the 18 old ones green.
   Read them. One must cover the unknown id.
3. **Then the code.** "Implement spec.md until tests/test_select.py passes. Don't change the
   tests. Run pytest before you say done."
4. **Read the diff before you run anything.**

   ```
   git add .            # stage everything, new files too
   git diff --staged    # every line changed since your last commit, in every file
   ```

   The five questions from slide 16: new imports? files it wasn't asked to touch (`models.py`,
   `triage.py`, the old tests)? the edge case (follow an unknown id through the new lines)? any
   error swallowed? did it change a test to make it pass? Then `python -m pytest`, then
   `python main.py --ticket T-1001` (one live call, a fraction of a cent).

If the agent edits a test to make it pass, or touches `triage.py`, revert it and log it.

## 3. Your test list: the three gaps (15 min)

Write `test-list.md` in plain sentences, no code:

- A confidence outside 0..1, such as 1.7, is rejected.
- When a repair happens, the cost adds up both calls.
- Every failure, including a rate limit, leaves a line in `runs.jsonl` with its error code.
- One line of your own.

Hand it over:

```
Write pytest tests in tests/test_triage_extra.py, one per line of test-list.md.
Copy the pattern in tests/test_triage.py: monkeypatch llm.structured_completion
with a fake. No network calls. Don't change triage.py or models.py.
```

Run `python -m pytest -q`. Expect the first three to go red against today's code: confidence
has no bounds, the broken first answer's cost is never added, a rate limit leaves no log line.
(If the log test only tries a broken answer, it stays green: add a rate limit and a timeout.)
Red here is a finding, not a broken test. Mark those tests
`@pytest.mark.xfail(reason="gap: ...", strict=True)` instead of deleting them. If the agent
"fixes" `triage.py` or `models.py` to turn one green, revert it and log it. A test that hangs
or asks for a key is calling a real model: also a log entry.

Then make every green test fail once: break one line of the code, see red, undo, see green.
Three good breaks: `"warranty"` added to the categories in `models.py`, the `triage.failed`
log call deleted in `triage.py`, `MAX_REPAIR_ATTEMPTS = 0`. A test you can't make red can't
fail: fix it or delete it, and count them. Then:

```
git add .
git commit -m "triage: --ticket flag spec first, my test list, each test seen failing"
```

## 4. Two safeguards: the hooks, and a reviewer (15 min)

**The hooks.** Claude Code: create the three files below under `work/my-triage/.claude/`.
Start `claude` from `work/my-triage` with the venv active (so `python` is the one with
pytest), switch to accept-edits mode, and ask for a change that contradicts your spec:
"Change select_tickets() so an unknown id returns an empty list instead of exiting." The
contract hook blocks any edit to the existing tests or `spec.md`, and if a test is red when the
agent says done, the Stop hook sends it back with the failing output. In rehearsal, with the
Stop hook alone, the agent rewrote the test and the spec to match its change and the Stop hook
saw green; the contract hook is what stops that. (The two hooks from class guard different
things, the `.env` file and `innerHTML`; they are in `slides/demos/session-03/class-hooks/`.) Other tools: a pre-commit hook that runs `python -m pytest -q`, or your
tool's own hooks (Cursor and Copilot have them).

`.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          { "type": "command", "command": "python .claude/hooks/protect_tests.py", "timeout": 30 }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python .claude/hooks/pytest_gate.py", "timeout": 120 }
        ]
      }
    ]
  }
}
```

`.claude/hooks/protect_tests.py`:

```python
"""PreToolUse hook: the agent may not change the contract while it works.

Runs before every Edit or Write. The contract is the tests and spec.md: changing
them to make a run pass erases the finding. Editing an existing test file or the
spec exits 2, which blocks the edit and hands the agent the reason. Writing a new
test file is allowed, so "tests first" still works. Everything else exits 0.
"""
import json
import sys
from pathlib import Path

payload = {}
if not sys.stdin.isatty():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

tool = payload.get("tool_name", "")
target = Path(str(payload.get("tool_input", {}).get("file_path", "")))
parts = [p.lower() for p in target.parts]
is_contract = "tests" in parts or target.name.lower() == "spec.md"
is_new_file = tool == "Write" and not target.exists()

if is_contract and not is_new_file:
    print(
        f"Blocked: {target.name} is part of the contract (tests/ and spec.md). "
        "Change the code to satisfy the tests, or stop and say what in the spec should change.",
        file=sys.stderr,
    )
    sys.exit(2)
sys.exit(0)
```

`.claude/hooks/pytest_gate.py`:

```python
"""Stop hook: the agent may not say "done" while a test is red.

Claude Code runs this command each time the agent is about to stop. Exit 0
lets it stop. Exit 2 blocks the stop and hands everything printed to stderr
back to the agent, so it sees the failing test and keeps working.

The agent gets three tries per session. After that the hook lets it stop, so a
test it cannot fix ends with a report instead of an endless loop.
"""
import json
import subprocess
import sys
from pathlib import Path

MAX_TRIES = 3

payload = {}
if not sys.stdin.isatty():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

counter = Path(__file__).with_name(f".gate-{payload.get('session_id', 'default')}")
tries = int(counter.read_text()) if counter.exists() else 0

run = subprocess.run(
    [sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
    capture_output=True, text=True,
)
if run.returncode == 0:
    if counter.exists():
        counter.unlink()
    sys.exit(0)

if tries >= MAX_TRIES:
    counter.unlink()
    print(f"Tests are still red after {MAX_TRIES} tries. Stopping; report the failure.", file=sys.stderr)
    sys.exit(0)

counter.write_text(str(tries + 1))
tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-25:])
print(f"Tests are red, so this is not done (try {tries + 1} of {MAX_TRIES}).\n{tail}", file=sys.stderr)
sys.exit(2)
```

**A reviewer.** One that didn't write the code. Open a fresh session whose context holds
nothing but the diff:

```
git diff --staged > review.diff
```

Then: "Review review.diff. Answer these five questions, each with the file and line: 1 new
imports? 2 files it wasn't asked to touch? 3 follow one unknown ticket id through the new
lines: what happens? 4 any error swallowed? 5 any test changed to make it pass? End with
MERGE or FIX FIRST." Did the reviewer find anything the author missed?

Done when a hook has blocked the agent at least once and `assistant-log.md` has its three
lines.

## Before Monday

Bring two lines from `assistant-log.md`, especially the "how I caught it" half. Session 4 turns
triage into a service; skim `checkpoints/02-service/README.md`. Stretch: plant two bugs in a
classmate's `triage.py` (or your own) and see which ones the suite misses (deck, last slide).
