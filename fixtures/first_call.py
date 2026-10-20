import json
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
request_id = uuid.uuid4().hex[:8]
t0 = time.perf_counter()
resp = client.chat.completions.create(
    model="gpt-4o-mini",
    temperature=0,
    messages=[{"role": "user", "content": "Say hello to Nimbus."}],
)
latency_ms = round((time.perf_counter() - t0) * 1000)

log = {
    "request_id": request_id,
    "reply": resp.choices[0].message.content,
    "prompt_tokens": resp.usage.prompt_tokens,
    "completion_tokens": resp.usage.completion_tokens,
    "latency_ms": latency_ms,
}

file_path = Path("../logs/runs.jsonl")
file_path.parent.mkdir(parents=True, exist_ok=True)

with open(file_path, "a", encoding="utf-8") as f:
    f.write(json.dumps(log) + "\n")
