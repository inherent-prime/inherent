"""Compose-backed retrieval ranking regression (#33/#35, hard-gated per #37).

Uploads the golden-corpus fixtures to the live local stack, then runs every
golden query and scores the ranking against the judged relevances with
recall@k / MRR / nDCG. Two gates apply:

1. Relative: no per-mode metric may regress more than its *effective*
   tolerance below the committed baseline (``corpus/retrieval_baseline.json``),
   enforced via ``tests/evals/eval_gate.py``. ``EVAL_GATE_TOLERANCE`` is a
   FLOOR, not the tolerance itself: the actual per-metric tolerance is
   ``max(EVAL_GATE_TOLERANCE, min_detectable_delta(metric, n))``, where ``n``
   is the number of gated golden queries (every query except
   ``category == "abstention"``, matching the pooled-average exclusion
   below). This closes #236: with a small golden corpus, a single query's
   rank slipping by one position can move a pooled metric by more than a
   fixed 0.02 tolerance, so a fixed tolerance hard-fails on noise the corpus
   is too small to actually resolve -- see the 2026-08-12 amendment to ADR
   0003. A green run on `main` ratchets the baseline up (never down) -- see
   ``.github/workflows/integration.yml``.
2. Absolute: a LOOSE backstop floor so a fresh checkout with an unset/zeroed
   baseline still guards against gross regressions.

Marked ``retrieval_eval`` + ``compose`` — deselected by default; run against a
live stack with: ``make dev`` then ``uv run pytest -m 'retrieval_eval and compose'``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from src.services.ranking_metrics import mrr, ndcg_at_k, recall_at_k
from tests.evals.eval_gate import (
    effective_tolerance,
    find_regressions,
    format_regressions,
    load_metrics,
)

# ``eval_gate`` is what CI's hard-gate step selects on, and this is the ONLY
# module that carries it: a failure here means a ranking metric regressed vs
# the committed baseline, which is what the gate's error message and the
# auto-filed regression issue both assert. Sibling evals tests keep
# ``retrieval_eval`` alone so their failures are reported as themselves (#240).
pytestmark = [pytest.mark.retrieval_eval, pytest.mark.eval_gate, pytest.mark.compose]

# Where per-mode metrics are written for downstream reporting (#37) and for the
# CI ratchet step to read after this test passes. CI sets EVAL_REPORT; locally
# it defaults beside this test so a stray run is obvious.
EVAL_REPORT_PATH = os.environ.get(
    "EVAL_REPORT", str(Path(__file__).resolve().parent / "eval-report.json")
)
# Committed governance baseline. The gate (below) hard-fails on any per-mode
# metric that regresses beyond its effective tolerance; a green run on `main`
# ratchets this file up to the higher of (current, baseline) -- see
# tests/evals/eval_gate.py and .github/workflows/integration.yml.
BASELINE_PATH = Path(__file__).resolve().parent / "corpus" / "retrieval_baseline.json"
# The FLOOR under the derived per-metric tolerance (#236) -- see the module
# docstring. Named EVAL_GATE_TOLERANCE for backward compatibility: this is
# the same env var CI/docs have always referenced, its meaning as a lower
# bound is unchanged, it just no longer doubles as the tolerance itself.
EVAL_GATE_TOLERANCE = float(os.environ.get("EVAL_GATE_TOLERANCE", "0.02"))


def _write_and_summarize(summary: dict[str, dict[str, float]], num_queries: int) -> list:
    """Persist metrics to EVAL_REPORT, print a baseline diff, return regressions.

    Writing the report is best-effort (never raises, so it cannot break the
    eval run itself); computing regressions is not -- the caller asserts on it.
    ``num_queries`` is the gated golden-query count (abstention excluded, see
    module docstring) used to derive each metric's effective tolerance (#236).
    """
    try:
        Path(EVAL_REPORT_PATH).write_text(json.dumps(summary, indent=2, sort_keys=True))
        print(f"[retrieval-eval] wrote report to {EVAL_REPORT_PATH}")
    except OSError as exc:  # pragma: no cover - reporting is non-fatal
        print(f"[retrieval-eval] could not write report to {EVAL_REPORT_PATH}: {exc}")

    baseline = load_metrics(BASELINE_PATH)
    print("[retrieval-eval] summary (current vs baseline):")
    # "_"-prefixed keys (e.g. "_by_category") are supplementary reporting, not
    # per-mode metrics -- same convention as "_comment" in the baseline file.
    for mode in sorted(m for m in summary if not m.startswith("_")):
        for metric in sorted(summary[mode]):
            cur = summary[mode][metric]
            base = baseline.get(mode, {}).get(metric)
            if base is None:
                print(f"  {mode}.{metric}: {cur:.3f} (no baseline)")
            else:
                delta = cur - base
                sign = "+" if delta >= 0 else ""
                print(f"  {mode}.{metric}: {cur:.3f} (baseline {base:.3f}, {sign}{delta:.3f})")

    # Per-metric tolerance derived from corpus resolution (#236): the metric
    # names the baseline actually tracks, gated at max(EVAL_GATE_TOLERANCE,
    # min_detectable_delta(metric, num_queries)).
    metric_names = {metric for metrics in baseline.values() for metric in metrics}
    tolerances = {
        metric: effective_tolerance(metric, num_queries, floor=EVAL_GATE_TOLERANCE)
        for metric in metric_names
    }
    print(
        "[retrieval-eval] effective tolerances "
        f"(n={num_queries}, floor={EVAL_GATE_TOLERANCE}): {tolerances}"
    )
    regressions = find_regressions(summary, baseline, tolerance=tolerances)
    print(format_regressions(regressions))
    return regressions


API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))
HEADERS = {"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID}

# Loose regression guard, NOT a quality target. Calibrated below the measured
# fresh-stack baseline (best mode ~0.22 mean recall@5 on this small corpus with
# bge-small) so it catches a real drop without flaking; ratchet it up as
# retrieval improves (see #45/#47 eval-gate policy). Override via env.
MIN_MEAN_RECALL_AT_5 = float(os.environ.get("RETRIEVAL_MIN_RECALL5", "0.15"))


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(timeout=30) as c:
        try:
            resp = c.get(f"{API_URL}/health", timeout=5)
        except httpx.HTTPError as exc:
            pytest.skip(f"public API not reachable at {API_URL}: {exc}")
        if resp.status_code != 200:
            pytest.skip(f"public API unhealthy: HTTP {resp.status_code}")
        yield c


def _content_type(filename: str) -> str:
    return {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".html": "text/html",
        ".json": "application/json",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(os.path.splitext(filename)[1], "application/octet-stream")


def _search(client: httpx.Client, query: str, mode: str, limit: int = 5) -> list[str]:
    resp = client.post(
        f"{API_URL}/v1/search",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": query, "limit": limit, "search_mode": mode},
    )
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    # Rank by document_name (the original filename), which is what qrels key on.
    return [r["document_name"] for r in resp.json()["results"]]


def test_ranking_regression_against_golden_corpus(client, golden_corpus):
    sample_dir = golden_corpus["sample_dir"]
    queries = golden_corpus["queries"]
    relevant = golden_corpus["relevant"]
    relevance = golden_corpus["relevance"]
    category = golden_corpus["category"]

    # 1. Upload every fixture the corpus references (dedup; reuse = reindex).
    fixtures = {j["document_id"] for j in golden_corpus["judgments"]}
    for fn in sorted(fixtures):
        path = sample_dir / fn
        assert path.exists(), f"missing fixture {path}"
        with path.open("rb") as fh:
            up = client.post(
                f"{API_URL}/v1/documents",
                headers=HEADERS,
                files={"file": (fn, fh, _content_type(fn))},
            )
        assert up.status_code == 201, f"upload {fn} failed: {up.status_code} {up.text}"

    # 2. Wait until the corpus is searchable. Checking only ONE probe query
    # (the historical behavior) is a race: that single probe's own document
    # can finish its extract->chunk->embed->index pipeline well before the
    # rest of the corpus does, especially on a slow/CPU-only embedding
    # runner, so scoring below can start while most documents are still
    # mid-pipeline -- every query against a not-yet-indexed document then
    # legitimately scores zero, not because ranking is bad but because
    # nothing was there yet to rank. This produced a spuriously low
    # measurement in the past (corpus/retrieval_history.jsonl's first entry,
    # sha 201363a, pooled recall@5 ~0.21 across every mode -- a uniformity
    # consistent with only a handful of documents being ready when scoring
    # started, not a real ranking-quality signal) and was directly observed
    # for a newly-added query during #146 development (see ADR 0004): the
    # first eval run after adding two new fixtures scored 0/0/0 on that
    # query, and an immediate re-run (once processing caught up) scored a
    # perfect 1.0/1.0/1.0 with no code change in between. Every query must
    # independently surface its own judged-relevant document (or, for
    # abstention queries with no relevant document by construction, any
    # non-empty result) before scoring starts.
    def _query_ready(query: str, relevant_ids: set[str]) -> bool:
        ranked = _search(client, query, "semantic", limit=20)
        if not relevant_ids:
            return bool(ranked)  # abstention: any result means the index is live
        # ALL judged-relevant docs, not just one -- a multi-relevant query
        # (e.g. multi_doc_crowding's q14, judged against two documents) is
        # otherwise marked ready the moment the first of its relevant docs
        # indexes, while the second is still mid-pipeline, reintroducing the
        # exact race this function exists to close (#146 cross-review).
        return relevant_ids.issubset(set(ranked))

    deadline = time.monotonic() + TIMEOUT
    unready = set(queries)
    while unready and time.monotonic() < deadline:
        for qid in list(unready):
            if _query_ready(queries[qid], relevant.get(qid, set())):
                unready.discard(qid)
        if unready:
            time.sleep(3)
    if unready:
        pytest.fail(
            f"corpus not fully searchable within {TIMEOUT}s -- still not ready: "
            f"{sorted(unready)}"
        )

    # 3. Score each query per mode; report and assert a loose baseline. Also
    # break scores down per query category (#37 corpus expansion) -- reporting
    # only, not gated, so it can't flake on a single thin category while still
    # giving visibility into which archetype (exact-id/stale/paraphrase) is
    # dragging a mode down.
    #
    # "abstention" queries are scored (and shown in the by-category breakdown)
    # but excluded from the pooled recall/MRR/nDCG averages below: they have no
    # relevant document by construction, so recall_at_k/mrr/ndcg_at_k always
    # return 0.0 for them regardless of ranking quality. Pooling that
    # structural zero into an average meant to track "is retrieval good" would
    # just dilute the signal on every mode equally, not catch a regression.
    summary: dict[str, dict[str, float]] = {}
    by_category: dict[str, dict[str, dict[str, list[float]]]] = {}
    for mode in ("semantic", "hybrid", "keyword"):
        recalls, mrrs, ndcgs = [], [], []
        for qid, query in queries.items():
            ranked = _search(client, query, mode, limit=5)
            relevant_ids = relevant.get(qid, set())
            r, m, n_ = (
                recall_at_k(ranked, relevant_ids, k=5),
                mrr(ranked, relevant_ids),
                ndcg_at_k(ranked, relevance[qid], k=5),
            )
            cat_bucket = by_category.setdefault(mode, {}).setdefault(
                category.get(qid, "general"), {"recall@5": [], "mrr": [], "ndcg@5": []}
            )
            cat_bucket["recall@5"].append(r)
            cat_bucket["mrr"].append(m)
            cat_bucket["ndcg@5"].append(n_)
            if category.get(qid) == "abstention":
                continue
            recalls.append(r)
            mrrs.append(m)
            ndcgs.append(n_)
        n = len(recalls)
        summary[mode] = {
            "recall@5": sum(recalls) / n,
            "mrr": sum(mrrs) / n,
            "ndcg@5": sum(ndcgs) / n,
        }
        print(f"[retrieval-eval] {mode}: {summary[mode]}")

    category_summary = {
        mode: {
            cat: {metric: sum(values) / len(values) for metric, values in metrics.items()}
            for cat, metrics in cats.items()
        }
        for mode, cats in by_category.items()
    }
    print(f"[retrieval-eval] by category: {json.dumps(category_summary, indent=2)}")

    # Persist metrics + print a baseline diff, then hard-gate on regressions
    # (#37 -> hard gate): any per-mode metric that drops more than its
    # effective tolerance below the committed baseline fails the build (#236 --
    # see module docstring). A green run on `main` ratchets the baseline up; it
    # never moves down. The "_by_category" key is prefixed so eval_gate.py's
    # loader drops it (same convention as "_comment" in
    # retrieval_baseline.json) -- reporting only, never part of the enforced
    # gate. `n` (the gated query count -- every category but "abstention",
    # identical across modes) came out of the loop above; it's the same pool
    # the pooled averages themselves were computed over, so the tolerance is
    # derived from exactly the corpus resolution this run actually measured.
    regressions = _write_and_summarize({**summary, "_by_category": category_summary}, n)
    assert not regressions, format_regressions(regressions)

    # Absolute floor as a backstop under the relative gate above -- catches a
    # collapse even on the first run, before any baseline has been set.
    best_recall = max(s["recall@5"] for s in summary.values())
    assert (
        best_recall >= MIN_MEAN_RECALL_AT_5
    ), f"mean recall@5 regressed below {MIN_MEAN_RECALL_AT_5}: {summary}"
