"""GraphQL name guard must not rely on assert (#33).

collection/tenant names are string-interpolated into the GraphQL body. The
charset guard was an ``assert``, which is stripped under ``python -O`` — losing
the defense entirely. It must raise explicitly.
"""

from __future__ import annotations

import pytest

from src.services.search import _require_safe_name


def test_rejects_unsafe_name():
    with pytest.raises(ValueError):
        _require_safe_name("Bad Name!; drop", "collection")


def test_accepts_valid_prefixed_base32_name():
    # e.g. Workspace_<base32> — alphanumerics plus underscores.
    _require_safe_name("Workspace_O5ZS2MJSGM", "collection")
    _require_safe_name("User_OVZWK4S7GAYDC", "tenant")
