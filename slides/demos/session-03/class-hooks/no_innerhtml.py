"""Hook 2 - PostToolUse reviewer: flag innerHTML in JS/HTML that Claude just wrote.

Claude Code runs this AFTER every Edit or Write. The file is already saved, so
this hook can't stop the change. Exit 2 sends our stderr back to Claude as
feedback, and Claude fixes its own code in the same turn.

The point: a repo rule ("render untrusted text with textContent, never
innerHTML") gets checked on every edit, not just when someone remembers it.
"""
import json
import sys

call = json.loads(sys.stdin.buffer.read().decode("utf-8"))
args = call.get("tool_input", {})
path = args.get("file_path", "")

if not path.endswith((".js", ".html")):
    sys.exit(0)

# Only the text written in this call: Write sends "content", Edit sends "new_string".
written = args.get("content") or args.get("new_string") or ""
hits = [line.strip() for line in written.splitlines() if "innerHTML" in line]

if hits:
    print(
        f"no_innerhtml hook: {path} now uses innerHTML:\n  " + "\n  ".join(hits) + "\n"
        "Tickets and model output can carry prompt-injection payloads, so this repo renders "
        "them with textContent (L.el in the lab). Rewrite these lines to use textContent.",
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
