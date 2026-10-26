"""Hook 1 - PreToolUse guard: Claude may never read, edit or cat a .env file.

Claude Code runs this BEFORE every matching tool call and pipes the call in as
JSON on stdin. Exit 0 lets the call run. Exit 2 blocks it, and whatever we print
to stderr goes back to Claude as the reason.

The point: the model doesn't decide whether it may touch secrets. Code does.
"""
import json
import re
import sys

# ".env" or ".env.local", but not ".venv", "dotenv" or "os.environ".
ENV_FILE = re.compile(r"(?:^|[\s/\\\"'=:])\.env(?:\.([A-Za-z0-9_-]+))?(?![A-Za-z0-9_])")
SAFE_SUFFIXES = {"example", "sample", "template"}

call = json.loads(sys.stdin.buffer.read().decode("utf-8"))
tool = call.get("tool_name", "")
args = call.get("tool_input", {})

# Each tool keeps its target in a different field.
target = " ".join(str(args.get(k, "")) for k in ("file_path", "path", "command"))

for match in ENV_FILE.finditer(target):
    if match.group(1) not in SAFE_SUFFIXES:
        print(
            f"Blocked by guard_env hook: {tool} touches a .env file, which holds API keys. "
            "Ask the user to check the value themselves, or use .env.example for the variable names.",
            file=sys.stderr,
        )
        sys.exit(2)

sys.exit(0)
