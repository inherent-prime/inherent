"""WORKSPACE_REUSE_BOOST parsing tests (inherent#394)."""

import pytest

from inh_contracts.workspace_reuse_boost import (
    WorkspaceReuseBoostError,
    parse_workspace_reuse_boost,
)


def test_none_is_empty():
    assert parse_workspace_reuse_boost(None) == {}


def test_empty_string_is_empty():
    assert parse_workspace_reuse_boost("") == {}


def test_whitespace_only_is_empty():
    assert parse_workspace_reuse_boost("   ") == {}


def test_single_entry():
    assert parse_workspace_reuse_boost("ws_a=0.1") == {"ws_a": 0.1}


def test_multiple_entries():
    assert parse_workspace_reuse_boost("ws_a=0.1,ws_b=0.5") == {"ws_a": 0.1, "ws_b": 0.5}


def test_whitespace_around_entries_and_sides_is_trimmed():
    assert parse_workspace_reuse_boost(" ws_a = 0.1 , ws_b=0.5 ") == {
        "ws_a": 0.1,
        "ws_b": 0.5,
    }


def test_trailing_comma_is_ignored():
    assert parse_workspace_reuse_boost("ws_a=0.1,") == {"ws_a": 0.1}


def test_boundary_values_accepted():
    assert parse_workspace_reuse_boost("ws_a=0.0,ws_b=1.0") == {"ws_a": 0.0, "ws_b": 1.0}


def test_missing_equals_raises():
    with pytest.raises(WorkspaceReuseBoostError, match="missing '='"):
        parse_workspace_reuse_boost("ws_a_0.1")


def test_empty_workspace_id_raises():
    with pytest.raises(WorkspaceReuseBoostError, match="empty workspace_id"):
        parse_workspace_reuse_boost("=0.1")


def test_empty_weight_raises():
    with pytest.raises(WorkspaceReuseBoostError, match="empty workspace_id"):
        parse_workspace_reuse_boost("ws_a=")


def test_double_equals_raises():
    with pytest.raises(WorkspaceReuseBoostError, match="more than one '='"):
        parse_workspace_reuse_boost("ws_a=0.1=0.2")


def test_non_numeric_weight_raises():
    with pytest.raises(WorkspaceReuseBoostError, match="non-numeric"):
        parse_workspace_reuse_boost("ws_a=support")


def test_out_of_range_weight_raises():
    with pytest.raises(WorkspaceReuseBoostError, match="outside \\[0.0, 1.0\\]"):
        parse_workspace_reuse_boost("ws_a=1.5")


def test_negative_weight_raises():
    with pytest.raises(WorkspaceReuseBoostError, match="outside \\[0.0, 1.0\\]"):
        parse_workspace_reuse_boost("ws_a=-0.1")


def test_duplicate_workspace_id_raises():
    with pytest.raises(WorkspaceReuseBoostError, match="bound twice"):
        parse_workspace_reuse_boost("ws_a=0.1,ws_a=0.5")
