"""Workspace-ownership checks shared by inh-ingestion-svc's protected routes.

Every route in ``src/api/app.py`` is gated by ``Depends(verify_api_key)`` --
proof the caller holds the ONE shared ``INGESTION_API_KEY`` service secret,
not proof the caller is entitled to touch a particular workspace's data.
Unlike the public API's per-key workspace entitlement model
(``resolve_workspace_read`` in inh-public-api-svc), ``verify_api_key`` has no
key->workspace binding at all (#177) -- closing that gap is a bigger,
separate design decision (see #177's issue body). Until it lands, every
route that takes a ``document_id`` or dead-letter ``job_id`` and a caller-
supplied ``workspace_id`` MUST verify the two are actually paired in
PostgreSQL before doing anything else with them -- the "match-or-404" shape
#134 established for ``PATCH /chunks/{document_id}/{chunk_index}``. This
module gives every other route (#134's own follow-ups: #175's
``DELETE /documents``, #177's six read/write routes) one place to call that
check from instead of re-deriving it per endpoint.

This is workspace<->row CONSISTENCY, not caller<->workspace ENTITLEMENT --
narrower than it looks at a glance. A caller that already knows a genuine
``(document_id, workspace_id)`` pair for a workspace it doesn't own (e.g. by
reading one out of an unscoped ``GET /dead-letter``) still passes these
checks; #177 closes that specific escalation path by making ``GET
/dead-letter`` require and enforce ``workspace_id`` instead of treating it as
an optional filter, and by applying this same match-or-404 guard to the
single-job dead-letter routes so a job can't be read/mutated by guessing an
id, either.

Both resolve_* helpers below are deliberately thin wrappers with NO
try/except: a PostgreSQL failure during the ownership lookup must propagate
as a 5xx, never be silently swallowed into an "allow" decision. This mirrors
the posture of
``services/inh-public-api-svc/src/services/database.py::user_owns_workspace_in_mongo``,
which raises on failure for the identical reason -- see that method's
docstring, and docs/developer/learnings.md, for why fail-open on an
authorization lookup is unacceptable even during an outage.

POST-#177-REVIEW HARDENING: an adversarial review found that ``GET
/dead-letter?workspace_id=`` (present, EMPTY) returned 200 with no
workspace filter applied at all -- ``Query(...)`` only enforces that the
query param is PRESENT, not that it is non-empty, and
``DatabaseService.get_dead_letter_jobs`` then guarded the WHERE clause with
``if workspace_id:``, which is falsy for ``""``. A "filter" that can be
switched off by passing an empty string is not a filter: this reopened the
exact cross-tenant harvesting the #177 fix was supposed to close. Every
``workspace_id`` this module receives is now run through
``require_workspace_id`` FIRST, which rejects blank/whitespace-only values
at the boundary -- see that function's own docstring for the full story and
why this is a SEPARATE check from "does workspace_id own this row".

#210: ``POST /ingest`` was scoped OUT of #175/#177 on the reasoning that it
is "a creation endpoint with nothing to own yet" -- true of the ROW
(``processed_documents`` doesn't have one until this request creates it),
false of the OBJECT the request names. The pipeline fetches whatever bytes
live at the caller-supplied ``storage_path`` and files the result into the
caller-supplied ``workspace_id`` with no check that the two are related, so
a caller holding the one shared ``INGESTION_API_KEY`` can pair a victim's
``storage_path`` with their OWN ``workspace_id`` and read the victim's
document afterwards through their own legitimate key. There is no row to
resolve this against (see the other two functions below) --
``require_storage_path_workspace_prefix`` checks the one thing that IS
knowable up front: the storage layout is workspace-prefixed, so
``storage_path`` must name the claimed workspace as its own path prefix.
That is necessary, not sufficient -- see that function's docstring for what
it does NOT prove.
"""

from __future__ import annotations

import posixpath
from typing import Any

import structlog
from fastapi import HTTPException

logger = structlog.get_logger(__name__)


def require_workspace_id(workspace_id: str) -> str:
    """Boundary validation for a caller-supplied ``workspace_id``.

    ``Query(..., min_length=1)`` on a route rejects a fully empty string at
    the FastAPI/Pydantic layer, but NOT a whitespace-only one (``" "`` has
    length 1) -- and FastAPI's ``...`` marker only enforces PRESENCE of the
    query param, not non-emptiness, so a bare ``?workspace_id=`` slips past
    ``...`` alone. This was the exact bypass an adversarial review found on
    ``GET /dead-letter``: a falsy ``workspace_id`` reached
    ``DatabaseService.get_dead_letter_jobs``'s ``if workspace_id:`` guard,
    which silently skipped the WHERE clause and returned every workspace's
    dead-letter rows.

    Every route below that takes ``workspace_id`` MUST call this (directly,
    or transitively via ``resolve_owned_document`` /
    ``resolve_owned_dead_letter_job``, which both call it first) BEFORE the
    value reaches any DB call -- a blank value must never even reach a
    query that might treat "no value" as "no filter", the way
    ``get_dead_letter_jobs`` did.

    Args:
        workspace_id: The raw, caller-supplied query param value.

    Returns:
        The stripped, non-blank value.

    Raises:
        HTTPException: 422 if ``workspace_id`` is blank or whitespace-only
            after stripping.
    """
    stripped = workspace_id.strip()
    if not stripped:
        # Denial visibility (post-#177-review finding: no denial was ever
        # logged or counted, so exploitation of the empty-string bypass --
        # or any cross-workspace probe -- was invisible in logs/metrics).
        # Matches inh-public-api-svc's services/auth.py
        # workspace_access_denied posture (reason + what was rejected, no
        # secrets).
        logger.warning(
            "workspace_access_denied",
            reason="blank_workspace_id",
            requested_workspace_id=repr(workspace_id),
        )
        raise HTTPException(status_code=422, detail="workspace_id must not be blank.")
    return stripped


async def resolve_owned_document(
    db_svc: Any, document_id: str, workspace_id: str
) -> dict[str, Any]:
    """Resolve ``document_id`` in PostgreSQL and verify ``workspace_id`` owns it.

    Same response for "no such document" and "exists in a workspace you
    don't own" -- a distinguishable error would leak cross-tenant existence
    of ``document_id``. Used by #175 (``DELETE /documents/{document_id}``),
    #177's ``GET /ingest/{document_id}/status`` and ``GET
    /lineage/{document_id}``, and (already, pre-existing) #134's
    ``PATCH /chunks/{document_id}/{chunk_index}``.

    Args:
        db_svc: A ``DatabaseService`` (or test double) exposing
            ``get_document_status``.
        document_id: The document to resolve.
        workspace_id: The workspace the caller claims owns it. Validated via
            ``require_workspace_id`` before it ever reaches the DB call.

    Returns:
        The full ``processed_documents`` row as a dict.

    Raises:
        HTTPException: 422 if ``workspace_id`` is blank/whitespace-only.
            404 if the document doesn't exist, or exists but its stored
            ``workspace_id`` doesn't match.
        Exception: whatever ``db_svc.get_document_status`` itself raises
            (e.g. a DB outage) -- deliberately NOT caught here. See the
            module docstring.
    """
    workspace_id = require_workspace_id(workspace_id)
    document = await db_svc.get_document_status(document_id)
    if document is None or document.get("workspace_id") != workspace_id:
        # Denial visibility (post-#177-review finding): the HTTP response
        # deliberately stays identical for both cases (no existence leak to
        # the CALLER), but the server-side log is exactly where that
        # distinction belongs -- an operator investigating a cross-tenant
        # probe needs to tell "guessed a document_id that doesn't exist" apart
        # from "guessed a real document_id belonging to another workspace".
        # Mirrors inh-public-api-svc's services/auth.py workspace_access_denied.
        logger.warning(
            "workspace_access_denied",
            reason="document_not_found" if document is None else "document_workspace_mismatch",
            document_id=document_id,
            requested_workspace_id=workspace_id,
            actual_workspace_id=document.get("workspace_id") if document else None,
        )
        raise HTTPException(
            status_code=404,
            detail=f"Document {document_id} not found in workspace {workspace_id}.",
        )
    return document


async def resolve_owned_dead_letter_job(
    db_svc: Any, job_id: int, workspace_id: str
) -> dict[str, Any]:
    """Resolve dead-letter ``job_id`` and verify ``workspace_id`` owns it.

    Same match-or-404 shape as ``resolve_owned_document``, applied to
    ``dead_letter_jobs`` rows (#177: ``GET /dead-letter/{job_id}``, ``POST
    /dead-letter/{job_id}/retry``, ``POST /dead-letter/{job_id}/abandon``).
    ``dead_letter_jobs.workspace_id`` is ``NOT NULL`` (see
    ``DatabaseService._define_tables``), so there is no "row exists but has
    no workspace_id" case to special-case here.

    Args:
        db_svc: A ``DatabaseService`` (or test double) exposing
            ``get_dead_letter_job``.
        job_id: The dead-letter job to resolve.
        workspace_id: The workspace the caller claims owns it. Validated via
            ``require_workspace_id`` before it ever reaches the DB call.

    Returns:
        The full ``dead_letter_jobs`` row as a dict.

    Raises:
        HTTPException: 422 if ``workspace_id`` is blank/whitespace-only.
            404 if the job doesn't exist, or exists but its stored
            ``workspace_id`` doesn't match.
        Exception: whatever ``db_svc.get_dead_letter_job`` itself raises --
            deliberately NOT caught here. See the module docstring.
    """
    workspace_id = require_workspace_id(workspace_id)
    job = await db_svc.get_dead_letter_job(job_id)
    if job is None or job.get("workspace_id") != workspace_id:
        # Denial visibility -- see resolve_owned_document's identical comment.
        logger.warning(
            "workspace_access_denied",
            reason="job_not_found" if job is None else "job_workspace_mismatch",
            job_id=job_id,
            requested_workspace_id=workspace_id,
            actual_workspace_id=job.get("workspace_id") if job else None,
        )
        raise HTTPException(
            status_code=404,
            detail=f"Dead-letter job {job_id} not found in workspace {workspace_id}.",
        )
    return job


def require_storage_path_workspace_prefix(storage_path: str, workspace_id: str) -> str:
    """Verify a caller-supplied ``storage_path`` names an object under the
    claimed ``workspace_id`` before ``POST /ingest`` ever reads it (#210).

    ``POST /ingest`` is the one route in this module with no existing
    ``processed_documents`` row to resolve ownership against -- it CREATES
    that row. That is exactly the reasoning #175/#177 used to scope this
    route out ("a creation endpoint with nothing to own yet"), and exactly
    what makes that reasoning wrong: the endpoint doesn't just create a row,
    it READS an object the caller names. Without this check, a caller
    holding the single shared ``INGESTION_API_KEY`` can submit another
    tenant's ``storage_path`` next to their OWN ``workspace_id``; the
    pipeline fetches those bytes, extracts, chunks, embeds, and files the
    result into the attacker's own Weaviate tenant -- readable afterwards
    through their own legitimate key, via ordinary search.

    The storage layout is workspace-prefixed, so this checks that
    ``storage_path``'s first meaningful path segment IS ``workspace_id``.
    Two prefix conventions are both live in this codebase and both accepted:
    ``workspaces/{workspace_id}/...`` (the historical intg-svc/GCS layout --
    see ``inh_contracts.events.DocumentUploadMessage``'s example, and this
    module's own callers in ``tests/test_api.py``) and ``{workspace_id}/...``
    (``inh-public-api-svc``'s current ``StorageService.generate_key``, which
    drops the ``workspaces/`` literal). ``storage_path`` is normalized with
    ``posixpath.normpath`` FIRST so a ``..`` component can't nominally
    satisfy the prefix check while actually resolving elsewhere (e.g.
    ``workspaces/attacker/../victim/f.pdf`` collapses to
    ``workspaces/victim/f.pdf`` before the prefix is read off it, so it's
    correctly rejected against a claimed ``workspace_id`` of ``attacker``).

    NOT tenant-safety -- say this explicitly wherever this function's result
    is relied on (route docstrings, CHANGELOG, code review), because #210
    was filed precisely because the opposite claim ("scoped out, nothing to
    own") was made about this route once already. ``verify_api_key`` has no
    key->workspace binding (#177): every tenant presents the identical
    secret, so this proves ``storage_path`` is CONSISTENT with the
    ``workspace_id`` the caller CLAIMS, never that the caller is ENTITLED to
    claim that ``workspace_id``. A caller who knows (or guesses) a genuine
    ``workspaces/{victim}/...`` path can still pair it with
    ``workspace_id={victim}`` and pass this check -- only the key->workspace
    binding tracked as the #177 follow-up closes that. Until then this route
    must not be described as tenant-safe.

    Deliberately no try/except: this is a pure, in-process string check with
    no I/O, so there is nothing here that can fail closed OR open -- but it
    is written match-or-deny like ``resolve_owned_document`` /
    ``resolve_owned_dead_letter_job`` above for the same reason: a caller
    that fails this check gets denied, never silently let through because
    something downstream couldn't be reached.

    Args:
        storage_path: The raw, caller-supplied storage path.
        workspace_id: The raw, caller-supplied workspace_id claimed for this
            ingestion. Validated via ``require_workspace_id`` first -- a
            blank/whitespace-only claim is rejected before the path is even
            looked at, same boundary-first posture as the other two
            functions in this module.

    Returns:
        The stripped, validated workspace_id (never the raw caller value --
        matches the pattern every other resolver in this module follows).

    Raises:
        HTTPException: 422 if ``workspace_id`` is blank/whitespace-only.
            403 if ``storage_path``, once normalized, is not prefixed by
            ``workspace_id`` (optionally after a literal ``workspaces/``
            component).
    """
    workspace_id = require_workspace_id(workspace_id)

    # Collapse ".."/"."/redundant slashes BEFORE reading off the prefix, so a
    # nominally-matching first segment can't smuggle a traversal past this
    # check (see docstring example). posixpath.normpath never touches the
    # filesystem -- pure string manipulation, safe to run unconditionally.
    normalized = posixpath.normpath((storage_path or "").strip().lstrip("/"))
    segments = [] if normalized in ("", ".") else normalized.split("/")
    if segments and segments[0] == "workspaces":
        segments = segments[1:]
    prefix = segments[0] if segments else None

    if prefix != workspace_id:
        # Denial visibility -- see resolve_owned_document's identical comment.
        # storage_path is caller-supplied and already known to the caller
        # (it's their own request body), so logging it carries no existence
        # leak the way document_id/job_id lookups above have to avoid.
        logger.warning(
            "workspace_access_denied",
            reason="storage_path_workspace_mismatch",
            requested_workspace_id=workspace_id,
            storage_path=storage_path,
        )
        raise HTTPException(
            status_code=403,
            # Attacker-persona review finding: naming the mismatch alone
            # gives an operator on a third, unsupported layout nothing to
            # act on -- this detail is the only place that gets read at the
            # moment it matters (an auto-generated /docs error toast, a
            # curl response body), not the docstring above. Spell out both
            # accepted forms explicitly; there is no third one.
            detail=(
                f"storage_path does not belong to workspace {workspace_id!r}. "
                f"storage_path must start with 'workspaces/{workspace_id}/...' "
                f"or '{workspace_id}/...'."
            ),
        )
    return workspace_id
