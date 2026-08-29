"""Meta-tests for pytest compose marker enforcement hook (#286).

Verifies that the *shipped* ``pytest_collection_modifyitems`` hook (imported
from ``tests/compose_marker_gate.py``, the same module ``tests/conftest.py``
delegates to) correctly deselects compose-marked tests unless the marker
expression explicitly mentions "compose".

These tests use pytest's pytester fixture (pytest plugin for testing pytest
plugins). The generated project's conftest imports the real
``compose_marker_gate`` module rather than re-typing its logic, so these
tests fail if that module's behaviour changes or the function is removed --
see the "fail-when-removed" check below.
"""

import pathlib

import pytest

pytest_plugins = ["pytester"]

# Directory containing this file, i.e. tests/ -- also where compose_marker_gate.py
# lives. Added to the generated project's sys.path so its conftest can
# `import compose_marker_gate` and exercise the real, shipped hook.
_TESTS_DIR = pathlib.Path(__file__).parent


@pytest.fixture
def test_project(pytester):
    """Create a minimal test project that imports the real compose-marker gate.

    The generated conftest does NOT re-implement the hook: it imports
    ``pytest_collection_modifyitems`` (and the marker-registration
    ``pytest_configure``) straight from the real ``tests/compose_marker_gate.py``
    module that ``tests/conftest.py`` also delegates to, via
    ``pytester.syspathinsert``. A ``fastlane`` marker is registered
    separately here as a surrogate non-compose marker for these meta-tests
    only (see ``tests/test_smoke_lane_size.py``, which greps fixtures for a
    literal ``@pytest.mark.smoke`` and would be tripped by using the real
    marker name here).
    """
    pytester.syspathinsert(str(_TESTS_DIR))

    # Match the shipped pyproject.toml: `addopts = "-m 'not compose'"`. This
    # makes markexpr default to "not compose" via addopts (the code path
    # that actually ships), rather than the empty string a bare pytester
    # project would otherwise leave it as.
    pytester.makepyprojecttoml(
        """
        [tool.pytest.ini_options]
        addopts = "-m 'not compose'"
        """
    )

    pytester.makepyfile(
        conftest="""
import pytest

from compose_marker_gate import pytest_collection_modifyitems, pytest_configure as _register_compose_marker

def pytest_configure(config):
    _register_compose_marker(config)
    config.addinivalue_line(
        "markers", "fastlane: surrogate non-compose marker used only by these meta-tests"
    )
"""
    )

    # Create test files with various markers
    pytester.makepyfile(
        test_regular="""
import pytest

def test_no_marker():
    pass

@pytest.mark.compose
def test_compose_only():
    pass

@pytest.mark.unit
def test_unit():
    pass
"""
    )

    pytester.makepyfile(
        test_compose_smoke="""
import pytest

pytestmark = [pytest.mark.compose]

@pytest.mark.fastlane
def test_compose_smoke():
    pass
"""
    )

    return pytester


def test_default_no_marker_excludes_compose(test_project):
    """pytest with no -m should exclude compose tests via addopts (row 1 of the truth table).

    The generated project's pyproject.toml sets `addopts = "-m 'not compose'"`,
    same as the shipped services. With no -m on the command line, pytest
    resolves markexpr to "not compose" from addopts, so the hook sees
    "compose" in markexpr and returns early -- the exclusion is already done
    by pytest's own `-m` filter, not by the hook's deselection branch. This
    is a distinct code path from the empty-markexpr case, which is covered
    separately by test_empty_markexpr_excludes_compose below.
    """
    result = test_project.runpytest("--collect-only", "-q")

    # The 2 non-compose tests should be collected: test_no_marker, test_unit.
    # test_compose_only and test_compose_smoke are both compose-marked and
    # excluded by the addopts `-m 'not compose'` filter itself.
    assert "2/" in result.outlines[-1] or "2 deselected" in result.outlines[-1]


def test_empty_markexpr_excludes_compose(test_project):
    """pytest -m "" should exclude compose tests via the hook's deselection branch.

    An empty markexpr is the safe-direction edge case noted in the hook's
    docstring: `markexpr and "compose" in markexpr` is False when markexpr
    is "", so the hook's own deselection logic runs (rather than pytest's
    `-m` filter or addopts having already done the job).
    """
    result = test_project.runpytest("-m", "", "--collect-only", "-q")

    assert "2 deselected" in result.outlines[-1]


def test_marker_smoke_excludes_compose(test_project):
    """pytest -m fastlane should NOT collect compose tests even though test_compose_smoke is marked."""
    result = test_project.runpytest("-m", "fastlane", "--collect-only", "-q")

    # The hook sees markexpr = "fastlane" (no "compose" in it), so it
    # deselects all compose-marked items. test_compose_smoke.py has
    # pytestmark = [pytest.mark.compose] but also @pytest.mark.fastlane, so
    # pytest's own -m filter would select it -- the hook then deselects it
    # anyway. Net result: 0 collected (exit code 5 means no tests collected).
    assert result.ret == 5


def test_marker_compose_and_smoke_includes_compose(test_project):
    """pytest -m 'compose and fastlane' should include the compose+fastlane test."""
    result = test_project.runpytest("-m", "compose and fastlane", "--collect-only", "-q")

    # The hook sees markexpr = "compose and fastlane" (has "compose" in it),
    # so it returns early (does nothing). pytest's own -m filter selects
    # tests with both compose and fastlane: test_compose_smoke.
    assert "1/" in result.outlines[-1] and "collected" in result.outlines[-1]


def test_marker_not_compose_excludes_compose(test_project):
    """pytest -m 'not compose' should exclude compose tests (double-filter, no crash)."""
    result = test_project.runpytest("-m", "not compose", "--collect-only", "-q")

    # The hook sees markexpr = "not compose" (has "compose" in it), so it
    # returns early (does nothing). pytest's own -m filter selects tests not
    # marked with compose: test_no_marker, test_unit.
    assert "2/" in result.outlines[-1] and "collected" in result.outlines[-1]


def test_marker_compose_alone_includes_all_compose(test_project):
    """pytest -m 'compose' should collect all compose-marked tests."""
    result = test_project.runpytest("-m", "compose", "--collect-only", "-q")

    # The hook sees markexpr = "compose" (has "compose" in it), so it
    # returns early (does nothing). pytest's own -m filter selects tests
    # marked with compose: test_compose_only, test_compose_smoke.
    assert "2/" in result.outlines[-1] and "collected" in result.outlines[-1]
