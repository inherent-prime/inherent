"""Model-identity guard (#311 item 4 -- HIGHEST SEVERITY).

Covers exactly the four cases the issue calls out: matching identity passes,
mismatched model_id raises, mismatched dimension raises, and an unstamped
legacy collection follows the documented adopt policy.
"""

from __future__ import annotations

import pytest

from inh_contracts.embedding.identity import (
    EmbeddingIdentityAdoptionRequiredError,
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


# --- legacy-adopt policy (PR #314 review finding 3) ------------------------------------------
#
# An unstamped collection used to be adopted unconditionally -- which let an
# operator who upgraded and switched providers in the same deploy get
# pre-existing vectors from a DIFFERENT model permanently certified as
# matching. The five branches below are exactly the ones the review asked
# to be pinned: empty+unstamped, non-empty+unstamped with the opt-in off,
# non-empty+unstamped with the opt-in on, stamped+match (above), and
# stamped+mismatch (above).


def test_unstamped_empty_collection_adopts_silently() -> None:
    """Nothing to be wrong about in an empty collection -- always safe to adopt."""
    result = resolve_identity(
        persisted=None, current=CURRENT, collection_name="Workspace_legacy", is_empty=True
    )
    assert result == CURRENT


def test_unstamped_nonempty_collection_without_optin_raises() -> None:
    """The dangerous case: refuse to silently certify unverifiable vectors."""
    with pytest.raises(EmbeddingIdentityAdoptionRequiredError, match="Workspace_legacy"):
        resolve_identity(
            persisted=None,
            current=CURRENT,
            collection_name="Workspace_legacy",
            is_empty=False,
            allow_adopt_unstamped=False,
        )


def test_unstamped_nonempty_collection_without_optin_error_is_a_mismatch_error() -> None:
    """Must subclass EmbeddingIdentityMismatchError so every existing
    ``except EmbeddingIdentityMismatchError: raise`` guard on the write path
    catches this too, without any call site needing to change."""
    with pytest.raises(EmbeddingIdentityMismatchError):
        resolve_identity(
            persisted=None,
            current=CURRENT,
            collection_name="Workspace_legacy",
            is_empty=False,
            allow_adopt_unstamped=False,
        )


def test_unstamped_nonempty_collection_with_optin_adopts() -> None:
    """Explicit operator opt-in (EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS=true) is honored."""
    result = resolve_identity(
        persisted=None,
        current=CURRENT,
        collection_name="Workspace_legacy",
        is_empty=False,
        allow_adopt_unstamped=True,
    )
    assert result == CURRENT


def test_unstamped_unknown_emptiness_defaults_to_the_safe_non_adopt_branch() -> None:
    """is_empty=None (not checked) must behave like is_empty=False, not True --
    the safe default is "prove it", never "assume it"."""
    with pytest.raises(EmbeddingIdentityAdoptionRequiredError):
        resolve_identity(
            persisted=None, current=CURRENT, collection_name="Workspace_legacy", is_empty=None
        )


def test_adoption_required_error_names_the_opt_in_env_var() -> None:
    """The error must tell the operator how to proceed, not just that it failed."""
    with pytest.raises(EmbeddingIdentityAdoptionRequiredError) as exc_info:
        resolve_identity(persisted=None, current=CURRENT, collection_name="Workspace_legacy")
    assert "EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS" in str(exc_info.value)
    assert "Workspace_legacy" in str(exc_info.value)


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
