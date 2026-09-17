"""Baseline-comparison CLI for the retrieval-eval gate (#37 -> hard gate).

Compares a retrieval-eval run's metrics (``eval-report.json``, written by
``test_compose_retrieval_regression.py``) against the committed governance
baseline (``corpus/retrieval_baseline.json``) and fails when any per-mode
metric regresses beyond a small tolerance. Previously the baseline diff was
print-only (reporting, not a gate); this makes "beats baseline" an enforced
CI contract instead of a number a human has to notice.

The comparison/ratchet functions are pure and dependency-free (stdlib only,
matching ``ranking_metrics.py``'s convention) so they are unit-tested offline
in ``test_eval_gate.py`` and also imported directly by
``test_compose_retrieval_regression.py`` for the live-stack hard-gate assertion.

The CLI is what CI actually invokes end to end::

    # Fail (exit 1) if the just-produced report regressed vs the committed
    # baseline; used as a standalone check step. --tolerance is a FLOOR: pass
    # --num-queries (or --qrels, to derive it) to also gate at
    # max(--tolerance, per-metric minimum detectable single-query delta) --
    # see effective_tolerance() (#236). Omit both and --tolerance is used
    # as-is, unchanged from before #236.
    uv run python tests/evals/eval_gate.py check \\
        --report eval-report.json --baseline tests/evals/corpus/retrieval_baseline.json \\
        --qrels tests/evals/corpus/qrels.jsonl

    # Ratchet the committed baseline up to the higher of (current, baseline)
    # per mode/metric; used only after a green gate on `main` (#37/#45 policy:
    # the baseline only ever moves up, never down).
    uv run python tests/evals/eval_gate.py ratchet \\
        --report eval-report.json --baseline tests/evals/corpus/retrieval_baseline.json \\
        --out tests/evals/corpus/retrieval_baseline.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Union

MetricsByMode = dict[str, dict[str, float]]
Tolerance = Union[float, Mapping[str, float]]

DEFAULT_TOLERANCE = 0.02


def load_metrics(path: Path) -> MetricsByMode:
    """Parse a per-mode metrics JSON file.

    Drops documentation keys (anything starting with ``_``, e.g. ``_comment``)
    and any non-dict values. Returns ``{}`` if the file is missing or not valid
    JSON, mirroring the existing best-effort baseline loader in
    ``test_compose_retrieval_regression.py``.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


def load_doc_keys(path: Path) -> dict:
    """Return only the ``_``-prefixed documentation keys from a metrics file.

    The ratchet writes a fresh baseline from the per-mode metrics alone, so
    without carrying these forward the file's ``_comment`` (which documents the
    hard-gate + ratchet policy) would be silently dropped on the first ratchet.
    Best-effort: an unreadable/invalid file just yields no doc keys.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if k.startswith("_")}


def load_qrels_query_count(path: Path, *, excluded_category: str = "abstention") -> int:
    """Count distinct gated queries in a ``qrels.jsonl`` file (#236).

    A query's category is its first-seen ``category`` field across its
    (possibly multiple) judgment lines, defaulting to ``"general"`` -- this
    mirrors ``conftest.py``'s ``golden_corpus`` fixture exactly. Any query in
    ``excluded_category`` (default ``"abstention"``) is dropped: those queries
    have no relevant document by construction, so recall/MRR/nDCG are
    structurally 0.0 for them regardless of ranking quality, and
    ``test_compose_retrieval_regression.py`` already excludes them from the
    pooled averages the gate compares against the baseline. Counting them
    here would derive a tolerance from a query the gate doesn't actually
    score. Best-effort: an unreadable/invalid file yields ``0``.
    """
    try:
        raw = path.read_text()
    except OSError:
        return 0
    categories: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            judgment = json.loads(line)
        except ValueError:
            continue
        qid = judgment.get("query_id")
        if qid is None:
            continue
        categories.setdefault(qid, judgment.get("category", "general"))
    return sum(1 for category in categories.values() if category != excluded_category)


def min_detectable_delta(metric: str, num_queries: int) -> float:
    """The smallest possible single-query move for ``metric`` averaged over ``num_queries``.

    A fixed absolute tolerance can be finer than a pooled metric's actual
    resolution: with ``n`` gated golden queries, one query changing rank
    moves the pooled average by a step no smaller than this (#236 -- see
    ``docs/adr/0003-traffic-mined-retrieval-evals.md``'s 2026-08-12
    amendment). Below that step, the gate cannot distinguish "one document
    moved one rank position" from "retrieval regressed": the two produce
    overlapping numbers.

    Metric family is inferred from the metric name (matching
    ``retrieval_baseline.json``'s actual keys, e.g. ``"mrr"``,
    ``"recall@5"``, ``"ndcg@5"`` -- the ``@k`` suffix, if any, does not
    change the formula):

    - ``mrr``: a single query's reciprocal rank can move at minimum from
      ``1/1`` to ``1/2`` (rank 1 -> rank 2), a step of ``0.5``.
    - ``recall@k``: a single query gaining or losing one relevant document
      moves recall by ``1`` (conservative, single-relevant-document case).
    - ``ndcg@k``: the smallest possible move is the top-2 positions
      swapping, worth ``1 - 1/log2(3)`` of that query's (normalized) score.
    """
    if num_queries <= 0:
        raise ValueError(f"num_queries must be positive, got {num_queries!r}")
    if metric == "mrr":
        single_query_step = 0.5
    elif metric.startswith("recall"):
        single_query_step = 1.0
    elif metric.startswith("ndcg"):
        single_query_step = 1 - 1 / math.log2(3)
    else:
        raise ValueError(f"unrecognized metric family: {metric!r}")
    return single_query_step / num_queries


def effective_tolerance(metric: str, num_queries: int, floor: float = DEFAULT_TOLERANCE) -> float:
    """The tolerance to gate ``metric`` with: ``max(floor, min_detectable_delta(...))``.

    ``floor`` (``EVAL_GATE_TOLERANCE`` at the CLI/gate boundary) keeps its
    prior meaning as a lower bound -- on a large enough corpus the floor
    still dominates -- but never lets the gate be stricter than the corpus
    can actually resolve.
    """
    return max(floor, min_detectable_delta(metric, num_queries))


@dataclass(frozen=True)
class Regression:
    """A single (mode, metric) that dropped beyond tolerance vs. the baseline."""

    mode: str
    metric: str
    current: float
    baseline: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline


def find_regressions(
    current: MetricsByMode,
    baseline: MetricsByMode,
    tolerance: Tolerance = DEFAULT_TOLERANCE,
) -> list[Regression]:
    """Return every (mode, metric) the baseline tracks that regressed beyond ``tolerance``.

    Only metrics present in the *baseline* are checked: a metric the baseline
    doesn't track yet (e.g. a newly added mode) has nothing to regress against.
    A metric the baseline tracks but the current run didn't produce is treated
    as ``0.0`` -- a broken or incomplete eval run must not silently pass the
    gate by omission.

    ``tolerance`` is either a single flat float applied to every metric (the
    original behavior), or a ``{metric: tolerance}`` mapping -- e.g. built
    from ``effective_tolerance`` per metric (#236) -- applied per metric name
    across every mode. A metric missing from the mapping falls back to
    ``DEFAULT_TOLERANCE``.
    """
    regressions = []
    for mode, metrics in baseline.items():
        current_mode = current.get(mode, {})
        for metric, baseline_value in metrics.items():
            current_value = current_mode.get(metric, 0.0)
            metric_tolerance = (
                tolerance.get(metric, DEFAULT_TOLERANCE)
                if isinstance(tolerance, Mapping)
                else tolerance
            )
            if current_value < baseline_value - metric_tolerance:
                regressions.append(
                    Regression(mode, metric, current=current_value, baseline=baseline_value)
                )
    return regressions


def ratchet_baseline(current: MetricsByMode, baseline: MetricsByMode) -> MetricsByMode:
    """Return a new baseline that never decreases: ``max(current, baseline)`` per metric.

    Union of modes/metrics from both sides, so a new mode or metric in
    ``current`` is adopted and one that disappeared from ``current`` keeps its
    prior baseline value untouched.
    """
    updated: MetricsByMode = {}
    for mode in sorted(set(current) | set(baseline)):
        current_mode = current.get(mode, {})
        baseline_mode = baseline.get(mode, {})
        updated[mode] = {
            metric: max(current_mode.get(metric, 0.0), baseline_mode.get(metric, 0.0))
            for metric in sorted(set(current_mode) | set(baseline_mode))
        }
    return updated


def format_regressions(regressions: list[Regression]) -> str:
    """Human-readable summary for CI logs / step summaries."""
    if not regressions:
        return "[eval-gate] no regressions vs baseline."
    lines = ["[eval-gate] regressions vs baseline:"]
    for reg in sorted(regressions, key=lambda r: (r.mode, r.metric)):
        lines.append(
            f"  {reg.mode}.{reg.metric}: {reg.current:.3f} "
            f"(baseline {reg.baseline:.3f}, {reg.delta:+.3f})"
        )
    return "\n".join(lines)


def _resolve_check_tolerance(args: argparse.Namespace, baseline: MetricsByMode) -> Tolerance:
    """Derive per-metric tolerance from corpus resolution when asked to (#236).

    ``--tolerance`` is the floor either way. Derivation is requested
    explicitly via ``--num-queries`` (direct) or ``--qrels`` (count gated
    queries from a qrels file, excluding ``category == "abstention"``); with
    neither, ``--tolerance`` is used as the original flat value for every
    metric, unchanged from before #236.

    An explicit ``--num-queries``/``--qrels`` that resolves to zero (a
    missing/invalid/empty qrels file, an all-abstention corpus, or a literal
    ``--num-queries 0``) is a caller error, not a reason to quietly fall back
    to the flat ``--tolerance``: opting into derivation and silently getting
    the un-derived behavior instead is exactly the kind of surprise this gate
    exists to prevent. Raises ``ValueError`` in that case; the caller decides
    how to surface it. Omitting both flags is unaffected -- that is the
    intentional "keep the pre-#236 flat tolerance" path.
    """
    num_queries = args.num_queries
    qrels_source: str | None = None
    if num_queries is None and args.qrels:
        qrels_source = args.qrels
        num_queries = load_qrels_query_count(Path(args.qrels))

    explicitly_requested = qrels_source is not None or args.num_queries is not None
    if explicitly_requested and not num_queries:
        source = f"--qrels {qrels_source!r}" if qrels_source else "--num-queries"
        raise ValueError(
            f"{source} yielded {num_queries!r} gated queries -- refusing to silently fall "
            "back to the flat --tolerance. Point --qrels at a qrels.jsonl with at least one "
            "non-abstention query, pass a positive --num-queries, or omit both to use "
            "--tolerance as a flat value for every metric."
        )
    if not num_queries:
        return args.tolerance
    metric_names = {metric for metrics in baseline.values() for metric in metrics}
    return {
        metric: effective_tolerance(metric, num_queries, floor=args.tolerance)
        for metric in metric_names
    }


def _cmd_check(args: argparse.Namespace) -> int:
    current = load_metrics(Path(args.report))
    baseline = load_metrics(Path(args.baseline))
    try:
        tolerance = _resolve_check_tolerance(args, baseline)
    except ValueError as exc:
        print(f"[eval-gate] {exc}")
        return 2
    regressions = find_regressions(current, baseline, tolerance=tolerance)
    print(format_regressions(regressions))
    return 1 if regressions else 0


def _cmd_ratchet(args: argparse.Namespace) -> int:
    current = load_metrics(Path(args.report))
    baseline = load_metrics(Path(args.baseline))
    updated = ratchet_baseline(current, baseline)
    # Carry the source baseline's documentation keys (e.g. _comment) through, so
    # the committed policy note is not lost on the first ratchet.
    doc_keys = load_doc_keys(Path(args.baseline))
    out_path = Path(args.out)
    out_path.write_text(json.dumps({**doc_keys, **updated}, indent=2, sort_keys=True) + "\n")
    changed = updated != baseline
    print(f"[eval-gate] wrote ratcheted baseline to {out_path} (changed={changed})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Fail if the report regressed vs baseline.")
    check.add_argument("--report", required=True, help="Path to the current eval-report.json.")
    check.add_argument("--baseline", required=True, help="Path to the committed baseline JSON.")
    check.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=(
            "Floor tolerance (EVAL_GATE_TOLERANCE). Used as the flat tolerance for every "
            "metric unless --num-queries or --qrels is also given, in which case it is the "
            "floor under the derived per-metric resolution (#236)."
        ),
    )
    check.add_argument(
        "--num-queries",
        type=int,
        default=None,
        help="Number of gated golden queries; derives per-metric tolerance from corpus resolution.",
    )
    check.add_argument(
        "--qrels",
        default=None,
        help=(
            "Path to a qrels.jsonl to derive --num-queries from (counts distinct query_ids, "
            "excluding category == 'abstention'). Ignored if --num-queries is also given."
        ),
    )
    check.set_defaults(func=_cmd_check)

    ratchet = subparsers.add_parser(
        "ratchet", help="Write a baseline that never decreases below the current one."
    )
    ratchet.add_argument("--report", required=True, help="Path to the current eval-report.json.")
    ratchet.add_argument("--baseline", required=True, help="Path to the committed baseline JSON.")
    ratchet.add_argument("--out", required=True, help="Path to write the updated baseline JSON.")
    ratchet.set_defaults(func=_cmd_ratchet)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
