"""Pick the workflow runtime: the plain runner or LangGraph.

    DESKHAND_RUNTIME=plain      graph.py     a dict of nodes and a while loop (default)
    DESKHAND_RUNTIME=langgraph  graph_lg.py  the same nodes on a LangGraph StateGraph

Both modules expose the same names (ENTRY, run, run_in_thread, resume,
resume_after_approval, resume_stuck_runs), so app.py does not care which one
it gets. The import is lazy: the plain default never imports langgraph.
"""
from __future__ import annotations

import importlib
import os
from types import ModuleType

from dotenv import load_dotenv

# llm.py loads the repo .env too, but only once graph is imported, which is
# after this module reads DESKHAND_RUNTIME. A shell variable still wins.
load_dotenv()

# DESKHAND_RUNTIME value -> module name. Same nodes.py, same store.py, two
# different loops around them.
RUNTIMES = {"plain": "graph", "langgraph": "graph_lg"}


def load(name: str | None = None) -> ModuleType:
    """Import and return one runtime module: `name`, else DESKHAND_RUNTIME,
    else plain. An unknown name raises instead of falling back, so a typo in
    .env stops the service at startup."""
    name = (name or os.getenv("DESKHAND_RUNTIME") or "plain").strip().lower()
    if name not in RUNTIMES:
        raise ValueError(
            f"DESKHAND_RUNTIME={name!r} is not a runtime; use one of "
            + ", ".join(sorted(RUNTIMES))
        )
    return importlib.import_module(RUNTIMES[name])


# Resolved once, at import. app.py does `from runtime import graph`, so a
# change to DESKHAND_RUNTIME needs a restart.
graph = load()
# "plain" or "langgraph". app.py logs it at startup (service.runtime), so the
# log says which loop drove the runs that follow.
NAME = next(key for key, module in RUNTIMES.items() if module == graph.__name__)
