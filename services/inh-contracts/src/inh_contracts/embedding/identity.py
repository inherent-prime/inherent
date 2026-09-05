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

- No persisted identity, and the collection is EMPTY (no objects) -> ADOPT
  silently: stamp the collection with the active identity now. There is
  nothing to be wrong about -- an empty collection cannot hold vectors from a
  different model. This is what keeps `make up` / a fresh deployment working
  with zero manual migration.
- No persisted identity, and the collection is NOT empty -> the vectors
  already in it were written by *something*, and adopting them as the
  current provider's would silently CERTIFY that as correct even when it
  is not (e.g. an operator who upgrades and switches providers in the same
  deploy -- PR #314 review finding 3). This is refused by default: raises
  ``EmbeddingIdentityAdoptionRequiredError`` naming the collection, unless
  the caller passes ``allow_adopt_unstamped=True`` (the ingestion write path
  wires this to the ``EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS`` operator
  opt-in, default off) -- in which case it adopts anyway, and the caller is
  expected to log loudly that it did.
- A persisted identity that matches the active one -> pass silently.
- A persisted identity that does NOT match -> hard error
  (``EmbeddingIdentityMismatchError``), always -- never a warning. The only
  supported recovery is the (out-of-scope-for-#311, tracked separately)
  dimension-migration workflow: shadow collection, backfill, cutover.

The query path (public-api, read-only against Weaviate's schema over HTTP)
follows the same match/mismatch rule for a STAMPED collection, but never
itself adopts -- it has no business writing Weaviate schema from a read
path, and cannot cheaply prove emptiness across every tenant of a
multi-tenant collection the way the write path can. Encountering an
unstamped collection on the query path is therefore never routed through
``resolve_identity``'s adopt gate at all; see
``inh-public-api-svc/src/services/search.py``'s ``_ensure_identity_checked``
for the read-path handling (log loudly, then proceed -- visible, not
silent, but not a hard failure either, since query has no way to fix what it
finds).
"""

from __future__ import annotations

import json
from dataclasses import asdict

from inh_contracts.embedding.provider import EmbeddingIdentity

__all__ = [
    "EmbeddingIdentity",
    "EmbeddingIdentityMismatchError",
    "EmbeddingIdentityAdoptionRequiredError",
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


class EmbeddingIdentityAdoptionRequiredError(EmbeddingIdentityMismatchError):
    """A non-empty, unstamped ("legacy") collection cannot be silently adopted.

    Deliberately a SUBCLASS of ``EmbeddingIdentityMismatchError`` (PR #314
    review finding 3) -- not a sibling exception -- so every existing
    ``except EmbeddingIdentityMismatchError: raise`` guard already in place
    on the write path (``inh-ingestion-svc/src/services/weaviate.py``) keeps
    catching and re-raising this one too, with no call site needing to
    change. Raised only when ``resolve_identity`` is asked to adopt a
    collection that is (a) unstamped AND (b) not known to be empty AND (c)
    the caller did not pass ``allow_adopt_unstamped=True``.
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
    is_empty: bool | None = None,
    allow_adopt_unstamped: bool = False,
) -> EmbeddingIdentity:
    """Apply the adopt-or-assert policy; return the identity that should end up persisted.

    Args:
        persisted: The identity decoded from the collection's stored
            metadata, or ``None`` for an unstamped/legacy collection.
        current: The active provider's identity.
        collection_name: Only used to build a useful error message.
        is_empty: Whether the collection is known to hold zero objects.
            Only consulted when ``persisted`` is ``None`` -- an empty
            collection can always be adopted silently, since there is
            nothing yet that could be wrong (PR #314 review finding 3).
            ``None`` (the default) means "not checked" and is treated the
            same as ``False`` (NOT known to be empty): the safe default is
            to require ``allow_adopt_unstamped`` rather than assume
            emptiness a caller never verified.
        allow_adopt_unstamped: Operator opt-in (wired to
            ``EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS`` on the ingestion write
            path) permitting a NON-empty unstamped collection to be adopted
            anyway. Only consulted when ``persisted is None`` and
            ``is_empty`` is not ``True``. Defaults to ``False`` -- adoption
            of unverifiable state must be an explicit choice, never silent.

    Returns:
        ``current`` when adopting (the collection is empty, or the operator
        opted in) -- the caller is expected to persist it and, in the
        opt-in case, log loudly that it did. ``persisted`` unchanged when it
        already matches ``current`` (nothing to write).

    Raises:
        EmbeddingIdentityMismatchError: ``persisted`` is set and disagrees
            with ``current`` on model_id and/or dimension.
        EmbeddingIdentityAdoptionRequiredError: ``persisted`` is ``None``,
            the collection is not known to be empty, and
            ``allow_adopt_unstamped`` was not set. A subclass of
            ``EmbeddingIdentityMismatchError`` -- see its docstring for why.
    """
    if persisted is None:
        if is_empty:
            return current
        if allow_adopt_unstamped:
            return current
        raise EmbeddingIdentityAdoptionRequiredError(
            f"Collection '{collection_name}' has no persisted embedding identity and is "
            "NOT known to be empty. Adopting it would silently certify whatever model wrote "
            f"its existing vectors as the current active provider "
            f"(model_id={current.model_id!r} dimension={current.dimension}) without ever "
            "checking that they agree -- exactly the silent-corruption failure the #311 "
            "model-identity guard exists to prevent (e.g. upgrading and switching embedding "
            "providers in the same deploy). "
            "Set EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS=true to adopt anyway -- only if you "
            "are certain this collection's existing vectors already match the active "
            "provider -- or run the dimension-migration workflow (shadow collection, "
            "backfill, cutover) if they do not. "
            "See docs/reference/configuration.md#embedding-provider-model-identity-guard."
        )
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
