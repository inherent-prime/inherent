"""WORKSPACE_HYBRID_ALPHA parsing tests (inherent#391)."""

import pytest

from inh_contracts.workspace_hybrid_alpha import (
    WorkspaceHybridAlphaError,
    parse_workspace_hybrid_alpha,
)


def test_none_is_empty():
    assert parse_workspace_hybrid_alpha(None) == {}


def test_empty_string_is_empty():
    assert parse_workspace_hybrid_alpha("") == {}


def test_whitespace_only_is_empty():
    assert parse_workspace_hybrid_alpha("   ") == {}


def test_single_entry():
    assert parse_workspace_hybrid_alpha("ws_a=0.3") == {"ws_a": 0.3}


def test_multiple_entries():
    assert parse_workspace_hybrid_alpha("ws_a=0.3,ws_b=0.5") == {"ws_a": 0.3, "ws_b": 0.5}


def test_whitespace_around_entries_and_sides_is_trimmed():
    assert parse_workspace_hybrid_alpha(" ws_a = 0.3 , ws_b=0.5 ") == {
        "ws_a": 0.3,
        "ws_b": 0.5,
    }


def test_trailing_comma_is_ignored():
    assert parse_workspace_hybrid_alpha("ws_a=0.3,") == {"ws_a": 0.3}


def test_boundary_values_accepted():
    assert parse_workspace_hybrid_alpha("ws_a=0.0,ws_b=1.0") == {"ws_a": 0.0, "ws_b": 1.0}


def test_missing_equals_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="missing '='"):
        parse_workspace_hybrid_alpha("ws_a_0.3")


def test_empty_workspace_id_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="empty workspace_id"):
        parse_workspace_hybrid_alpha("=0.3")


def test_empty_alpha_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="empty workspace_id"):
        parse_workspace_hybrid_alpha("ws_a=")


def test_double_equals_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="more than one '='"):
        parse_workspace_hybrid_alpha("ws_a=0.3=0.4")


def test_non_numeric_alpha_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="non-numeric"):
        parse_workspace_hybrid_alpha("ws_a=support")


def test_out_of_range_alpha_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="outside \\[0.0, 1.0\\]"):
        parse_workspace_hybrid_alpha("ws_a=1.5")


def test_negative_alpha_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="outside \\[0.0, 1.0\\]"):
        parse_workspace_hybrid_alpha("ws_a=-0.1")


def test_duplicate_workspace_id_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="bound twice"):
        parse_workspace_hybrid_alpha("ws_a=0.3,ws_a=0.5")


def test_duplicate_workspace_id_same_value_still_raises():
    with pytest.raises(WorkspaceHybridAlphaError, match="bound twice"):
        parse_workspace_hybrid_alpha("ws_a=0.3,ws_a=0.3")
