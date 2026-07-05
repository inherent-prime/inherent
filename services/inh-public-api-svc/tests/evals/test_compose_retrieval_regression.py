"""Compose-backed retrieval ranking regression (#33/#35).

Uploads the golden-corpus fixtures to the live local stack, then runs every
golden query and scores the ranking against the judged relevances with
recall@k / MRR / nDCG. Asserts a LOOSE baseline so it guards against gross
ranking regressions without flaking.

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

pytestmark = [pytest.mark.retrieval_eval, pytest.mark.compose]

# Where per-mode metrics are written for downstream reporting (#37). CI sets
# EVAL_REPORT; locally it defaults beside this test so a stray run is obvious.
EVAL_REPORT_PATH = os.environ.get(
    "EVAL_REPORT", str(Path(__file__).resolve().parent / "eval-report.json")
)
# Committed governance baseline (reporting only -- the diff is printed, not a
# new gate). See corpus/retrieval_baseline.json for the documented shape.
BASELINE_PATH = Path(__file__).resolve().parent / "corpus" / "retrieval_baseline.json"


def _load_baseline() -> dict[str, dict[str, float]]:
    try:
        raw = json.loads(BASELINE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    # Drop documentation keys (anything starting with "_").
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


def _write_and_summarize(summary: dict[str, dict[str, float]]) -> None:
    """Persist metrics to EVAL_REPORT and print a baseline-vs-current diff.

    Best-effort: never raises, so it cannot break the eval run itself.
    """
    try:
        Path(EVAL_REPORT_PATH).write_text(json.dumps(summary, indent=2, sort_keys=True))
        print(f"[retrieval-eval] wrote report to {EVAL_REPORT_PATH}")
    except OSError as exc:  # pragma: no cover - reporting is non-fatal
        print(f"[retrieval-eval] could not write report to {EVAL_REPORT_PATH}: {exc}")

    baseline = _load_baseline()
    print("[retrieval-eval] summary (current vs baseline):")
    for mode in sorted(summary):
        for metric in sorted(summary[mode]):
            cur = summary[mode][metric]
            base = baseline.get(mode, {}).get(metric)
            if base is None:
                print(f"  {mode}.{metric}: {cur:.3f} (no baseline)")
            else:
                delta = cur - base
                sign = "+" if delta >= 0 else ""
                print(f"  {mode}.{metric}: {cur:.3f} (baseline {base:.3f}, {sign}{delta:.3f})")


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

    # 2. Wait until the corpus is searchable (a known query returns results).
    probe = next(iter(queries.values()))
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if _search(client, probe, "semantic"):
            break
        time.sleep(3)
    else:
        pytest.fail(f"corpus not searchable within {TIMEOUT}s")

    # 3. Score each query per mode; report and assert a loose baseline.
    summary: dict[str, dict[str, float]] = {}
    for mode in ("semantic", "hybrid", "keyword"):
        recalls, mrrs, ndcgs = [], [], []
        for qid, query in queries.items():
            ranked = _search(client, query, mode, limit=5)
            recalls.append(recall_at_k(ranked, relevant[qid], k=5))
            mrrs.append(mrr(ranked, relevant[qid]))
            ndcgs.append(ndcg_at_k(ranked, relevance[qid], k=5))
        n = len(queries)
        summary[mode] = {
            "recall@5": sum(recalls) / n,
            "mrr": sum(mrrs) / n,
            "ndcg@5": sum(ndcgs) / n,
        }
        print(f"[retrieval-eval] {mode}: {summary[mode]}")

    # Reporting/governance (#37): persist metrics + print a baseline diff. This
    # is best-effort and does NOT introduce a new gate -- the only hard check is
    # the loose recall assertion below.
    _write_and_summarize(summary)

    # The best mode must clear the loose recall baseline.
    best_recall = max(s["recall@5"] for s in summary.values())
    assert (
        best_recall >= MIN_MEAN_RECALL_AT_5
    ), f"mean recall@5 regressed below {MIN_MEAN_RECALL_AT_5}: {summary}"
