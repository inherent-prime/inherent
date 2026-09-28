"""Config-based workspace -> vertical pack binding (inherent#390 follow-up).

WORKSPACE_VERTICAL_PACKS is parsed once at Settings construction (startup);
resolve_workspace_pack looks the workspace up in that mapping and loads the
named pack from VERTICAL_PACKS_DIR.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.services.workspace_pack import _discover_packs_cached, resolve_workspace_pack

FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures"


class TestSettingsParsing:
    def test_unset_is_empty(self):
        s = Settings(_env_file=None)
        assert s.workspace_vertical_packs == {}

    def test_parses_mapping(self):
        s = Settings(_env_file=None, workspace_vertical_packs_raw="ws_abc=support,ws_def=handbook")
        assert s.workspace_vertical_packs == {"ws_abc": "support", "ws_def": "handbook"}

    def test_malformed_value_fails_construction(self):
        """Startup, not query time, is where this must fail."""
        with pytest.raises(ValidationError, match="WORKSPACE_VERTICAL_PACKS"):
            Settings(_env_file=None, workspace_vertical_packs_raw="not-a-mapping")

    def test_duplicate_workspace_id_fails_construction(self):
        with pytest.raises(ValidationError, match="bound twice"):
            Settings(_env_file=None, workspace_vertical_packs_raw="ws_abc=support,ws_abc=handbook")


class TestResolveWorkspacePack:
    def setup_method(self):
        _discover_packs_cached.cache_clear()

    def teardown_method(self):
        _discover_packs_cached.cache_clear()

    def test_unmapped_workspace_returns_none(self):
        fake_settings = Settings(
            _env_file=None,
            vertical_packs_dir=str(FIXTURE_PACKS_DIR),
            workspace_vertical_packs_raw="ws_abc=handbook",
        )
        with patch("src.services.workspace_pack.settings", fake_settings):
            assert resolve_workspace_pack("ws_unmapped") is None

    def test_mapped_workspace_with_dir_unset_returns_none(self):
        """Feature-off (no VERTICAL_PACKS_DIR) always wins, even if the
        workspace has a mapping -- there is nowhere to load the pack from."""
        fake_settings = Settings(
            _env_file=None,
            vertical_packs_dir=None,
            workspace_vertical_packs_raw="ws_abc=handbook",
        )
        with patch("src.services.workspace_pack.settings", fake_settings):
            assert resolve_workspace_pack("ws_abc") is None

    def test_mapped_workspace_resolves_the_named_pack(self):
        fake_settings = Settings(
            _env_file=None,
            vertical_packs_dir=str(FIXTURE_PACKS_DIR),
            workspace_vertical_packs_raw="ws_abc=handbook",
        )
        with patch("src.services.workspace_pack.settings", fake_settings):
            vertical = resolve_workspace_pack("ws_abc")
        assert vertical is not None
        assert vertical.manifest.name == "handbook"

    def test_mapped_to_unknown_pack_name_returns_none(self):
        """The mapping names a pack that isn't actually under VERTICAL_PACKS_DIR
        -- degrades to None rather than raising (one bad mapping must not
        break search/tagging for every other workspace)."""
        fake_settings = Settings(
            _env_file=None,
            vertical_packs_dir=str(FIXTURE_PACKS_DIR),
            workspace_vertical_packs_raw="ws_abc=no-such-pack",
        )
        with patch("src.services.workspace_pack.settings", fake_settings):
            assert resolve_workspace_pack("ws_abc") is None
