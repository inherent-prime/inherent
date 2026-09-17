"""Unit tests for the #288 stale-content_type backfill (#288).

`derive_confident_content_type` and `plan_content_type_update` are pure
functions -- no database, no Weaviate, no event loop -- so every test here
runs in the same offline, fast lane as the rest of the unit suite. This is
deliberate (see `scripts/backfill_stale_content_type.py`'s module
docstring): the derivation used to live in a SQL `CASE` function, which is
exactly why the previous version of this test file needed a live database
and its "real" coverage was 15 `@pytest.mark.skip`s. Pulling the derivation
into a plain Python function is what makes it testable at all without one.

No `@pytest.mark.skip` appears anywhere in this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not an importable package (no __init__.py, not on sys.path by
# default) -- add it directly, the same way the script itself resolves paths
# relative to its own file. Matches how the sibling operator scripts at the
# repo root are written (see scripts/reindex_orphaned_document.py), just one
# directory level shallower since this script lives inside the service.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from backfill_stale_content_type import (  # noqa: E402
    STALE_CONTENT_TYPE,
    derive_confident_content_type,
    plan_content_type_update,
)


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture.

    `tests/conftest.py` defines an autouse `cleanup_test_data` that depends
    on `db_service`, which `pytest.skip`s the whole test when PostgreSQL is
    unavailable -- exactly how EVERY test in this file's previous
    incarnation (`test_migration_018_content_type.py`) ended up skipped
    regardless of what it actually tested. This suite covers a pure
    function with no I/O at all, so it overrides the fixture with a no-op
    the same way `tests/test_migrations.py` already does for its own
    offline suite.
    """
    yield


class TestDeriveConfidentContentType:
    """`derive_confident_content_type`: the pure filename -> mime derivation."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("notes.md", "text/markdown"),
            ("notes.markdown", "text/markdown"),
            ("script.py", "text/x-python"),
            ("data.csv", "text/csv"),
            ("notes.txt", "text/plain"),
            ("config.yaml", "application/yaml"),
            ("config.yml", "application/yaml"),
            ("settings.toml", "application/toml"),
            ("feed.xml", "application/xml"),
            ("transcript.srt", "application/x-subrip"),
            ("captions.vtt", "text/vtt"),
            ("main.go", "text/x-go"),
            ("app.js", "text/javascript"),
        ],
    )
    def test_recognized_mcp_eligible_extensions_resolve(self, filename, expected):
        """A registered, MCP-eligible extension resolves to its real type --
        the confident, provably-correct case this backfill exists to fix."""
        assert derive_confident_content_type(filename) == expected

    @pytest.mark.parametrize(
        ("lower", "upper", "mixed"),
        [
            ("script.py", "script.PY", "script.Py"),
            ("readme.md", "README.MD", "Readme.Md"),
            ("data.csv", "DATA.CSV", "Data.Csv"),
        ],
    )
    def test_extension_matching_is_case_insensitive(self, lower, upper, mixed):
        result = derive_confident_content_type(lower)
        assert result is not None
        assert derive_confident_content_type(upper) == result
        assert derive_confident_content_type(mixed) == result

    @pytest.mark.parametrize("filename", ["Dockerfile", "Makefile", "README", "LICENSE"])
    def test_extensionless_filenames_are_not_determinable(self, filename):
        """No '.' at all -- #288's own headline examples. Could legitimately
        be markdown (a README explicitly declared as such); nothing about
        the bare filename proves otherwise, so this must return None, never
        a guess."""
        assert derive_confident_content_type(filename) is None

    def test_dotfile_takes_the_extension_branch_but_stays_undeterminable(self):
        """`.gitignore` contains a '.', so it takes the SAME extension
        branch as any other filename (#288's explicit note: the backfill
        query "must not simply test for the absence of '.'") -- it does not
        short-circuit to extensionless handling. Its derived extension,
        `.gitignore`, is simply not in FILE_TYPE_REGISTRY, so the result is
        still None: this pins that the dotfile path is exercised, not
        skipped, and lands on the correct (undeterminable) answer regardless."""
        assert derive_confident_content_type(".gitignore") is None

    @pytest.mark.parametrize(
        "filename",
        [
            "archive.tar.gz",  # last-dot extension is the unregistered ".gz"
            "notes.log",
            "data.parquet",
            "image.jpeg",
        ],
    )
    def test_unregistered_extensions_are_not_determinable(self, filename):
        assert derive_confident_content_type(filename) is None

    @pytest.mark.parametrize(
        "filename",
        ["report.pdf", "data.json", "sheet.xlsx", "slides.pptx", "photo.png"],
    )
    def test_registered_but_non_mcp_extensions_are_not_determinable(self, filename):
        """These extensions ARE in FILE_TYPE_REGISTRY, but their spec is
        REST-only. Only the MCP upload fallback could have produced a stale
        `text/markdown` row in the first place (#208 is specifically the MCP
        `upload_document` fallback), so a REST-only extension is exactly as
        undecidable here as an unregistered one -- treating it as confidently
        `application/pdf` etc. would assert something about how the row was
        produced that we cannot actually know."""
        assert derive_confident_content_type(filename) is None


class TestPlanContentTypeUpdate:
    """`plan_content_type_update`: whether/what to write for one row."""

    def test_stale_row_with_determinable_extension_plans_an_update(self):
        assert plan_content_type_update(STALE_CONTENT_TYPE, "script.py") == "text/x-python"

    def test_stale_row_with_undeterminable_filename_plans_no_update(self):
        """The most important case: when the correct value cannot be proven,
        the row is left untouched -- plan_content_type_update returns None,
        never a fallback guess."""
        assert plan_content_type_update(STALE_CONTENT_TYPE, "Dockerfile") is None
        assert plan_content_type_update(STALE_CONTENT_TYPE, ".gitignore") is None
        assert plan_content_type_update(STALE_CONTENT_TYPE, "archive.tar.gz") is None

    def test_row_already_correctly_markdown_plans_no_update(self):
        """A genuinely markdown-named file legitimately labelled
        text/markdown -- determinable, but not a change."""
        assert plan_content_type_update(STALE_CONTENT_TYPE, "notes.md") is None

    def test_row_not_currently_stale_plans_no_update(self):
        """Defensive: even if a caller passes a row whose content_type isn't
        the stale marker at all, nothing is proposed -- this function does
        not trust its caller's WHERE clause to have filtered correctly."""
        assert plan_content_type_update("text/plain", "script.py") is None
        assert plan_content_type_update("application/pdf", "script.py") is None

    def test_idempotent_second_pass_changes_nothing(self):
        """Running the backfill twice must be a no-op on the second pass.
        Modeled here without any database: feed the OUTPUT of the first
        call back in as the new `current_content_type` and confirm the
        second call proposes nothing further -- exactly what happens for a
        real row once Postgres holds the corrected value."""
        first_pass = plan_content_type_update(STALE_CONTENT_TYPE, "script.py")
        assert first_pass == "text/x-python"

        second_pass = plan_content_type_update(first_pass, "script.py")
        assert second_pass is None

    @pytest.mark.parametrize("filename", ["Dockerfile", "archive.tar.gz", ".gitignore"])
    def test_idempotent_second_pass_for_undeterminable_rows(self, filename):
        """An undeterminable row is left alone on every pass, not just the
        first -- repeatedly running the backfill against a permanently
        ambiguous filename never starts guessing on a later attempt."""
        first_pass = plan_content_type_update(STALE_CONTENT_TYPE, filename)
        assert first_pass is None

        second_pass = plan_content_type_update(STALE_CONTENT_TYPE, filename)
        assert second_pass is None
