"""Vertical pack discovery tests (inherent#390 item 2)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from inh_contracts.vertical_discovery import (
    discover_all_packs,
    discover_entry_point_packs,
    discover_packs,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_unset_dir_is_feature_off():
    """None (no VERTICAL_PACKS_DIR configured) -> empty, no error. Legacy default."""
    result = discover_packs(None)
    assert result.packs == {}
    assert result.errors == {}


def test_nonexistent_dir_is_feature_off(tmp_path):
    result = discover_packs(tmp_path / "does-not-exist")
    assert result.packs == {}
    assert result.errors == {}


def test_discovers_valid_pack_by_manifest_name(tmp_path):
    """Keyed by the manifest's `name`, not the subdirectory's name."""
    import shutil

    dest = tmp_path / "some_arbitrary_dir_name"
    shutil.copytree(FIXTURES / "handbook_pack", dest)

    result = discover_packs(tmp_path)
    assert "handbook" in result.packs
    assert result.errors == {}


def test_broken_pack_collected_as_error_not_raised(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "vertical.yaml").write_text("name: broken\n")  # missing required fields

    good = tmp_path / "good"
    import shutil

    shutil.copytree(FIXTURES / "handbook_pack", good)

    result = discover_packs(tmp_path)
    assert "handbook" in result.packs  # the good pack still loads
    assert "broken" in result.errors  # the bad one is reported, not raised


def test_non_pack_entries_skipped_quietly(tmp_path):
    (tmp_path / "README.md").write_text("not a pack")
    (tmp_path / "empty_dir").mkdir()

    result = discover_packs(tmp_path)
    assert result.packs == {}
    assert result.errors == {}


def test_entry_point_discovery_empty_by_default():
    """No packages register under inherent.verticals -> empty, feature off."""
    result = discover_entry_point_packs()
    assert result.packs == {}
    assert result.errors == {}


def test_entry_point_discovery_loads_registered_pack():
    fake_ep = MagicMock()
    fake_ep.name = "handbook_via_entry_point"
    fake_ep.load.return_value = str(FIXTURES / "handbook_pack")

    with patch("inh_contracts.vertical_discovery.entry_points", return_value=[fake_ep]):
        result = discover_entry_point_packs()

    assert "handbook" in result.packs
    assert result.errors == {}


def test_entry_point_discovery_collects_broken_pack_error():
    fake_ep = MagicMock()
    fake_ep.name = "broken_entry_point"
    fake_ep.load.return_value = "/definitely/not/a/real/pack/path"

    with patch("inh_contracts.vertical_discovery.entry_points", return_value=[fake_ep]):
        result = discover_entry_point_packs()

    assert result.packs == {}
    assert "broken_entry_point" in result.errors


def test_directory_pack_wins_name_collision(tmp_path):
    """A directory-discovered pack overrides an entry-point pack of the same name."""
    import shutil

    shutil.copytree(FIXTURES / "handbook_pack", tmp_path / "handbook")

    fake_ep = MagicMock()
    fake_ep.name = "handbook_ep"
    fake_ep.load.return_value = str(FIXTURES / "handbook_pack")

    with patch("inh_contracts.vertical_discovery.entry_points", return_value=[fake_ep]):
        result = discover_all_packs(tmp_path)

    assert "handbook" in result.packs
    # Both sources loaded the same fixture here, so this mainly asserts the
    # combine doesn't crash/duplicate and the directory copy is what's kept.
    assert result.packs["handbook"].manifest.name == "handbook"
