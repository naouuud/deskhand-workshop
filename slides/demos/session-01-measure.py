"""Session 1, slide 27: measure it, then find it in the log.

The measuring script from the Session 1 guide ("The measuring script"), and the reference
solution for tonight's steps 3 and 4. Run from the repo root with `.env` filled in:

    py slides\\demos\\session-01-measure.py

It makes 28 small calls (about a cent on gpt-4.1-mini or gpt-4o-mini), one part per
question on the slide, and ends with the three answers side by side:

  Part 1  T-1001 three ways: ticket only, + refund-policy.md, + all six policies.
          Expect about 60, 480 and 2,400 prompt tokens. Each answer is printed in full
          with the day windows it quotes ("30-day"), so the room can judge "better?".
  Part 2  one prompt 20 times: mean, p95 (sorted(latencies)[18]) and max latency, and
          p95 as a multiple of the mean.
  Part 3  five calls at temperature 1.0, printed in full, each tagged with the remedies
          it offers (a keyword match: refund, replacement, credit, ...). Distinct tags
          are the "different decisions"; read the answers to confirm.

Every call is appended to logs/runs.jsonl at the repo root with its own request ID, so
`findstr <id> logs\\runs.jsonl` finds it. The model call, the price table and the log
writer are checkpoint 01's (llm.text_completion, llm.PRICE_TABLE, obslog.log_event), so
LLM_PROVIDER in .env picks azure, openai or anthropic. Claude takes no temperature, so on
anthropic part 3 shows the drift you get anyway.
"""
import json
import re
import statistics
import sys
import textwrap
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "checkpoints" / "01-triage"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

import errors  # noqa: E402  (checkpoint 01's errors.py)
import llm  # noqa: E402  (checkpoint 01's llm.py)
import obslog  # noqa: E402  (checkpoint 01's obslog.py)

# Same writer as checkpoint 01, but into the repo root's logs/ folder, where the
# handout's own calls go and where the room searches.
obslog.LOG_PATH = REPO / "logs" / "runs.jsonl"

# Saving the output as the fallback file must not crash on a curly quote.
sys.stdout.reconfigure(errors="replace")

# What a resolution for T-1001 can offer. A rough tag for reading decisions off the
# projector, not a classifier: the instructor reads the answers to confirm.
REMEDIES = [
    ("replacement", r"\breplac"),
    ("refund", r"\brefund"),
    ("credit", r"\b(credit|discount|voucher|coupon|gift card|compensat)"),
    ("expedited shipping", r"\bexpedit"),
    ("return label", r"\b(return label|prepaid|return shipping)"),
    ("escalate", r"\b(escalat|supervisor|manager|team lead)"),
]


def call(prompt: str, part: str, temperature: float = 0.0) -> tuple[str, dict]:
    rid = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    text, usage = llm.text_completion([{"role": "user", "content": prompt}],
                                      temperature=temperature)
    ms = round((time.perf_counter() - t0) * 1000)
    rec = {"request_id": rid, "model": llm.DEFAULT_MODEL, "part": part,
           "temperature": temperature, "prompt_tokens": usage.prompt_tokens,
           "completion_tokens": usage.completion_tokens,
           "cost_usd": round(usage.cost_usd, 6), "latency_ms": ms}
    obslog.log_event("llm.call", **rec)
    return text, rec


def show(text: str) -> None:
    print(textwrap.fill(" ".join(text.split()), width=96,
                        initial_indent="    ", subsequent_indent="    "))


def remedies(text: str) -> str:
    found = [name for name, pattern in REMEDIES if re.search(pattern, text, re.IGNORECASE)]
    return " + ".join(found) or "none of the usual"


def money(text: str, prompt: str) -> list[str]:
    """Every amount the answer promises, and whether that number was in the prompt."""
    found = []
    for n in re.findall(r"\$\s?(\d+(?:\.\d\d)?)", text):
        n = n.removesuffix(".00")
        sent = re.search(rf"\${re.escape(n)}(\.00)?\b", prompt)
        found.append(f"${n} ({'in the prompt' if sent else 'not in the prompt'})")
    for n in re.findall(r"(\d+(?:\.\d+)?)\s?(?:%|percent)", text):
        sent = re.search(rf"\b{re.escape(n)}\s?(%|percent)", prompt)
        found.append(f"{n}% ({'in the prompt' if sent else 'not in the prompt'})")
    return found


def main() -> None:
    tickets = json.loads((REPO / "shared" / "data" / "tickets.json").read_text(encoding="utf-8"))["tickets"]
    ticket = next(t for t in tickets if t["ticket_id"] == "T-1001")["messages"][0]["text"]
    policies = {p.name: p.read_text(encoding="utf-8")
                for p in sorted((REPO / "shared" / "policies").glob("*.md"))}
    ask = "Suggest a resolution for this support ticket in two sentences.\n\n"
    total = 0.0

    print(f"Model: {llm.DEFAULT_MODEL} ({llm.PROVIDER}). Every call goes to logs/runs.jsonl.")

    print("\n1. Three prompt sizes (T-1001). How many tokens? Does the answer get better?")
    sizes, promised = [], []
    for label, extra in [("ticket only", ""),
                         ("ticket + refund policy", policies["refund-policy.md"]),
                         ("ticket + all six", "\n\n".join(policies.values()))]:
        prompt = ask + extra + "\n\nTicket:\n" + ticket
        text, rec = call(prompt, "prompt-size")
        total += rec["cost_usd"]
        sizes.append(rec["prompt_tokens"])
        promised.append(f"{label}: {', '.join(money(text, prompt)) or 'no amount'}")
        print(f"\n  {label:24} {rec['prompt_tokens']:>5} in {rec['completion_tokens']:>4} out"
              f"  ${rec['cost_usd']:.5f}  id {rec['request_id']}")
        print(f"    offers: {remedies(text)}   money: {', '.join(money(text, prompt)) or 'none'}")
        show(text)

    print("\n2. One prompt, 20 calls. How much slower is p95 than the average?")
    lat = []
    for i in range(20):
        _, rec = call("Classify this ticket as refund, replacement or question. One word.\n\n"
                      + ticket, "latency")
        total += rec["cost_usd"]
        lat.append(rec["latency_ms"])
        print(f"  call {i + 1:>2}  id {rec['request_id']}  {rec['latency_ms']:>5} ms")
    mean, p95 = round(statistics.mean(lat)), sorted(lat)[18]
    print(f"  mean {mean} ms   p95 {p95} ms   max {max(lat)} ms   p95 = {p95 / mean:.1f}x the mean")

    print("\n3. Five calls at temperature 1.0. How many different decisions out of five?")
    answers, decisions, made_up = [], [], 0
    for i in range(5):
        prompt = ask + ticket
        text, rec = call(prompt, "temperature", temperature=1.0)
        total += rec["cost_usd"]
        amounts = money(text, prompt)
        answers.append(" ".join(text.split()))
        decisions.append(remedies(text) + "".join(f", {a.split()[0]}" for a in amounts))
        made_up += any("not in the prompt" in a for a in amounts)
        print(f"\n  answer {i + 1}  id {rec['request_id']}  offers: {remedies(text)}"
              f"   money: {', '.join(amounts) or 'none'}")
        show(text)

    print("\nAnswers to slide 27")
    print(f"  1. Prompt tokens {sizes[0]:,} / {sizes[1]:,} / {sizes[2]:,}: all six policies cost"
          f" {sizes[2] / sizes[0]:.0f}x the ticket alone.")
    print(f"     Better? Money promised: {'; '.join(promised)}. Read the three answers above.")
    print(f"  2. p95 is {p95 / mean:.1f}x the mean ({p95} ms against {mean} ms); the slowest call took {max(lat)} ms.")
    print(f"  3. {len(set(decisions))} different decisions out of 5, {len(set(answers))} different wordings:")
    for d in sorted(set(decisions)):
        print(f"     {decisions.count(d)} x {d}")
    if made_up:
        print(f"     {made_up} of 5 promise an amount that was not in the prompt: the model made it up.")

    print(f"\n28 calls, ${total:.5f} in all.", end=" ")
    if total == 0:
        print("(Price 0: the model name matches nothing in llm.PRICE_TABLE.)", end=" ")
    print("Find any call: findstr <id> logs\\runs.jsonl")


if __name__ == "__main__":
    try:
        main()
    except (errors.DeskhandError, RuntimeError) as exc:
        sys.exit(f"The call failed: {exc}. Check .env (LLM_PROVIDER and its key), then run again.")
