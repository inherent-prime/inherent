"""Meta-tests for pytest compose marker enforcement hook (#286).

Verifies that pytest_collection_modifyitems correctly deselects compose-marked
tests unless the marker expression explicitly mentions "compose".

These tests use pytest's pytester fixture (pytest plugin for testing pytest plugins).
"""

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def test_project(pytester):
    """Create a minimal test project with compose markers."""
    # Create a conftest that registers the compose marker
    pytester.makepyfile(
        conftest="""
import pytest

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "compose: mark test as requiring a Docker Compose stack"
    )

def pytest_collection_modifyitems(config, items):
    markexpr = config.option.markexpr
    if markexpr and "compose" in markexpr:
        return
    deselected = [item for item in items if "compose" in item.keywords]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if "compose" not in item.keywords]
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

@pytest.mark.smoke
def test_compose_smoke():
    pass
"""
    )

    return pytester


def test_default_no_marker_excludes_compose(test_project):
    """pytest with no -m should exclude compose tests (addopts handles it)."""
    # Run pytest without any -m flag
    # The hook sees markexpr = "not compose" from addopts, so it does nothing
    # (addopts already excluded them)
    result = test_project.runpytest("--collect-only", "-q")

    # Check that compose tests are deselected
    # We expect to see deselected count in the output
    assert "deselected" in result.outlines[-1]
    # The 3 non-compose tests should be collected: test_no_marker, test_unit, test_compose_smoke
    # (Actually test_compose_smoke is in a compose module, so it would be included in 3 compose tests)
    # Wait, test_compose_smoke.py has pytestmark = [pytest.mark.compose], so ALL tests in that file
    # are marked with compose. So we have:
    # - test_regular.py: test_no_marker (1), test_unit (1) = 2 non-compose
    # - test_regular.py: test_compose_only (1) = 1 compose
    # - test_compose_smoke.py: test_compose_smoke (1) = 1 compose
    # Total: 2 non-compose, 2 compose
    # After default deselection: 2 collected, 2 deselected
    assert "2 deselected" in result.outlines[-1]


def test_marker_smoke_excludes_compose(test_project):
    """pytest -m smoke should NOT collect compose tests even though test_compose_smoke is marked."""
    result = test_project.runpytest("-m", "smoke", "--collect-only", "-q")

    # The hook sees markexpr = "smoke" (no "compose" in it)
    # So it deselects all compose-marked items
    # test_compose_smoke.py has pytestmark = [pytest.mark.compose] but also @pytest.mark.smoke
    # After marker filter: should select test_compose_smoke (matches smoke marker)
    # After hook: should deselect test_compose_smoke (has compose marker and markexpr doesn't mention compose)
    # Result: 0 collected
    # Exit code 5 means no tests were collected
    assert result.ret == 5


def test_marker_compose_and_smoke_includes_compose(test_project):
    """pytest -m 'compose and smoke' should include the compose+smoke test."""
    result = test_project.runpytest("-m", "compose and smoke", "--collect-only", "-q")

    # The hook sees markexpr = "compose and smoke" (has "compose" in it)
    # So it returns early (does nothing)
    # Marker filter selects tests with both compose and smoke: test_compose_smoke
    # Result: 1 collected (shown as "1/4 tests collected")
    assert "1/" in result.outlines[-1] and "collected" in result.outlines[-1]


def test_marker_not_compose_excludes_compose(test_project):
    """pytest -m 'not compose' should exclude compose tests (double-filter, no crash)."""
    result = test_project.runpytest("-m", "not compose", "--collect-only", "-q")

    # The hook sees markexpr = "not compose" (has "compose" in it)
    # So it returns early (does nothing)
    # Marker filter selects tests not marked with compose
    # Result: test_no_marker, test_unit = 2 collected (shown as "2/4 tests collected")
    # (test_compose_only and test_compose_smoke are both filtered out by the marker expression)
    assert "2/" in result.outlines[-1] and "collected" in result.outlines[-1]


def test_marker_compose_alone_includes_all_compose(test_project):
    """pytest -m 'compose' should collect all compose-marked tests."""
    result = test_project.runpytest("-m", "compose", "--collect-only", "-q")

    # The hook sees markexpr = "compose" (has "compose" in it)
    # So it returns early (does nothing)
    # Marker filter selects tests marked with compose
    # Result: test_compose_only, test_compose_smoke = 2 collected (shown as "2/4 tests collected")
    assert "2/" in result.outlines[-1] and "collected" in result.outlines[-1]
