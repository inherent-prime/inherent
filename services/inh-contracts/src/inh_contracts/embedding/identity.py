"""Model-identity guard for Weaviate collections (#311 item 4 -- HIGHEST SEVERITY).

Weaviate collections are created with ``Configure.Vectorizer.none()`` and
never declare a dimension, so Weaviate just pins vector width at first
insert. Querying with model A against a collection built with model B
returns plausible-looking noise with NO error anywhere -- this module exists
to turn that into a loud, immediate failure instead.

Policy (deliberate, documented -- see docs/reference/configuration.md and the
#311 issue): the provider's active identity (model_id, dimension) is
persisted as the collection's Weaviate ``description`` (JSON, behind a
recognizable prefix so a human-authored description is never misread as
identity data). On the write path:

- No persisted identity (a legacy collection created before #311, or brand
  new) -> ADOPT: stamp the collection with the active identity now. This is
  what keeps `make up` / an existing deployment working with zero manual
  migration.
- A persisted identity that matches the active one -> pass silently.
- A persisted identity that does NOT match -> hard error
  (``EmbeddingIdentityMismatchError``), always -- never a warning. The only
  supported recovery is the (out-of-scope-for-#311, tracked separately)
  dimension-migration workflow: shadow collection, backfill, cutover.

The query path (public-api, read-only against Weaviate's schema over HTTP)
follows the same match/mismatch rule but never itself stamps an unstamped
collection -- see ``inh-public-api-svc/src/services/search.py`` for why that
is a safe, deliberate asymmetry, not an oversight.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from inh_contracts.embedding.provider import EmbeddingIdentity

__all__ = [
    "EmbeddingIdentity",
    "EmbeddingIdentityMismatchError",
    "encode_identity",
    "decode_identity",
    "resolve_identity",
]

# Marks a Weaviate collection `description` as machine-owned identity JSON,
# not a human-authored note, so decode_identity() can tell them apart instead
# of misparsing free text as identity data (or vice versa).
_IDENTITY_PREFIX = "inh:embedding-identity:"


class EmbeddingIdentityMismatchError(RuntimeError):
    """The active embedding provider's identity does not match what a
    Weaviate collection was built with.

    Always a hard error -- raised on both the write and query paths, never
    logged-and-swallowed as a warning. Recovery requires a deliberate
    migration (dimension-migration shadow-collection/backfill/cutover is
    tracked as a follow-up to #311, out of scope here), not retrying the
    same request.
    """


def encode_identity(identity: EmbeddingIdentity) -> str:
    """Serialize an identity for storage in a Weaviate collection's `description`."""
    return _IDENTITY_PREFIX + json.dumps(asdict(identity), sort_keys=True)


def decode_identity(raw: object) -> EmbeddingIdentity | None:
    """Parse a persisted identity, or ``None`` when there isn't a valid one.

    ``None`` covers every "nothing to assert against" case on purpose: an
    unset description, a human-authored description predating #311 (no
    prefix), and a malformed/foreign payload behind the prefix (defensive --
    a corrupt description must fall back to the legacy/adopt policy, not
    crash the caller).
    """
    if not isinstance(raw, str) or not raw.startswith(_IDENTITY_PREFIX):
        return None
    try:
        payload = json.loads(raw[len(_IDENTITY_PREFIX) :])
        return EmbeddingIdentity(
            model_id=str(payload["model_id"]), dimension=int(payload["dimension"])
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def resolve_identity(
    *,
    persisted: EmbeddingIdentity | None,
    current: EmbeddingIdentity,
    collection_name: str,
) -> EmbeddingIdentity:
    """Apply the adopt-or-assert policy; return the identity that should end up persisted.

    Args:
        persisted: The identity decoded from the collection's stored
            metadata, or ``None`` for an unstamped/legacy collection.
        current: The active provider's identity.
        collection_name: Only used to build a useful error message.

    Returns:
        ``current`` when adopting an unstamped collection (the caller is
        expected to persist it); ``persisted`` unchanged when it already
        matches ``current`` (nothing to write).

    Raises:
        EmbeddingIdentityMismatchError: ``persisted`` is set and disagrees
            with ``current`` on model_id and/or dimension.
    """
    if persisted is None:
        return current
    if persisted.model_id != current.model_id or persisted.dimension != current.dimension:
        raise EmbeddingIdentityMismatchError(
            f"Embedding identity mismatch on collection '{collection_name}': "
            f"persisted model_id={persisted.model_id!r} dimension={persisted.dimension} vs "
            f"active model_id={current.model_id!r} dimension={current.dimension}. "
            "The EMBEDDING_PROVIDER/EMBEDDING_MODEL_ID/EMBEDDING_DIM configuration changed "
            "without a deliberate migration, or two different embedding configs are pointed "
            "at the same Weaviate instance. See docs/reference/configuration.md#embedding "
            "for recovery."
        )
    return persisted
