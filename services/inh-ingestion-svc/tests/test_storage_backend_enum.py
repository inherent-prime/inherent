"""Ingestion STORAGE_BACKEND must match the shared contract (#27).

The contract and DB allow 'azure', but the ingestion Settings Literal omitted
it, so STORAGE_BACKEND=azure crashed at settings load even though 'azure' is a
first-class value everywhere else.
"""

from __future__ import annotations

from typing import get_args

from src.config.settings import Settings


def test_storage_backend_allows_azure():
    field = Settings.model_fields["storage_backend"]
    assert "azure" in get_args(field.annotation)


def test_storage_backend_matches_contract():
    from inh_contracts.events import StorageBackend

    field = Settings.model_fields["storage_backend"]
    assert set(get_args(field.annotation)) == set(get_args(StorageBackend))
