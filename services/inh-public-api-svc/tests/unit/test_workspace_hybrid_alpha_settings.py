"""Config-based per-workspace hybrid alpha (inherent#391).

WORKSPACE_HYBRID_ALPHA is parsed once at Settings construction (startup),
mirroring WORKSPACE_VERTICAL_PACKS (see test_workspace_vertical_packs_binding.py) --
same "fail loudly at startup, not at query time" contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config.settings import Settings


class TestSettingsParsing:
    def test_unset_is_empty(self):
        s = Settings(_env_file=None)
        assert s.workspace_hybrid_alpha == {}

    def test_parses_mapping(self):
        s = Settings(_env_file=None, workspace_hybrid_alpha_raw="ws_a=0.3,ws_b=0.5")
        assert s.workspace_hybrid_alpha == {"ws_a": 0.3, "ws_b": 0.5}

    def test_malformed_value_fails_construction(self):
        """Startup, not query time, is where this must fail."""
        with pytest.raises(ValidationError, match="WORKSPACE_HYBRID_ALPHA"):
            Settings(_env_file=None, workspace_hybrid_alpha_raw="not-a-mapping")

    def test_out_of_range_alpha_fails_construction(self):
        with pytest.raises(ValidationError, match="outside"):
            Settings(_env_file=None, workspace_hybrid_alpha_raw="ws_a=1.5")

    def test_duplicate_workspace_id_fails_construction(self):
        with pytest.raises(ValidationError, match="bound twice"):
            Settings(_env_file=None, workspace_hybrid_alpha_raw="ws_a=0.3,ws_a=0.5")
