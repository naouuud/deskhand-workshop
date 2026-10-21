import json
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
LOG_FILE_PATH = Path("logs/runs.jsonl")
LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

with open("../shared/data/tickets.json", "r", encoding="utf-8") as f:
    data = json.load(f)

TICKET_TEXT = data["tickets"][0]["messages"][0]["text"]

PROMPT = f"""Classify this ticket as refund, replacement or question:
{TICKET_TEXT}"""

MODEL = "gpt-4o-mini"
CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def calc_cost(tokens_in: int, tokens_out: int, model: str):
    PRICE_TABLE: dict[str, dict[str, float]] = {
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
        "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
        "gpt-4.1": {"input": 2.00, "output": 8.00},
        "claude-opus-5-5": {"input": 4.00, "output": 20.00},
        "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
        "claude-sonnet-5": {"input": 2.00, "output": 10.00},
        "claude-haiku-5-5": {
            "input": 0.10,
            "output": 0.50,
        },  # prompts up to 100K tokens
        "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    }

    model_data = PRICE_TABLE[model]
    return (
        tokens_in * model_data["input"] + tokens_out * model_data["output"]
    ) / 1_000_000


def invoke():
    request_id = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    response = CLIENT.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[{"role": "user", "content": PROMPT}],
    )
    latency = round((time.perf_counter() - t0) * 1000)
    t_in = response.usage.prompt_tokens
    t_out = response.usage.completion_tokens
    run_cost = calc_cost(t_in, t_out, MODEL)

    log = {
        "request_id": request_id,
        "prompt": PROMPT,
        "reply": response.choices[0].message.content,
        "prompt_tokens": t_in,
        "completion_tokens": t_out,
        "cost": run_cost,
        "latency_ms": latency,
    }

    with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(log) + "\n")

    return run_cost, latency


run_costs = []
latencies = []

for i in range(20):
    run_cost, latency = invoke()
    run_costs.append(run_cost)
    latencies.append(latency)

total_cost = sum(run_costs)
avg_latency = sum(latencies) / len(latencies)
p95 = sorted(latencies)[18]
delta_p95_avg = p95 - avg_latency

log = {
    "total_cost": total_cost,
    "avg_latency": avg_latency,
    "p95": p95,
    "delta_p95_avg": delta_p95_avg,
}

with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
    f.write(json.dumps(log) + "\n")
