"""Docs-sync contract (#117, extended #193): the supported-file-types table
in docs/reference/file-types.md, and the other prose surfaces that name file
types, must match inh_contracts.file_types's own view of FILE_TYPE_REGISTRY.

This is the test the #117 issue asks for explicitly: "Docs must be GENERATED
from or VERIFIED against the registry -- a supported-types table that can
drift from the code is the defect being fixed, so a test that fails when they
disagree is part of the deliverable." Before this file, the equivalent table
lived by hand in docs/index.md and had already drifted twice over (missing
``image/png`` from docs/examples/README.md's allowed-types line, and from
tests/unit/test_upload_document.py's own "verify every allowed type" test --
see CURRENT_SCATTER in the #117 PR description). Nothing enforced agreement;
this test is that enforcement.

Regenerate the checked-in table after changing FILE_TYPE_REGISTRY:
    uv run --project services/inh-contracts python scripts/generate_supported_formats.py

#193 note on scope: the #117 table above is fully generated and byte-exact
verified. README.md, docs/index.md, and the mcp-tools.md prose paragraph were
changed to a non-exhaustive "representative types + link to file-types.md"
style instead (see #193's own suggested fix #2) -- a list that never claims
completeness cannot drift, so there is nothing for a test to check there.
docs/examples/README.md's "Allowed MIME types" line and 400-error JSON
example DO make an exhaustive, literal claim (real curl/response examples a
reader may copy verbatim), so those two get real, code-derived assertions
below instead.
"""

from __future__ import annotations

from pathlib import Path

from inh_contracts.file_types import all_mime_types, render_markdown_table

# tests/unit -> inh-public-api-svc -> services -> repo root (mirrors the
# existing REPO_ROOT convention in inh-ingestion-svc's
# test_extraction_by_type.py, which reaches into docs/examples the same way).
REPO_ROOT = Path(__file__).resolve().parents[4]
DOC_PATH = REPO_ROOT / "docs" / "reference" / "file-types.md"
EXAMPLES_DOC_PATH = REPO_ROOT / "docs" / "examples" / "README.md"
MCP_TOOLS_DOC_PATH = REPO_ROOT / "docs" / "reference" / "mcp-tools.md"

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED FILE TYPES TABLE (#117; run "
    "scripts/generate_supported_formats.py to refresh) -->"
)
END_MARKER = "<!-- END GENERATED FILE TYPES TABLE -->"


def test_file_types_doc_exists():
    assert DOC_PATH.is_file(), f"expected {DOC_PATH} to exist"


def test_file_types_doc_matches_registry():
    """The checked-in table between the markers must be BYTE-IDENTICAL to
    what render_markdown_table() produces right now. A mismatch means
    FILE_TYPE_REGISTRY changed (or the doc was hand-edited) without
    regenerating the doc -- exactly the drift #117 exists to prevent."""
    text = DOC_PATH.read_text()
    assert BEGIN_MARKER in text, "missing generated-table BEGIN marker"
    assert END_MARKER in text, "missing generated-table END marker"

    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    checked_in = text[start:end].strip("\n")
    expected = render_markdown_table().strip("\n")

    assert checked_in == expected, (
        "docs/reference/file-types.md is out of sync with FILE_TYPE_REGISTRY. "
        "Run: uv run --project services/inh-contracts python "
        "scripts/generate_supported_formats.py"
    )


def test_every_registry_type_named_in_doc():
    """Belt-and-suspenders on top of the exact-match check above: every
    registered MIME type must appear somewhere in the doc, so a future
    refactor of render_markdown_table() that accidentally drops a row still
    gets caught even if someone (wrongly) updates this test's marker
    comparison alongside it."""
    text = DOC_PATH.read_text()
    for mime in all_mime_types():
        assert mime in text, f"{mime} not documented in {DOC_PATH}"


# ---------------------------------------------------------------------------
# #193: docs/examples/README.md's literal, copy-pasteable examples.
#
# Unlike README.md/docs/index.md's prose (converted to a non-exhaustive
# "representative types + link" style that cannot drift because it no longer
# claims completeness), these two spots print values a reader can paste
# straight into a terminal -- an exhaustive claim that DOES need to track
# FILE_TYPE_REGISTRY, so it gets a real, code-derived assertion instead.
# ---------------------------------------------------------------------------


def test_examples_readme_exists():
    assert EXAMPLES_DOC_PATH.is_file(), f"expected {EXAMPLES_DOC_PATH} to exist"


def test_examples_readme_400_error_matches_registry():
    """The 'Unsupported file type' 400 JSON example must be BYTE-IDENTICAL to
    the real error `document_intake.py` raises (`ALLOWED_MIME_TYPES`, i.e.
    `all_mime_types()`, joined the same way) for the SAME declared content
    type the example uses (`application/octet-stream`). This is the literal
    error string #193's issue body calls out as one that "DOES change
    whenever `all_mime_types()` changes" -- previously nothing re-derived it
    from the registry to catch drift."""
    expected_detail = (
        "Unsupported file type 'application/octet-stream'. "
        f"Allowed types: {', '.join(all_mime_types())}"
    )
    text = EXAMPLES_DOC_PATH.read_text()
    assert expected_detail in text, (
        "docs/examples/README.md's 400 'Unsupported file type' example is out of "
        "sync with FILE_TYPE_REGISTRY. Expected this exact detail string:\n"
        f"{expected_detail}"
    )


def test_examples_readme_mentions_every_mime_type():
    """Every registered MIME type must appear somewhere in
    docs/examples/README.md. This is a whole-file substring scan, so it
    catches a type dropped from the WHOLE document (e.g. removed from both
    the 'Allowed MIME types' line and the 400 example, or never added for a
    newly-registered type) -- it does NOT independently pin the dedicated
    'Allowed MIME types' line: because the 400 JSON example already mentions
    every registered type (verified by
    `test_examples_readme_400_error_matches_registry` above), a type dropped
    from ONLY the 'Allowed MIME types' line still passes this whole-file
    scan as long as the 400 example still mentions it (proven by mutation:
    removing `application/toml` from only that line leaves this test
    green). If the 'Allowed MIME types' line itself needs a drift pin,
    that requires an assertion scoped to that line's text, not this one."""
    text = EXAMPLES_DOC_PATH.read_text()
    for mime in all_mime_types():
        assert mime in text, f"{mime} not mentioned anywhere in {EXAMPLES_DOC_PATH}"


# ---------------------------------------------------------------------------
# #193: docs/reference/mcp-tools.md's upload_document `content_type` docs.
#
# Coordinator adversarial-review finding (same #193 pass that flagged the
# server.py schema `default`): this doc used to hand-restate the accepted
# type list AND lead with "text/markdown default" -- a flat-default claim
# that flatly contradicted its own paragraph below explaining the real,
# extension-derived behavior (#197). That is the identical
# restate-instead-of-derive defect the schema fix removes, just in prose.
#
# Fix applied: the doc no longer enumerates the type list at all (that
# byte-exact enumeration lives in ONE place, file-types.md, already
# generated and verified above) -- it links there instead, the same
# non-exhaustive "representative + link" style used in README.md/
# docs/index.md (a claim that never asserts completeness cannot drift).
# What CAN still silently regress is the prose CLAIM about default
# behavior, so that -- not an exhaustive type list -- is what gets a real,
# code-derived assertion below: this doc's characterization of the default
# must keep matching `_default_upload_content_type`'s actual, documented
# fallback value.
# ---------------------------------------------------------------------------


def test_mcp_tools_doc_exists():
    assert MCP_TOOLS_DOC_PATH.is_file(), f"expected {MCP_TOOLS_DOC_PATH} to exist"


def test_mcp_tools_doc_does_not_claim_a_flat_default():
    """Regression pin for the coordinator's #193 blocker finding: the doc
    must never again lead with a flat 'content_type defaults to
    text/markdown' (or text/plain) claim. The real behavior is
    extension-derived (`_default_upload_content_type` in server.py) --
    text/plain is only the fallback for an unrecognized/absent extension
    (#208; was text/markdown pre-#208), not "the" default. The specific
    misleading phrasing this pins against is the literal old table-cell
    text this doc carried before the #193 fix."""
    text = MCP_TOOLS_DOC_PATH.read_text()
    assert "`text/markdown` default" not in text, (
        f"{MCP_TOOLS_DOC_PATH} must not claim content_type has a flat "
        "'text/markdown default' -- the real default is derived from the "
        "filename's extension (#197); state that instead."
    )
    assert "`text/plain` default" not in text, (
        f"{MCP_TOOLS_DOC_PATH} must not claim content_type has a flat "
        "'text/plain default' either -- the real default is derived from "
        "the filename's extension (#197); text/plain is only the "
        "unrecognized/absent-extension fallback (#208)."
    )


def test_mcp_tools_doc_explains_extension_derived_default():
    """Positive counterpart to the pin above: the doc must still correctly
    explain that an omitted content_type is DERIVED from the filename's
    extension, falling back to text/plain only when the extension is
    unrecognized or absent (#208: changed from text/markdown, which
    mislabelled Dockerfile/Makefile/README/.gitignore/archive.tar.gz as
    markdown) -- and must link to file-types.md (the single, generated,
    test-verified source of truth for the exhaustive type list) rather than
    re-enumerating it by hand."""
    text = MCP_TOOLS_DOC_PATH.read_text()
    assert "derived from" in text and "extension" in text, (
        f"{MCP_TOOLS_DOC_PATH} must explain that an omitted content_type is "
        "derived from the filename's extension (#197)"
    )
    assert "falling back to `text/plain`" in text, (
        f"{MCP_TOOLS_DOC_PATH} must document text/plain as the fallback for "
        "an unrecognized/absent extension (#208)"
    )
    assert "(file-types.md)" in text, (
        f"{MCP_TOOLS_DOC_PATH} must link to file-types.md as the exhaustive, "
        "generated type list instead of hand-enumerating types"
    )


def test_mcp_tools_doc_explains_explicit_value_is_never_overridden():
    """The doc must state the flip side of removing the schema `default`
    (coordinator review): a caller-supplied content_type is always honored
    as-is, never silently re-derived from the filename. Without this
    documented, a reader has no way to know whether an explicit value they
    pass is trustworthy or subject to a hidden override."""
    text = MCP_TOOLS_DOC_PATH.read_text()
    assert "never re-derived from the filename" in text or "never re-derived" in text, (
        f"{MCP_TOOLS_DOC_PATH} must state that an explicitly-declared "
        "content_type is honored as-is and never re-derived from the filename"
    )
