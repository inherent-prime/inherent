"""Config-based per-workspace usage-based ranking boost weight (inherent#394).

WORKSPACE_REUSE_BOOST is parsed once at Settings construction (startup),
mirroring WORKSPACE_HYBRID_ALPHA (see test_workspace_hybrid_alpha_settings.py)
-- same "fail loudly at startup, not at query time" contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config.settings import Settings


class TestSettingsParsing:
    def test_unset_is_empty(self):
        s = Settings(_env_file=None)
        assert s.workspace_reuse_boost == {}

    def test_parses_mapping(self):
        s = Settings(_env_file=None, workspace_reuse_boost_raw="ws_a=0.1,ws_b=0.3")
        assert s.workspace_reuse_boost == {"ws_a": 0.1, "ws_b": 0.3}

    def test_malformed_value_fails_construction(self):
        """Startup, not query time, is where this must fail."""
        with pytest.raises(ValidationError, match="WORKSPACE_REUSE_BOOST"):
            Settings(_env_file=None, workspace_reuse_boost_raw="not-a-mapping")

    def test_out_of_range_weight_fails_construction(self):
        with pytest.raises(ValidationError, match="outside"):
            Settings(_env_file=None, workspace_reuse_boost_raw="ws_a=1.5")

    def test_duplicate_workspace_id_fails_construction(self):
        with pytest.raises(ValidationError, match="bound twice"):
            Settings(_env_file=None, workspace_reuse_boost_raw="ws_a=0.1,ws_a=0.3")
