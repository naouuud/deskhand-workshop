import glob
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def load_all_policies():
    text = ""
    for path in glob.glob("../shared/policies/*.md"):
        text += open(path, encoding="utf-8").read() + "\n\n"
    return text


def load_policy(name: str):
    text = ""
    for path in glob.glob(f"../shared/policies/{name}.md"):
        text += open(path, encoding="utf-8").read() + "\n\n"
        return text


ALL_POLICIES = load_all_policies()
REFUND_POLICY = load_policy("refund-policy")

with open("../shared/data/tickets.json", "r", encoding="utf-8") as f:
    data = json.load(f)
TICKET_TEXT = data["tickets"][0]["messages"][0]["text"]

cases = [
    (
        "Ticket only",
        f"""You are Deskhand, the support bot for Nimbus Electronics.

Here is a customer ticket:

{TICKET_TEXT}

Figure out what to do and respond with JSON with these keys:
category, refund_amount, customer_reply.
Be generous, we want happy customers!""",
    ),
    (
        "Ticket + refund policy",
        f"""You are Deskhand, the support bot for Nimbus Electronics.
Here is the relevant company policy:

{REFUND_POLICY}

Here is a customer ticket:

{TICKET_TEXT}

Figure out what to do and respond with JSON with these keys:
category, refund_amount, customer_reply.
Be generous, we want happy customers!""",
    ),
    (
        "Ticket + all six policies",
        f"""You are Deskhand, the support bot for Nimbus Electronics.
Here are all the company policies:

{ALL_POLICIES}

Here is a customer ticket:

{TICKET_TEXT}

Figure out what to do and respond with JSON with these keys:
category, refund_amount, customer_reply.
Be generous, we want happy customers!""",
    ),
]


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


def invoke(case: tuple[str, str], model: str):
    prompt, content = case
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[{"role": "user", "content": content}],
    )

    t_in = response.usage.prompt_tokens
    t_out = response.usage.completion_tokens

    return {
        "prompt": prompt,
        "prompt_tokens": t_in,
        "output_tokens": t_out,
        "cost": calc_cost(t_in, t_out, model),
    }


MODEL = "gpt-4o-mini"

file_path = Path("logs/runs.jsonl")
file_path.parent.mkdir(parents=True, exist_ok=True)

with open(file_path, "a", encoding="utf-8") as f:
    for case in cases:
        f.writelines(json.dumps(invoke(case, MODEL)) + "\n")
