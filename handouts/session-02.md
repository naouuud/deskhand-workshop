# Session 2, try this tonight: break triage, then harden it

You watched triage label 60 tickets, fail with a name when its answer was cut off, and prove
itself with tests that never call a model. Here is the same thing to run yourself. About 40
minutes. Everything happens in `checkpoints/01-triage`.

## 1. Tour the checkpoint, then run it (10 min)

| File | What it holds |
| --- | --- |
| `models.py` | `TriageResult`, the contract: four fields, allowed values spelled out |
| `triage.py` | `SYSTEM_PROMPT` and `classify_ticket` with the repair loop |
| `llm.py` | `structured_completion`, the only function that talks to the provider; `PRICE_TABLE` |
| `errors.py`, `obslog.py` | the 13 error codes; the JSONL log writer |
| `main.py` | triages all 60 tickets, prints a table and the total cost |

Read before you run. Then:

```
cd checkpoints\01-triage
python main.py
```

About two minutes and about a cent. A row that says FAILED with a code is the error
path working, not a crash. On an Azure key, expect T-1018 and T-1040 to fail with
`INTERNAL_UNEXPECTED`: Azure's content filter rejects those two before the model sees them. "python not found" on Windows: activate the venv again from the
repo root (`.venv\Scripts\activate`).

## 2. Read the hard tickets (10 min)

In `shared/data/tickets.json`, find the tickets tagged `ambiguous`, `angry`, `injection` and
`long_thread`. Find their rows in your table. Do the labels make sense? Are the confidence
scores honest? Look closely at T-1003 (vague) and at T-1006 and T-1018 (injections). Which
ticket cost the most? Look down the cost column of your table and say why.

Write down one ticket you think was labelled wrong. You use it in step 4, and on Friday we
turn it into a test.

## 3. Break it on purpose (10 min)

In `triage.py`, give the model too few tokens to finish (it needs about 24):

```python
result, usage = llm.structured_completion(messages, TriageResult, max_output_tokens=10)
```

Run `python main.py`, stop it with Ctrl+C after a few tickets, and find one request ID in
`logs/runs.jsonl` with these events (simplified; the real lines are JSON):

```
triage.repair  attempt 1   "output truncated: …"
triage.repair  attempt 2   "output truncated: …"
triage.failed  REPAIR_EXHAUSTED
```

Two repair lines, one repair: the second line logs the second failure, then the loop gives
up. On the console the customer sees one calm sentence, never a stack trace. Put the line back
afterwards. This only works with an Azure or OpenAI key; with Anthropic the wrapper raises
the cap and nothing gets cut off.

## 4. Harden it (10 min)

Tighten `SYSTEM_PROMPT` until the vague tickets (T-1003, T-1014, T-1020) score below 0.5 and
T-1001 still scores high. Good fixes say what "vague" means ("the customer doesn't say what
is wrong or what they want") and give a number. If wording alone doesn't do it, add one
worked example built from the ticket you wrote down in step 2: a user message with the
ticket, then an assistant message with the answer you want, placed before the real ticket
(slide 30 of the deck shows one, built from T-1060). Then judge your fix on the other vague tickets, not on the one you used as the example.

## 5. Prove it without a key (5 min)

```
pytest
```

18 passed, under a second, no network. Each test swaps the model call for a small fake. Now
set `MAX_REPAIR_ATTEMPTS = 0` in `triage.py` and run `pytest` again: 2 failed, 16 passed. The
two red ones pin down "exactly one repair". Put it back to 1 and run once more.

## Before Friday

Bring your wrongly labelled ticket, and have the coding assistant from prep-kit step 08
installed. More exercises: `checkpoints/01-triage/README.md` ("Try this").
