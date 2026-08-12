"""Offline unit tests for the baseline-comparison CLI in ``eval_gate.py``.

Hand-computed expected values so the gate logic is pinned exactly. No services
required; runs in the default ``-m 'not compose'`` suite. The live-stack wiring
(compose test calling ``find_regressions`` against real metrics) lives in
``test_compose_retrieval_regression.py``.
"""

from __future__ import annotations

import json
import math

import pytest

from tests.evals.eval_gate import (
    DEFAULT_TOLERANCE,
    Regression,
    effective_tolerance,
    find_regressions,
    format_regressions,
    load_doc_keys,
    load_metrics,
    load_qrels_query_count,
    main,
    min_detectable_delta,
    ratchet_baseline,
)

pytestmark = pytest.mark.retrieval_eval


# ---------------------------------------------------------------------------
# load_metrics
# ---------------------------------------------------------------------------


def test_load_metrics_drops_documentation_keys(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(
        json.dumps(
            {
                "_comment": "not a mode",
                "hybrid": {"recall@5": 0.5},
            }
        )
    )
    assert load_metrics(path) == {"hybrid": {"recall@5": 0.5}}


def test_load_metrics_missing_file_returns_empty(tmp_path):
    assert load_metrics(tmp_path / "does-not-exist.json") == {}


def test_load_metrics_invalid_json_returns_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    assert load_metrics(path) == {}


# ---------------------------------------------------------------------------
# load_doc_keys
# ---------------------------------------------------------------------------


def test_load_doc_keys_returns_only_underscore_keys(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"_comment": "policy", "hybrid": {"recall@5": 0.5}}))
    assert load_doc_keys(path) == {"_comment": "policy"}


def test_load_doc_keys_missing_file_returns_empty(tmp_path):
    assert load_doc_keys(tmp_path / "nope.json") == {}


# ---------------------------------------------------------------------------
# ratchet CLI (main) preserves documentation keys
# ---------------------------------------------------------------------------


def test_cli_ratchet_preserves_comment_and_ratchets(tmp_path):
    """The ratchet CLI must keep the baseline's _comment and raise metrics.

    Regression guard for the doc-key-stripping bug: the first ratchet used to
    drop _comment (which documents the hard-gate policy) entirely.
    """
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"_comment": "policy note", "hybrid": {"recall@5": 0.50}}))
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"hybrid": {"recall@5": 0.70}}))
    out = tmp_path / "out.json"

    exit_code = main(
        ["ratchet", "--report", str(report), "--baseline", str(baseline), "--out", str(out)]
    )

    assert exit_code == 0
    written = json.loads(out.read_text())
    assert written["_comment"] == "policy note"
    assert written["hybrid"]["recall@5"] == 0.70


# ---------------------------------------------------------------------------
# find_regressions
# ---------------------------------------------------------------------------


def test_no_regression_when_current_matches_baseline():
    baseline = {"hybrid": {"recall@5": 0.5}}
    current = {"hybrid": {"recall@5": 0.5}}
    assert find_regressions(current, baseline) == []


def test_no_regression_when_current_improves():
    baseline = {"hybrid": {"recall@5": 0.5}}
    current = {"hybrid": {"recall@5": 0.9}}
    assert find_regressions(current, baseline) == []


def test_no_regression_within_tolerance():
    baseline = {"hybrid": {"recall@5": 0.50}}
    current = {"hybrid": {"recall@5": 0.49}}
    assert find_regressions(current, baseline, tolerance=0.02) == []


def test_regression_flagged_beyond_tolerance():
    baseline = {"hybrid": {"recall@5": 0.50}}
    current = {"hybrid": {"recall@5": 0.40}}
    regressions = find_regressions(current, baseline, tolerance=0.02)
    assert regressions == [Regression("hybrid", "recall@5", current=0.40, baseline=0.50)]


def test_regression_delta_is_negative():
    reg = Regression("hybrid", "recall@5", current=0.40, baseline=0.50)
    assert reg.delta == pytest.approx(-0.10)


def test_missing_baseline_metric_cannot_regress():
    """A metric absent from baseline (e.g. newly added) has nothing to regress against."""
    baseline: dict[str, dict[str, float]] = {}
    current = {"hybrid": {"recall@5": 0.0}}
    assert find_regressions(current, baseline) == []


def test_missing_current_metric_counts_as_zero():
    """A metric the baseline tracks but the current run didn't produce is treated as 0.0.

    This catches a broken/incomplete eval run silently passing the gate.
    """
    baseline = {"hybrid": {"recall@5": 0.50}}
    current: dict[str, dict[str, float]] = {"hybrid": {}}
    regressions = find_regressions(current, baseline, tolerance=0.02)
    assert regressions == [Regression("hybrid", "recall@5", current=0.0, baseline=0.50)]


def test_missing_current_metric_at_zero_baseline_is_not_a_regression():
    baseline = {"hybrid": {"recall@5": 0.0}}
    current: dict[str, dict[str, float]] = {"hybrid": {}}
    assert find_regressions(current, baseline) == []


def test_multiple_modes_and_metrics_checked_independently():
    baseline = {
        "hybrid": {"recall@5": 0.50, "mrr": 0.60},
        "keyword": {"recall@5": 0.20},
    }
    current = {
        "hybrid": {"recall@5": 0.55, "mrr": 0.30},
        "keyword": {"recall@5": 0.20},
    }
    regressions = find_regressions(current, baseline, tolerance=0.02)
    assert regressions == [Regression("hybrid", "mrr", current=0.30, baseline=0.60)]


# ---------------------------------------------------------------------------
# ratchet_baseline
# ---------------------------------------------------------------------------


def test_ratchet_takes_the_higher_value_per_metric():
    baseline = {"hybrid": {"recall@5": 0.50, "mrr": 0.80}}
    current = {"hybrid": {"recall@5": 0.60, "mrr": 0.70}}
    assert ratchet_baseline(current, baseline) == {"hybrid": {"recall@5": 0.60, "mrr": 0.80}}


def test_ratchet_never_decreases_below_baseline():
    """Even a large drop in current must not lower the committed baseline."""
    baseline = {"hybrid": {"recall@5": 0.50}}
    current = {"hybrid": {"recall@5": 0.0}}
    assert ratchet_baseline(current, baseline) == {"hybrid": {"recall@5": 0.50}}


def test_ratchet_adds_new_modes_and_metrics():
    baseline: dict[str, dict[str, float]] = {}
    current = {"semantic": {"ndcg@5": 0.42}}
    assert ratchet_baseline(current, baseline) == {"semantic": {"ndcg@5": 0.42}}


def test_ratchet_is_idempotent():
    baseline = {"hybrid": {"recall@5": 0.50}}
    current = {"hybrid": {"recall@5": 0.50}}
    assert ratchet_baseline(current, baseline) == baseline


# ---------------------------------------------------------------------------
# format_regressions
# ---------------------------------------------------------------------------


def test_format_regressions_empty_is_a_pass_message():
    message = format_regressions([])
    assert "no regressions" in message.lower()


def test_format_regressions_lists_each_one():
    regressions = [
        Regression("hybrid", "recall@5", current=0.40, baseline=0.50),
        Regression("keyword", "mrr", current=0.10, baseline=0.30),
    ]
    message = format_regressions(regressions)
    assert "hybrid.recall@5" in message
    assert "keyword.mrr" in message
    assert "0.40" in message
    assert "0.50" in message


# ---------------------------------------------------------------------------
# min_detectable_delta / effective_tolerance (#236)
#
# Metric-name keys match retrieval_baseline.json's actual keys ("mrr",
# "recall@5", "ndcg@5" -- NOT "recall_at_5"/"ndcg_at_5"), not the brief's
# placeholder names.
# ---------------------------------------------------------------------------


def test_min_detectable_delta_mrr():
    # smallest single-query MRR move: rank 1 -> 2 changes 1/1 - 1/2 = 0.5, averaged over n
    assert min_detectable_delta("mrr", 13) == pytest.approx(0.5 / 13)


def test_min_detectable_delta_recall():
    # one query gaining/losing one relevant doc: 1/n (conservative, single-relevant case)
    assert min_detectable_delta("recall@5", 13) == pytest.approx(1 / 13)


def test_min_detectable_delta_ndcg():
    # smallest top-2 swap: (1 - 1/log2(3)) / n
    assert min_detectable_delta("ndcg@5", 13) == pytest.approx((1 - 1 / math.log2(3)) / 13)


def test_min_detectable_delta_rejects_unrecognized_metric():
    with pytest.raises(ValueError):
        min_detectable_delta("unknown@5", 13)


def test_min_detectable_delta_rejects_non_positive_num_queries():
    with pytest.raises(ValueError):
        min_detectable_delta("mrr", 0)


def test_effective_tolerance_takes_max_of_floor_and_resolution():
    assert effective_tolerance("mrr", 13, floor=0.02) == pytest.approx(
        0.5 / 13
    )  # resolution dominates
    assert effective_tolerance("mrr", 200, floor=0.02) == pytest.approx(0.02)  # floor dominates


def test_effective_tolerance_defaults_floor_to_default_tolerance():
    assert effective_tolerance("mrr", 200) == pytest.approx(DEFAULT_TOLERANCE)


def test_find_regressions_with_effective_tolerance_ignores_single_rank_slip():
    # baseline mrr .70, current .6615 (= one rank-1->2 slip at n=13): NOT a regression
    baseline = {"keyword": {"mrr": 0.70}}
    tolerance = {"mrr": effective_tolerance("mrr", 13, floor=0.02)}
    current_slip = {"keyword": {"mrr": 0.70 - 0.5 / 13}}
    assert find_regressions(current_slip, baseline, tolerance=tolerance) == []

    # baseline mrr .70, current .60: IS a regression
    current_drop = {"keyword": {"mrr": 0.60}}
    assert find_regressions(current_drop, baseline, tolerance=tolerance) == [
        Regression("keyword", "mrr", current=0.60, baseline=0.70)
    ]


def test_find_regressions_still_accepts_a_flat_float_tolerance():
    """Backward-compat: existing callers passing a single float must keep working."""
    baseline = {"hybrid": {"recall@5": 0.50}}
    current = {"hybrid": {"recall@5": 0.49}}
    assert find_regressions(current, baseline, tolerance=0.02) == []


# ---------------------------------------------------------------------------
# load_qrels_query_count (#236)
# ---------------------------------------------------------------------------


def test_load_qrels_query_count_excludes_abstention_category(tmp_path):
    path = tmp_path / "qrels.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"query_id": "q1", "query": "a", "document_id": "d1", "relevance": 3},
                {
                    "query_id": "q2",
                    "query": "b",
                    "document_id": "d2",
                    "relevance": 0,
                    "category": "abstention",
                },
                {"query_id": "q3", "query": "c", "document_id": "d3", "relevance": 3},
            ]
        )
    )
    assert load_qrels_query_count(path) == 2


def test_load_qrels_query_count_dedupes_multi_line_queries(tmp_path):
    path = tmp_path / "qrels.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"query_id": "q1", "query": "a", "document_id": "d1", "relevance": 3},
                {"query_id": "q1", "query": "a", "document_id": "d2", "relevance": 2},
            ]
        )
    )
    assert load_qrels_query_count(path) == 1


def test_load_qrels_query_count_missing_file_returns_zero(tmp_path):
    assert load_qrels_query_count(tmp_path / "nope.jsonl") == 0


# ---------------------------------------------------------------------------
# `check` CLI subcommand: derivation wiring + loud-error-on-empty-derivation
# (code review fix round 1, finding 1) -- nothing previously exercised
# `main(["check", ...])` directly, so the CLI's own tolerance-resolution path
# (`_resolve_check_tolerance`) had no coverage at all.
#
# All scenarios share one baseline/report pair: baseline mrr .70, current
# .62 (delta -0.08). Under the flat DEFAULT_TOLERANCE (0.02) that is a
# regression; under a derived tolerance with n=5 gated queries
# (min_detectable_delta("mrr", 5) == 0.5/5 == 0.1) it is not -- so a passing
# exit code demonstrates derivation actually changed the outcome, not just
# that the CLI didn't crash.
# ---------------------------------------------------------------------------


@pytest.fixture
def _check_fixture_paths(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"hybrid": {"mrr": 0.70}}))
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"hybrid": {"mrr": 0.62}}))
    return baseline, report


def test_cli_check_without_derivation_flags_flags_the_regression(_check_fixture_paths, capsys):
    """Control case: omitting --num-queries/--qrels keeps the pre-#236 flat behavior."""
    baseline, report = _check_fixture_paths
    exit_code = main(["check", "--report", str(report), "--baseline", str(baseline)])
    assert exit_code == 1
    assert "hybrid.mrr" in capsys.readouterr().out


def test_cli_check_with_num_queries_derives_tolerance_and_passes(_check_fixture_paths, capsys):
    baseline, report = _check_fixture_paths
    exit_code = main(
        [
            "check",
            "--report",
            str(report),
            "--baseline",
            str(baseline),
            "--num-queries",
            "5",
        ]
    )
    assert exit_code == 0
    assert "no regressions" in capsys.readouterr().out.lower()


def test_cli_check_with_valid_qrels_derives_tolerance_and_passes(
    _check_fixture_paths, tmp_path, capsys
):
    baseline, report = _check_fixture_paths
    qrels = tmp_path / "qrels.jsonl"
    qrels.write_text(
        "\n".join(
            json.dumps({"query_id": f"q{i}", "query": "x", "document_id": "d", "relevance": 3})
            for i in range(5)
        )
    )
    exit_code = main(
        ["check", "--report", str(report), "--baseline", str(baseline), "--qrels", str(qrels)]
    )
    assert exit_code == 0
    assert "no regressions" in capsys.readouterr().out.lower()


def test_cli_check_with_bad_qrels_path_errors_loudly_instead_of_silently_falling_back(
    _check_fixture_paths, tmp_path, capsys
):
    """An explicit --qrels that resolves to 0 gated queries must not silently degrade

    to the flat --tolerance -- that would defeat the entire point of asking for
    derivation. It must fail loud instead (distinct exit code, explanatory message).
    """
    baseline, report = _check_fixture_paths
    missing_qrels = tmp_path / "does-not-exist.jsonl"
    exit_code = main(
        [
            "check",
            "--report",
            str(report),
            "--baseline",
            str(baseline),
            "--qrels",
            str(missing_qrels),
        ]
    )
    assert exit_code == 2
    out = capsys.readouterr().out
    assert "--qrels" in out
    assert str(missing_qrels) in out
    assert "0" in out


def test_cli_check_with_all_abstention_qrels_errors_loudly(_check_fixture_paths, tmp_path, capsys):
    """A qrels file that parses fine but has no gated (non-abstention) query is the

    same "explicit opt-in resolved to nothing" case as a missing file, and must
    fail the same loud way rather than silently using the flat --tolerance.
    """
    baseline, report = _check_fixture_paths
    qrels = tmp_path / "all-abstention.jsonl"
    qrels.write_text(
        json.dumps(
            {
                "query_id": "q1",
                "query": "x",
                "document_id": "d",
                "relevance": 0,
                "category": "abstention",
            }
        )
    )
    exit_code = main(
        ["check", "--report", str(report), "--baseline", str(baseline), "--qrels", str(qrels)]
    )
    assert exit_code == 2
    assert "--qrels" in capsys.readouterr().out


def test_cli_check_explicit_num_queries_zero_errors_loudly(_check_fixture_paths, capsys):
    """A literal --num-queries 0 is also an explicit (if odd) opt-in to derivation."""
    baseline, report = _check_fixture_paths
    exit_code = main(
        [
            "check",
            "--report",
            str(report),
            "--baseline",
            str(baseline),
            "--num-queries",
            "0",
        ]
    )
    assert exit_code == 2
    assert "--num-queries" in capsys.readouterr().out
