"""WORKSPACE_REUSE_DETECTION parsing tests (inherent#394)."""

import pytest

from inh_contracts.workspace_reuse_detection import (
    WorkspaceReuseDetectionError,
    parse_workspace_reuse_detection,
)


def test_none_is_empty():
    assert parse_workspace_reuse_detection(None) == set()


def test_empty_string_is_empty():
    assert parse_workspace_reuse_detection("") == set()


def test_whitespace_only_is_empty():
    assert parse_workspace_reuse_detection("   ") == set()


def test_single_entry():
    assert parse_workspace_reuse_detection("ws_a") == {"ws_a"}


def test_multiple_entries():
    assert parse_workspace_reuse_detection("ws_a,ws_b") == {"ws_a", "ws_b"}


def test_whitespace_around_entries_is_trimmed():
    assert parse_workspace_reuse_detection(" ws_a , ws_b ") == {"ws_a", "ws_b"}


def test_trailing_comma_is_ignored():
    assert parse_workspace_reuse_detection("ws_a,") == {"ws_a"}


def test_equals_sign_raises():
    with pytest.raises(WorkspaceReuseDetectionError, match="contains '='"):
        parse_workspace_reuse_detection("ws_a=true")


def test_duplicate_workspace_id_raises():
    with pytest.raises(WorkspaceReuseDetectionError, match="listed twice"):
        parse_workspace_reuse_detection("ws_a,ws_a")
