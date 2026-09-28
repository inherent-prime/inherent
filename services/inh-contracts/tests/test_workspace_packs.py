"""WORKSPACE_VERTICAL_PACKS parsing tests (inherent#390 follow-up)."""

import pytest

from inh_contracts.workspace_packs import (
    WorkspaceVerticalPacksError,
    parse_workspace_vertical_packs,
)


def test_none_is_empty():
    assert parse_workspace_vertical_packs(None) == {}


def test_empty_string_is_empty():
    assert parse_workspace_vertical_packs("") == {}


def test_whitespace_only_is_empty():
    assert parse_workspace_vertical_packs("   ") == {}


def test_single_entry():
    assert parse_workspace_vertical_packs("ws_abc=support") == {"ws_abc": "support"}


def test_multiple_entries():
    assert parse_workspace_vertical_packs("ws_abc=support,ws_def=handbook") == {
        "ws_abc": "support",
        "ws_def": "handbook",
    }


def test_whitespace_around_entries_and_sides_is_trimmed():
    assert parse_workspace_vertical_packs(" ws_abc = support , ws_def=handbook ") == {
        "ws_abc": "support",
        "ws_def": "handbook",
    }


def test_trailing_comma_is_ignored():
    assert parse_workspace_vertical_packs("ws_abc=support,") == {"ws_abc": "support"}


def test_missing_equals_raises():
    with pytest.raises(WorkspaceVerticalPacksError, match="missing '='"):
        parse_workspace_vertical_packs("ws_abc_support")


def test_empty_workspace_id_raises():
    with pytest.raises(WorkspaceVerticalPacksError, match="empty workspace_id"):
        parse_workspace_vertical_packs("=support")


def test_empty_pack_name_raises():
    with pytest.raises(WorkspaceVerticalPacksError, match="empty workspace_id"):
        parse_workspace_vertical_packs("ws_abc=")


def test_double_equals_raises():
    with pytest.raises(WorkspaceVerticalPacksError, match="more than one '='"):
        parse_workspace_vertical_packs("ws_abc=support=extra")


def test_duplicate_workspace_id_raises():
    with pytest.raises(WorkspaceVerticalPacksError, match="bound twice"):
        parse_workspace_vertical_packs("ws_abc=support,ws_abc=handbook")


def test_duplicate_workspace_id_same_pack_still_raises():
    """Even binding the same pack twice is rejected -- one canonical entry only."""
    with pytest.raises(WorkspaceVerticalPacksError, match="bound twice"):
        parse_workspace_vertical_packs("ws_abc=support,ws_abc=support")
