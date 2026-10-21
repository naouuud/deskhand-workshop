"""Lab runner for checkpoint 03's index, without the planner.

Started by lab/server.py with cwd = checkpoints/03-rag. It imports the
checkpoint's own `retrieve` and `llm` modules unmodified. Four modes:

  {"mode": "search", "query": "...", "k": null | int, "doc_ids": null | [...]}
      `retrieve.retrieve()`, the same function `retrieve_policy` calls and the
      same one behind `py retrieve.py "<query>"`: one embedding call through
      `llm.embed_texts`, then the top-k sections from the local Qdrant index.
      With `doc_ids`, the filter runs INSIDE the store's search (Session 8,
      "filter first"); the runner also runs the unfiltered search so the lab
      can show what dropping rows afterwards would have left.

  {"mode": "distance", "phrases": ["refund", "money back", ...]}
      One `llm.embed_texts` call for 2 to 4 phrases, then every pair's cosine
      similarity (Session 7, "close means pointing the same way"). No index.

  {"mode": "map", "query": null | "..."}
      Every stored section's vector, projected to 2-D with PCA (numpy, no new
      dependency), one point per section; with a query, its embedding on the
      same axes plus the real top-k for it (Session 7, "meaning as distance").

  {"mode": "probe"}
      opens the index and counts its sections: no embedding call, no cost.

Qdrant's local mode lets ONE process open qdrant_data/. lab/server.py refuses
to start the index modes while the 03 service is up; if something else holds
the lock (an eval, a test, ingest.py), the error comes back as code
"index_locked". `distance` never opens the index, so it works any time.
"""

from __future__ import annotations

import sys
import time
import traceback

import labproto

# Third-party packages 03's llm, ingest and retrieve import, loaded while the
# runner waits warm (labproto.preload).
PRELOAD = (
    "openai",
    "anthropic",
    "tiktoken",
    "dotenv",
    "pydantic",
    "numpy",
    "qdrant_client",
    "langchain_core.documents",
    "langchain_openai",
    "langchain_anthropic",
    "langchain_qdrant",
    "langchain_text_splitters",
)


def _embed_info(llm, usage, started: float) -> dict:
    """The embedding side of every answer: provider, model, tokens, cost, runner time."""
    return {
        "embed_provider": getattr(llm, "EMBED_PROVIDER", None),
        "embed_model": getattr(llm, "EMBED_MODEL", None),
        "tokens": usage.prompt_tokens,
        "cost_usd": usage.cost_usd,
        "runner_ms": int((time.perf_counter() - started) * 1000),
    }


def search(req: dict, ch: labproto.Channel) -> None:
    """Top-k sections for a query, through checkpoint 03's own retrieve()."""
    import llm
    import retrieve

    started = time.perf_counter()
    k = req.get("k")
    doc_ids = [d for d in (req.get("doc_ids") or []) if d] or None
    chunks, usage = retrieve.retrieve(req["query"], k=k, doc_ids=doc_ids)
    unfiltered = None
    if doc_ids:
        # The comparison the slide draws: search everything, drop rows after.
        # A second call, so a second (tiny) embedding; both are on the bill.
        unfiltered, usage2 = retrieve.retrieve(req["query"], k=k)
        usage = llm.Usage(
            prompt_tokens=usage.prompt_tokens + usage2.prompt_tokens,
            completion_tokens=0,
            cost_usd=usage.cost_usd + usage2.cost_usd,
        )
    ch.emit(
        "search",
        checkpoint="03",
        query=req["query"],
        k=k if k is not None else retrieve.TOP_K,
        top_k=retrieve.TOP_K,
        doc_ids=doc_ids,
        chunks=chunks,
        unfiltered=unfiltered,
        file=retrieve.__file__,
        **_embed_info(llm, usage, started),
    )


def distance(req: dict, ch: labproto.Channel) -> None:
    """Pairwise cosine similarity of 2 to 4 phrases, from one embedding call."""
    import numpy as np

    import llm

    started = time.perf_counter()
    phrases = [str(p).strip() for p in req.get("phrases") or [] if str(p).strip()]
    if not 2 <= len(phrases) <= 4:
        raise ValueError("distance needs 2 to 4 phrases")
    vectors, usage = llm.embed_texts(phrases)
    matrix = np.asarray(vectors, dtype=float)
    # Cosine: normalise explicitly instead of trusting the provider's unit length.
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    sims = (
        matrix @ matrix.T
    )  # unit vectors: each dot product is a cosine, 1.0 = same direction
    pairs = [
        {"a": i, "b": j, "score": round(float(sims[i, j]), 4)}
        for i in range(len(phrases))
        for j in range(i + 1, len(phrases))
    ]
    pairs.sort(key=lambda p: -p["score"])
    ch.emit(
        "distance",
        checkpoint="03",
        phrases=phrases,
        pairs=pairs,
        dims=int(matrix.shape[1]),
        file=llm.__file__,
        **_embed_info(llm, usage, started),
    )


def map_index(req: dict, ch: labproto.Channel) -> None:
    """Every stored section on two PCA axes, plus a query's point and its top-k."""
    import numpy as np

    import ingest
    import llm
    import retrieve

    started = time.perf_counter()
    store = (
        retrieve._get_store()
    )  # opens qdrant_data/ and takes its lock for this process
    records, _ = store.client.scroll(
        # 1000 is far more than the index holds, so one page is every section.
        ingest.COLLECTION,
        limit=1000,
        with_vectors=True,
        with_payload=True,
    )
    if len(records) < 3:
        raise RuntimeError("the index holds fewer than 3 sections; run `py ingest.py`")
    vectors = np.asarray([r.vector for r in records], dtype=float)
    mean = vectors.mean(axis=0)
    centred = vectors - mean
    # PCA by SVD: the two directions along which the 27 sections differ most.
    _, singular, components = np.linalg.svd(centred, full_matrices=False)
    basis = components[:2].T  # (dims, 2)
    coords = centred @ basis
    total = float((singular**2).sum()) or 1.0
    # Share of the spread each axis shows: the rest is lost in the flattening to 2-D.
    explained = [round(float(s * s / total), 4) for s in singular[:2]]
    points = []
    for record, (x, y) in zip(records, coords):
        meta = record.payload.get("metadata") or {}
        if "doc_id" not in meta:
            raise RuntimeError(
                "the index was built by an older ingest.py — re-run `py ingest.py`"
            )
        points.append(
            {
                "doc_id": meta["doc_id"],
                "section_id": meta["section_id"],
                "x": round(float(x), 5),
                "y": round(float(y), 5),
            }
        )

    query = (req.get("query") or "").strip() or None
    query_point = None
    nearest: list[dict] = []
    usage = llm.usage_for(llm.EMBED_MODEL, 0, 0)
    if query:
        # One embedding, used twice: projected onto the same axes, and
        # searched with the store's own call (what retrieve() does).
        (vector,), usage = llm.embed_texts([query])
        qx, qy = (np.asarray(vector, dtype=float) - mean) @ basis
        query_point = {
            "text": query,
            "x": round(float(qx), 5),
            "y": round(float(qy), 5),
        }
        hits = store.similarity_search_with_score_by_vector(vector, k=retrieve.TOP_K)
        nearest = [
            retrieve._chunk(doc, score) for doc, score in hits
        ]  # the dict shape retrieve() returns
    ch.emit(
        "map",
        checkpoint="03",
        points=points,
        query=query_point,
        nearest=nearest,
        top_k=retrieve.TOP_K,
        explained=explained,
        dims=int(vectors.shape[1]),
        file=retrieve.__file__,
        **_embed_info(llm, usage, started),
    )


def probe(ch: labproto.Channel) -> None:
    """Open the index and count its sections. No embedding call, no cost."""
    import ingest
    import retrieve

    store = retrieve._get_store()
    count = store.client.count(ingest.COLLECTION).count
    ch.emit(
        "search",
        checkpoint="03",
        probe=True,
        sections=count,
        path=str(ingest.QDRANT_PATH),
    )


MODES = {
    "search": search,
    "distance": distance,
    "map": map_index,
}  # probe takes no request: dispatched apart


def main() -> None:
    labproto.preload(PRELOAD)
    req = labproto.read_request()
    ch = labproto.Channel()
    labproto.use_checkpoint_dir()
    sys.stdout = labproto.CapturedStdout(ch, live=False)
    mode = req.get("mode") or "search"
    try:
        if mode == "probe":
            probe(ch)
        else:
            MODES[mode](req, ch)
    except BaseException:
        detail = traceback.format_exc()
        # Qdrant's own message when another process holds qdrant_data/.
        # server.py turns code "index_locked" into a plain-language answer.
        locked = "already accessed by another instance of Qdrant" in detail
        ch.emit(
            "error",
            message="the policy index is locked by another process"
            if locked
            else f"{mode} failed",
            code="index_locked" if locked else None,
            detail=detail,
        )


if __name__ == "__main__":
    main()
