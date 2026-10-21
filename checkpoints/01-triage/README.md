# Checkpoint 01 — Triage

**Sessions:** 2–3
**Demonstrates:** structured outputs with Pydantic, a bounded repair loop, a typed
error taxonomy, and request-ID observability from day one.

This is the first Deskhand node done properly: a triage classifier that reads a raw
ticket and returns a validated `TriageResult` — `{category, priority, sentiment,
confidence}` — or fails with a typed error. The mantra:

> Never trust LLM output directly. Never expose raw failures to users.

## What changed vs 00-broken-bot

- **Output contract.** `TriageResult` is a Pydantic model enforced via the provider's
  structured-output mode — no more `json.loads` and hope. (`models.py`)
- **One provider boundary.** All LLM calls go through `llm.py`; provider exceptions
  are mapped to taxonomy codes at that boundary and swapping providers touches one
  file. It also prices every call from `PRICE_TABLE`. (`llm.py`) Besides Azure,
  OpenAI and Anthropic it accepts `gemini`, `openrouter`, `ollama` and `lmstudio`:
  the same `openai` client at a different `base_url` (`OPENAI_COMPATIBLE`; setup
  in [LLM_PROVIDERS.md](../../LLM_PROVIDERS.md)).
- **Bounded repair, not blind retry.** On malformed output: exactly ONE re-prompt
  that includes what was wrong, then a typed `REPAIR_EXHAUSTED` failure. Rate limits
  and timeouts are not "repaired" — they surface with their own codes. (`triage.py`)
- **Error taxonomy.** Internal codes for machines and logs; user-visible messages
  chosen separately, never `str(exception)`. (`errors.py`)
- **Observability.** Every run carries a `request_id`; every event is one JSON line
  in `logs/runs.jsonl` with tokens, latency, and cost. A failed run is greppable.
  (`obslog.py`)
- **Temperature 0** and an honest confidence field instead of "creative answers".
- **Offline tests.** The LLM is monkeypatched; the suite runs with no key and no cost.

## Run it

From the repo root (venv active, `.env` filled in — see the root README):

```bash
cd checkpoints/01-triage
python main.py        # triages all 60 shared tickets, prints a rich table + total cost
pytest                # offline — no API key needed
```

`main.py` costs real money (60 small calls — roughly a cent on a mini-tier model). The
per-run log lands in `logs/runs.jsonl`; pick any `request_id` from the table run and
grep for it.

## Try this

1. Break the contract on purpose: in `models.py`, rename `category` to `categoryy`
   and run `main.py`. Where does the failure surface, and what does the log line say?
   Now put it back and instead make `confidence` a `str` — different failure, why?
2. Look at what triage says for T-1006 and T-1018 (the injection tickets) and
   T-1003 (the vague one). Are the confidence scores honest? Tighten `SYSTEM_PROMPT`
   until vague tickets reliably score below 0.5, without wrecking the happy paths.
3. Compute cost-per-ticket from `logs/runs.jsonl` alone (one `jq`/Python one-liner).
   Which ticket was most expensive, and why? (Hint: look at the long threads.)
