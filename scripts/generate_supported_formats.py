#!/usr/bin/env python3
"""Regenerate the supported-file-types table in docs/reference/file-types.md
from the FILE_TYPE_REGISTRY single source of truth (#117).

The table lives between two HTML-comment markers in the doc; everything
outside them (prose, headings) is untouched. Run this after adding, removing,
or editing a FILE_TYPE_REGISTRY entry in
services/inh-contracts/src/inh_contracts/file_types.py:

    uv run --project services/inh-contracts python scripts/generate_supported_formats.py

services/inh-public-api-svc/tests/unit/test_docs_sync.py fails CI the moment
the checked-in table and the registry disagree, so forgetting this step is
caught, not silent -- that test failing IS the reminder to run this script.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "services" / "inh-contracts" / "src"))

from inh_contracts.file_types import render_markdown_table  # noqa: E402

DOC_PATH = REPO_ROOT / "docs" / "reference" / "file-types.md"

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED FILE TYPES TABLE (#117; run "
    "scripts/generate_supported_formats.py to refresh) -->"
)
END_MARKER = "<!-- END GENERATED FILE TYPES TABLE -->"


def main() -> int:
    if not DOC_PATH.is_file():
        print(f"error: {DOC_PATH} does not exist", file=sys.stderr)
        return 1

    text = DOC_PATH.read_text()
    if BEGIN_MARKER not in text or END_MARKER not in text:
        print(
            f"error: {DOC_PATH} is missing the BEGIN/END generated-table markers",
            file=sys.stderr,
        )
        return 1

    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    new_text = f"{text[:start]}\n\n{render_markdown_table()}\n{text[end:]}"

    if new_text == text:
        print(f"{DOC_PATH} already up to date.")
        return 0

    DOC_PATH.write_text(new_text)
    print(f"Regenerated the supported-file-types table in {DOC_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
