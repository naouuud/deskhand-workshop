# Session 1, try this tonight: measure the machine

You watched Deskhand v1 fail and saw its cost and latency measured. Here is the same thing to
run yourself. About an hour. Everything needs the repo and a working key; nothing needs to
be finished before Wednesday.

Setup, once: from the `deskhand-workshop` folder with the venv active,
`python ..\prep-kit\code\check_setup.py` should end with "Ready for Day 1". If it doesn't,
it tells you what to fix. Skipped the prep kit? https://github.com/hasankanaan26/prep-kit,
steps 01 and 08.

## 1. Watch v1 fail, three times (10 min)

```
cd checkpoints\00-broken-bot
python incidents.py
```

Run it three times. Write down the refund amount for ticket C each time. How much did the
bot's randomness cost Nimbus? Bring the three numbers on Wednesday; they open the session.

Run it from its own folder. From anywhere else, `bot.py` loads zero policies and still
answers, which is a silent failure worth seeing once.

## 2. Your first API call, done properly (15 min)

In a new file at the repo root:

```python
import json, time, uuid
from dotenv import load_dotenv
from openai import OpenAI            # on Azure: AzureOpenAI, set up as in bot.py
load_dotenv()                        # loads your keys from .env
client = OpenAI()
request_id = uuid.uuid4().hex[:8]    # this call's tracking number
t0 = time.perf_counter()
resp = client.chat.completions.create(
    model="gpt-4o-mini", temperature=0,       # on Azure: your deployment name
    messages=[{"role": "user", "content": "Say hello to Nimbus."}])
latency_ms = round((time.perf_counter() - t0) * 1000)
```

Print the reply, the two token counts in `resp.usage`, and `latency_ms`. Then append them,
with the `request_id`, as one JSON line to `logs/runs.jsonl` (create the folder first).
Done when the file has one line per call.

Stuck? "Key not found" means `load_dotenv()` is missing. On Azure, use the deployment name,
not the model name. A reference for the logging pattern: `checkpoints/01-triage/obslog.py`.

## 3. Count tokens, put a price on them (15 min)

Take ticket T-1001 (the cracked headphones) from `shared/data/tickets.json`. Send it three
ways and fill in the table:

| Prompt                                      | Prompt tokens | Output tokens | Cost | Was the answer better? |
| ------------------------------------------- | ------------- | ------------- | ---- | ---------------------- |
| Ticket only                                 |               |               |      |                        |
| Ticket + `shared/policies/refund-policy.md` |               |               |      |                        |
| Ticket + all six policies                   |               |               |      |                        |

Cost = tokens × price ÷ 1,000,000. Prices are in `PRICE_TABLE` in
`checkpoints/01-triage/llm.py`: use the row for the model you called (step 2 uses
gpt-4o-mini). Expect roughly 60, 480 and 2,400 prompt tokens. For this
ticket the refund policy helps; the other five add cost and little else. That gap is why
Week 3 exists.

## 4. Twenty calls: cost, average, p95 (15 min)

Loop one prompt 20 times, for example "Classify this ticket as refund, replacement or
question:" with T-1001. Log every call. Then compute the total cost, the average latency,
and p95, which with 20 calls is `sorted(latencies)[18]` (the 19th value; Python counts from
0). How much bigger is your p95 than your average?

Bonus: five calls at `temperature=1.0` asking for a one-sentence resolution. Are the five
answers the same? That drift is incident #8.

## Before Wednesday

- Keep your `logs/runs.jsonl`. We use it again.
- Imagine a v1 run just failed for a customer on the phone. List every piece of information
  you wish you had. That list is the next four weeks.

The old lab walk-through, with more detail, is `checkpoints/00-broken-bot/README.md`
("Try this").
