"""Shared implementation of the pytest compose-marker enforcement hook (#286).

Extracted out of ``tests/conftest.py`` so it is a single importable unit: the
real conftest delegates to it, and ``test_pytest_compose_marker_hook.py``
exercises this exact module (via a pytester-generated conftest that imports
it) rather than a re-typed copy. Keeping the module free of any ``src``/DB
imports also keeps it safe to import from a pytester subprocess/child run.

See issue #286.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_COMPOSE_MARKER_LINE = "compose: mark test as requiring a Docker Compose stack"
_COMPOSE_WORD_RE = re.compile(r"\bcompose\b")


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``compose`` marker."""
    config.addinivalue_line("markers", _COMPOSE_MARKER_LINE)


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Enforce the 'not compose' default even when command-line -m is used.

    Problem: pytest's `-m` option REPLACES (not intersects) the default
    `addopts = "-m 'not compose'"` from pyproject.toml. So a developer
    running `pytest -m benchmark` would inadvertently select compose-marked
    tests that need a live Docker stack, causing confusing failures.

    Solution: If the effective marker expression does not mention "compose"
    as a whole word (meaning it's not explicitly included or excluded),
    deselect all compose-marked items. This ensures the `not compose` safety
    default is honored even when -m overrides addopts.

    Rule: if "compose" is not in markexpr, deselect compose items.
    This handles:
    - No -m: markexpr = "not compose" (has "compose") -> do nothing
    - -m smoke: markexpr = "smoke" (no "compose") -> deselect
    - -m "compose and smoke": markexpr has "compose" -> do nothing
    - -m "not compose": markexpr has "compose" -> do nothing
    - -m decompose: markexpr = "decompose" (word-boundary regex does NOT
      match "compose" as a substring of "decompose") -> deselect

    See issue #286.
    """
    # Get the effective marker expression (includes both addopts and command line)
    markexpr = config.option.markexpr

    # If the marker expression mentions "compose" as a whole word (either
    # inclusion or exclusion), respect it and don't deselect. Otherwise,
    # enforce the default by deselecting. A word-boundary match avoids a
    # false positive on an expression like "decompose".
    if markexpr and _COMPOSE_WORD_RE.search(markexpr):
        return

    # Deselect all compose-marked tests. get_closest_marker() checks the
    # item's own marks plus any class/module-level `pytestmark`, unlike
    # `"compose" in item.keywords`, which also matches substrings of node
    # *names* (e.g. a test literally named `test_compose_thing`).
    deselected = [item for item in items if item.get_closest_marker("compose") is not None]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item.get_closest_marker("compose") is None]
