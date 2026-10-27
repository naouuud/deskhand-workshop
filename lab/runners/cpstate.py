"""Lab runner for a service's saved state: which runs a restart would pick up.

Started by lab/server.py with cwd = checkpoints/02-service, 03-rag or
04-production. It imports the checkpoint's own `store` module unmodified and
calls the function the service calls at startup, through resume_stuck_runs:

  {"mode": "resumable", "db_path": "<that checkpoint's runs.db>"}
      `store.find_resumable(db_path)`: every run that is not finished and not
      waiting for a person, with the node it would restart at. Nothing is
      resumed here; the answer is only the list.

The lab reads the rows itself, read only. It asks this runner only for the
rule, so the Saved state page says what the checkpoint's code says, not a copy
of it. server.py calls it only when runs.db exists: store's connection would
create the file otherwise.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import labproto

# store imports models, which imports pydantic: loaded while the runner waits warm.
PRELOAD = ("pydantic",)


def resumable(req: dict, ch: labproto.Channel) -> None:
    """The runs a restart would pick up, as find_resumable returns them."""
    import store

    found = store.find_resumable(db_path=Path(req["db_path"]))
    ch.emit("resumable", runs=[{"request_id": state.request_id, "status": state.status, "next_node": next_node}
                               for state, next_node in found])


MODES = {"resumable": resumable}


def main() -> None:
    labproto.preload(PRELOAD)
    req = labproto.read_request()
    ch = labproto.Channel()
    labproto.use_checkpoint_dir()
    sys.stdout = labproto.CapturedStdout(ch, live=False)
    mode = req.get("mode") or "resumable"
    try:
        MODES[mode](req, ch)
    except BaseException:
        ch.emit("error", message=f"{mode} failed", detail=traceback.format_exc())


if __name__ == "__main__":
    main()
