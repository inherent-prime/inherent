"""Offline unit tests for the retrieval-eval baseline-table renderer.

No services required; runs in the default ``-m 'not compose'`` suite alongside
``test_eval_gate.py``. These pin the properties the CI wiring depends on: the
rendered block is a pure function of the committed baseline, rewriting an
already-rendered README or docs snippet is a no-op (idempotent) so the ratchet
job only commits those files when the baseline actually moved, and the
checked-in docs snippet cannot drift from the committed JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.evals.eval_gate import load_metrics
from tests.evals.render_baseline_table import (
    MARKER_END,
    MARKER_START,
    MissingMarkersError,
    main,
    render_block,
    render_snippet,
    render_table,
    replace_block,
)

pytestmark = pytest.mark.retrieval_eval

REPO_ROOT = Path(__file__).resolve().parents[4]
COMMITTED_BASELINE = (
    REPO_ROOT
    / "services"
    / "inh-public-api-svc"
    / "tests"
    / "evals"
    / "corpus"
    / "retrieval_baseline.json"
)
COMMITTED_SNIPPET = REPO_ROOT / "docs" / "_generated" / "retrieval-baseline.md"


# ---------------------------------------------------------------------------
# render_table
# ---------------------------------------------------------------------------


def test_render_table_emits_one_row_per_mode_in_sorted_order():
    table = render_table(
        {
            "semantic": {"recall@5": 0.5, "mrr": 0.25, "ndcg@5": 0.125},
            "hybrid": {"recall@5": 1.0, "mrr": 0.75, "ndcg@5": 0.5},
        }
    )
    lines = table.splitlines()
    # Header + separator + one row per mode, modes alphabetical so the table
    # ordering is stable across runs (dict order must not leak into the diff).
    assert lines[0] == "| Mode | Recall@5 | MRR | nDCG@5 |"
    assert lines[1] == "| --- | --- | --- | --- |"
    assert lines[2] == "| Hybrid | 1.000 | 0.750 | 0.500 |"
    assert lines[3] == "| Semantic | 0.500 | 0.250 | 0.125 |"
    assert len(lines) == 4


def test_render_table_includes_all_gated_modes_and_metrics():
    table = render_table(
        {
            "hybrid": {"recall@5": 0.1, "mrr": 0.2, "ndcg@5": 0.3},
            "keyword": {"recall@5": 0.4, "mrr": 0.5, "ndcg@5": 0.6},
            "semantic": {"recall@5": 0.7, "mrr": 0.8, "ndcg@5": 0.9},
        }
    )
    assert "| Hybrid | 0.100 | 0.200 | 0.300 |" in table
    assert "| Keyword | 0.400 | 0.500 | 0.600 |" in table
    assert "| Semantic | 0.700 | 0.800 | 0.900 |" in table
    assert "Recall@5" in table
    assert "MRR" in table
    assert "nDCG@5" in table


def test_render_table_marks_metrics_the_baseline_does_not_track():
    # A mode missing a metric renders an em dash rather than a fabricated 0.000,
    # which would read as "measured and terrible" instead of "not measured".
    table = render_table({"keyword": {"recall@5": 0.8}})
    assert table.splitlines()[2] == "| Keyword | 0.800 | — | — |"


def test_render_table_handles_an_empty_baseline():
    # A zeroed/absent baseline is exactly the failure mode #139 existed to make
    # visible, so it must render an explicit note, never a bare empty table.
    assert "No retrieval baseline" in render_table({})


# ---------------------------------------------------------------------------
# replace_block
# ---------------------------------------------------------------------------


def _readme(body: str) -> str:
    return f"# Title\n\nintro\n\n{MARKER_START}\n{body}\n{MARKER_END}\n\ntrailing\n"


def test_replace_block_preserves_text_outside_the_markers():
    updated = replace_block(_readme("old content"), "new content")
    assert "# Title" in updated
    assert "intro" in updated
    assert "trailing" in updated
    assert "old content" not in updated
    assert "new content" in updated


def test_replace_block_is_idempotent():
    # The ratchet job commits README.md alongside the baseline; if rendering an
    # unchanged baseline produced a different byte sequence each run, every run
    # would dirty README.md and (via the merge to main) risk re-triggering CI.
    once = replace_block(_readme("old"), "generated")
    twice = replace_block(once, "generated")
    assert once == twice


def test_replace_block_keeps_the_markers_themselves():
    updated = replace_block(_readme("old"), "generated")
    assert updated.count(MARKER_START) == 1
    assert updated.count(MARKER_END) == 1


@pytest.mark.parametrize(
    "text",
    [
        "no markers at all",
        f"{MARKER_START}\nunclosed\n",
        f"{MARKER_END}\nend before start\n{MARKER_START}\n",
    ],
)
def test_replace_block_rejects_malformed_markers(text):
    # Fail loudly rather than silently leaving README.md un-updated -- a silent
    # no-op here reproduces the "gate looks wired but never moves" class of bug
    # this whole eval pipeline exists to prevent.
    with pytest.raises(MissingMarkersError):
        replace_block(text, "generated")


# ---------------------------------------------------------------------------
# render_block / render_snippet
# ---------------------------------------------------------------------------


def test_render_block_is_wrapped_in_a_do_not_edit_notice():
    block = render_block({"hybrid": {"recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0}})
    assert "generated" in block.lower()
    # The block body must not embed the markers; replace_block owns those.
    assert MARKER_START not in block
    assert MARKER_END not in block


def test_render_snippet_is_render_block_plus_trailing_newline():
    metrics = {"hybrid": {"recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0}}
    snippet = render_snippet(metrics)
    assert snippet == render_block(metrics) + "\n"
    assert snippet.endswith("\n")


def test_render_snippet_is_deterministic():
    metrics = {
        "semantic": {"recall@5": 0.7, "mrr": 0.8, "ndcg@5": 0.9},
        "hybrid": {"recall@5": 0.1, "mrr": 0.2, "ndcg@5": 0.3},
    }
    assert render_snippet(metrics) == render_snippet(metrics)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_rewrites_the_readme_block_from_the_baseline(tmp_path):
    baseline = tmp_path / "retrieval_baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "_comment": "documentation key, not a mode",
                "hybrid": {"recall@5": 0.75, "mrr": 0.5, "ndcg@5": 0.25},
            }
        )
    )
    readme = tmp_path / "README.md"
    readme.write_text(_readme("stale table"))

    assert main(["--baseline", str(baseline), "--readme", str(readme)]) == 0

    updated = readme.read_text()
    assert "| Hybrid | 0.750 | 0.500 | 0.250 |" in updated
    assert "stale table" not in updated
    # `_comment` is a documentation key, not a retrieval mode -- it must not
    # become a table row (load_metrics in eval_gate.py already drops it; this
    # pins that the renderer relies on that and does not re-add it).
    assert "_comment" not in updated


def test_main_is_idempotent_across_runs(tmp_path):
    baseline = tmp_path / "retrieval_baseline.json"
    baseline.write_text(json.dumps({"hybrid": {"recall@5": 0.75, "mrr": 0.5, "ndcg@5": 0.25}}))
    readme = tmp_path / "README.md"
    readme.write_text(_readme("stale"))

    main(["--baseline", str(baseline), "--readme", str(readme)])
    first = readme.read_text()
    main(["--baseline", str(baseline), "--readme", str(readme)])
    assert readme.read_text() == first


def test_main_fails_when_the_readme_has_no_markers(tmp_path):
    baseline = tmp_path / "retrieval_baseline.json"
    baseline.write_text(json.dumps({"hybrid": {"recall@5": 1.0}}))
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n\nno markers here\n")

    # Non-zero exit so the CI step fails loudly instead of committing a README
    # that silently never updates again.
    assert main(["--baseline", str(baseline), "--readme", str(readme)]) == 1


def test_main_writes_docs_snippet_from_the_baseline(tmp_path):
    baseline = tmp_path / "retrieval_baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "_comment": "not a mode",
                "hybrid": {"recall@5": 0.75, "mrr": 0.5, "ndcg@5": 0.25},
                "keyword": {"recall@5": 0.70, "mrr": 0.40, "ndcg@5": 0.20},
                "semantic": {"recall@5": 0.65, "mrr": 0.30, "ndcg@5": 0.15},
            }
        )
    )
    snippet = tmp_path / "docs" / "_generated" / "retrieval-baseline.md"

    assert main(["--baseline", str(baseline), "--docs-snippet", str(snippet)]) == 0

    written = snippet.read_text(encoding="utf-8")
    assert written == render_snippet(load_metrics(baseline))
    assert "| Hybrid | 0.750 | 0.500 | 0.250 |" in written
    assert "| Keyword | 0.700 | 0.400 | 0.200 |" in written
    assert "| Semantic | 0.650 | 0.300 | 0.150 |" in written
    assert "Recall@5" in written
    assert "MRR" in written
    assert "nDCG@5" in written
    assert "_comment" not in written


def test_main_docs_snippet_is_idempotent(tmp_path):
    baseline = tmp_path / "retrieval_baseline.json"
    baseline.write_text(json.dumps({"hybrid": {"recall@5": 0.75, "mrr": 0.5, "ndcg@5": 0.25}}))
    snippet = tmp_path / "retrieval-baseline.md"

    main(["--baseline", str(baseline), "--docs-snippet", str(snippet)])
    first = snippet.read_bytes()
    main(["--baseline", str(baseline), "--docs-snippet", str(snippet)])
    assert snippet.read_bytes() == first


def test_main_readme_and_docs_snippet_share_the_same_table(tmp_path):
    baseline = tmp_path / "retrieval_baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "hybrid": {"recall@5": 0.75, "mrr": 0.5, "ndcg@5": 0.25},
                "keyword": {"recall@5": 0.70, "mrr": 0.40, "ndcg@5": 0.20},
            }
        )
    )
    readme = tmp_path / "README.md"
    readme.write_text(_readme("stale"))
    snippet = tmp_path / "retrieval-baseline.md"

    assert (
        main(
            [
                "--baseline",
                str(baseline),
                "--readme",
                str(readme),
                "--docs-snippet",
                str(snippet),
            ]
        )
        == 0
    )

    table = render_table(load_metrics(baseline))
    assert table in readme.read_text(encoding="utf-8")
    assert table in snippet.read_text(encoding="utf-8")


def test_main_requires_an_output_path(tmp_path):
    baseline = tmp_path / "retrieval_baseline.json"
    baseline.write_text(json.dumps({"hybrid": {"recall@5": 1.0}}))

    with pytest.raises(SystemExit) as excinfo:
        main(["--baseline", str(baseline)])
    assert excinfo.value.code == 2


def test_committed_docs_snippet_matches_committed_baseline():
    """The checked-in snippet is generated output, not a hand-edited copy.

    If this fails, re-run ``render_baseline_table.py --docs-snippet``; do not
    edit ``docs/_generated/retrieval-baseline.md`` by hand.
    """
    expected = render_snippet(load_metrics(COMMITTED_BASELINE))
    assert COMMITTED_SNIPPET.read_text(encoding="utf-8") == expected
