"""Write-path model-identity guard + legacy-adopt policy (#311 item 4;
PR #314 review finding 3).

Covers ``WeaviateService._collection_is_empty`` and
``_check_or_stamp_collection_identity`` directly, mocking the weaviate-client
collection object -- the same style as ``tests/test_weaviate.py``. This is
the write-side counterpart to inh-contracts' ``tests/test_embedding_identity.
py`` (which pins the pure ``resolve_identity`` policy function these methods
call into) and to inh-public-api-svc's ``tests/unit/
test_search_embedding_identity.py`` (the read-path guard).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from inh_contracts.embedding.identity import (
    EmbeddingIdentityAdoptionRequiredError,
    EmbeddingIdentityMismatchError,
    encode_identity,
)
from inh_contracts.embedding.provider import EmbeddingIdentity

from src.config.settings import Settings
from src.services.weaviate import WeaviateService

CURRENT = EmbeddingIdentity(model_id="BAAI/bge-small-en-v1.5", dimension=384)


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture

    (tests/conftest.py) -- these tests are pure mocked-collection unit tests
    with no database involved, same pattern as
    tests/test_weaviate_store_budget.py and
    tests/test_settings_config_dedup_contract.py.
    """
    yield


@pytest.fixture(autouse=True)
def stub_active_identity(monkeypatch):
    """Pin the "active provider identity" the guard compares against."""
    monkeypatch.setattr(
        "src.services.embedder.get_active_embedding_identity", lambda: CURRENT, raising=False
    )


def _service(*, allow_adopt_unstamped: bool = False) -> WeaviateService:
    settings = MagicMock(spec=Settings)
    settings.weaviate_url = "http://localhost:8080"
    settings.weaviate_api_key = None
    settings.embedding_adopt_unstamped_collections = allow_adopt_unstamped
    return WeaviateService(settings)


def _mt_collection(*, description: str | None, tenants: dict | None) -> MagicMock:
    """A multi-tenant workspace collection (the #12 shape)."""
    coll = MagicMock()
    coll.config.get.return_value = MagicMock(
        description=description,
        multi_tenancy_config=MagicMock(enabled=True),
    )
    coll.tenants.get.return_value = tenants if tenants is not None else {}
    return coll


def _non_mt_collection(*, description: str | None, total_count: int) -> MagicMock:
    """A plain, non-multi-tenant collection (the legacy DOCUMENT_CHUNKS_COLLECTION shape)."""
    coll = MagicMock()
    coll.config.get.return_value = MagicMock(
        description=description,
        multi_tenancy_config=MagicMock(enabled=False),
    )
    coll.aggregate.over_all.return_value = MagicMock(total_count=total_count)
    return coll


# --- _collection_is_empty --------------------------------------------------------------------


def test_is_empty_multi_tenant_with_no_tenants() -> None:
    svc = _service()
    coll = _mt_collection(description=None, tenants={})
    assert svc._collection_is_empty(coll) is True


def test_is_empty_multi_tenant_with_a_tenant_is_not_empty() -> None:
    """Conservative proxy: any tenant existing means NOT proven empty, even
    though this particular tenant might hold zero objects."""
    svc = _service()
    coll = _mt_collection(description=None, tenants={"User_abc": MagicMock()})
    assert svc._collection_is_empty(coll) is False


def test_is_empty_non_multi_tenant_zero_objects() -> None:
    svc = _service()
    coll = _non_mt_collection(description=None, total_count=0)
    assert svc._collection_is_empty(coll) is True


def test_is_empty_non_multi_tenant_with_objects() -> None:
    svc = _service()
    coll = _non_mt_collection(description=None, total_count=42)
    assert svc._collection_is_empty(coll) is False


def test_is_empty_fails_closed_on_error() -> None:
    """A check that itself fails must be treated as NOT proven empty --
    fail closed, same conservative direction as everything else here."""
    svc = _service()
    coll = MagicMock()
    coll.config.get.side_effect = RuntimeError("schema fetch failed")
    assert svc._collection_is_empty(coll) is False


# --- _check_or_stamp_collection_identity -- the five branches --------------------------------


def test_stamped_matching_identity_passes_and_does_not_rewrite() -> None:
    svc = _service()
    coll = _mt_collection(description=encode_identity(CURRENT), tenants={"t": MagicMock()})

    svc._check_or_stamp_collection_identity(coll, "Workspace_x")

    coll.config.update.assert_not_called()


def test_stamped_mismatched_identity_raises() -> None:
    svc = _service()
    stale = EmbeddingIdentity(model_id="some-other-model", dimension=384)
    coll = _mt_collection(description=encode_identity(stale), tenants={"t": MagicMock()})

    with pytest.raises(EmbeddingIdentityMismatchError):
        svc._check_or_stamp_collection_identity(coll, "Workspace_x")
    coll.config.update.assert_not_called()


def test_unstamped_empty_collection_adopts_silently() -> None:
    svc = _service(allow_adopt_unstamped=False)  # opt-in irrelevant when empty
    coll = _mt_collection(description=None, tenants={})

    svc._check_or_stamp_collection_identity(coll, "Workspace_fresh")

    coll.config.update.assert_called_once_with(description=encode_identity(CURRENT))


def test_unstamped_nonempty_collection_without_optin_raises() -> None:
    svc = _service(allow_adopt_unstamped=False)
    coll = _mt_collection(description=None, tenants={"User_abc": MagicMock()})

    with pytest.raises(EmbeddingIdentityAdoptionRequiredError, match="Workspace_legacy"):
        svc._check_or_stamp_collection_identity(coll, "Workspace_legacy")
    coll.config.update.assert_not_called()


def test_unstamped_nonempty_collection_without_optin_is_a_mismatch_error() -> None:
    """Must be catchable by the existing `except EmbeddingIdentityMismatchError:
    raise` guards at both call sites without any call-site change."""
    svc = _service(allow_adopt_unstamped=False)
    coll = _mt_collection(description=None, tenants={"User_abc": MagicMock()})

    with pytest.raises(EmbeddingIdentityMismatchError):
        svc._check_or_stamp_collection_identity(coll, "Workspace_legacy")


def test_unstamped_nonempty_collection_with_optin_adopts_and_logs(monkeypatch) -> None:
    from src.services import weaviate as weaviate_module

    warnings: list[dict] = []
    monkeypatch.setattr(
        weaviate_module.logger,
        "warning",
        lambda event, **kw: warnings.append({"event": event, **kw}),
        raising=True,
    )
    svc = _service(allow_adopt_unstamped=True)
    coll = _mt_collection(description=None, tenants={"User_abc": MagicMock()})

    svc._check_or_stamp_collection_identity(coll, "Workspace_legacy")

    coll.config.update.assert_called_once_with(description=encode_identity(CURRENT))
    adopt_warnings = [
        w
        for w in warnings
        if w["event"] == "embedding_identity_adopted_unstamped_nonempty_collection"
    ]
    assert len(adopt_warnings) == 1
    assert adopt_warnings[0]["collection"] == "Workspace_legacy"


def test_non_multi_tenant_unstamped_nonempty_without_optin_raises() -> None:
    """Same policy applies to the non-MT legacy DOCUMENT_CHUNKS_COLLECTION shape."""
    svc = _service(allow_adopt_unstamped=False)
    coll = _non_mt_collection(description=None, total_count=100)

    with pytest.raises(EmbeddingIdentityAdoptionRequiredError):
        svc._check_or_stamp_collection_identity(coll, "DocumentChunk")


def test_non_multi_tenant_unstamped_empty_adopts() -> None:
    svc = _service(allow_adopt_unstamped=False)
    coll = _non_mt_collection(description=None, total_count=0)

    svc._check_or_stamp_collection_identity(coll, "DocumentChunk")

    coll.config.update.assert_called_once_with(description=encode_identity(CURRENT))
