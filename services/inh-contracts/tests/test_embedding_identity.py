"""Model-identity guard (#311 item 4 -- HIGHEST SEVERITY).

Covers exactly the four cases the issue calls out: matching identity passes,
mismatched model_id raises, mismatched dimension raises, and an unstamped
legacy collection follows the documented adopt policy.
"""

from __future__ import annotations

import pytest

from inh_contracts.embedding.identity import (
    EmbeddingIdentityMismatchError,
    decode_identity,
    encode_identity,
    resolve_identity,
)
from inh_contracts.embedding.provider import EmbeddingIdentity

CURRENT = EmbeddingIdentity(model_id="BAAI/bge-small-en-v1.5", dimension=384)


def test_matching_identity_passes_and_returns_persisted_unchanged() -> None:
    persisted = EmbeddingIdentity(model_id="BAAI/bge-small-en-v1.5", dimension=384)
    result = resolve_identity(persisted=persisted, current=CURRENT, collection_name="Workspace_x")
    assert result == persisted


def test_mismatched_model_id_raises() -> None:
    persisted = EmbeddingIdentity(model_id="some-other-model", dimension=384)
    with pytest.raises(EmbeddingIdentityMismatchError, match="Workspace_x"):
        resolve_identity(persisted=persisted, current=CURRENT, collection_name="Workspace_x")


def test_mismatched_dimension_raises() -> None:
    persisted = EmbeddingIdentity(model_id="BAAI/bge-small-en-v1.5", dimension=768)
    with pytest.raises(EmbeddingIdentityMismatchError):
        resolve_identity(persisted=persisted, current=CURRENT, collection_name="Workspace_x")


def test_mismatch_error_message_names_both_identities() -> None:
    persisted = EmbeddingIdentity(model_id="old-model", dimension=768)
    with pytest.raises(EmbeddingIdentityMismatchError) as exc_info:
        resolve_identity(persisted=persisted, current=CURRENT, collection_name="Workspace_x")
    message = str(exc_info.value)
    assert "old-model" in message
    assert "768" in message
    assert CURRENT.model_id in message
    assert str(CURRENT.dimension) in message


def test_unstamped_legacy_collection_adopts_current_identity() -> None:
    """No persisted identity (persisted=None) -> adopt, never raise."""
    result = resolve_identity(persisted=None, current=CURRENT, collection_name="Workspace_legacy")
    assert result == CURRENT


# --- encode/decode round trip ---------------------------------------------------------------


def test_encode_decode_round_trip() -> None:
    encoded = encode_identity(CURRENT)
    assert decode_identity(encoded) == CURRENT


def test_decode_none_is_unstamped() -> None:
    assert decode_identity(None) is None


def test_decode_empty_string_is_unstamped() -> None:
    assert decode_identity("") is None


def test_decode_human_authored_description_is_unstamped_not_a_crash() -> None:
    """A collection description predating #311 must fall back to the legacy policy."""
    assert decode_identity("Chunks for workspace 42") is None


def test_decode_non_string_is_unstamped() -> None:
    assert decode_identity(object()) is None
    assert decode_identity(123) is None


def test_decode_malformed_json_behind_prefix_is_unstamped_not_a_crash() -> None:
    from inh_contracts.embedding.identity import _IDENTITY_PREFIX

    assert decode_identity(_IDENTITY_PREFIX + "{not json") is None


def test_decode_prefixed_but_missing_fields_is_unstamped() -> None:
    from inh_contracts.embedding.identity import _IDENTITY_PREFIX

    assert decode_identity(_IDENTITY_PREFIX + '{"model_id": "m"}') is None
