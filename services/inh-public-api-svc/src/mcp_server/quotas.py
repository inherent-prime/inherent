"""Per-identity entitlement and quota enforcement (#309), now shared by the
MCP dispatcher AND REST v1 writes (#365).

Sits between the caller's permission/scope check and the actual write --
``http_transport.py``'s ``call_tool``/``_call_tool_oauth`` for MCP,
``src/api/v1/documents.py``'s ``upload_document`` and
``src/api/v1/conversations.py``'s ``append_conversation_turns`` for REST.
By the time ``check_quota`` runs, the caller is already authenticated and
holds the required permission/scope -- this module answers ONE further
question, "has this identity got budget left for this call", keyed off
``Principal`` (``principal_type`` + ``principal_id``, #295) rather than a
second identity notion of its own, per the issue's design.

Surface scope (#365): #309 shipped this enforced on MCP only -- every
docstring here said "MCP dispatcher" specifically, and that was SCOPE, not a
bug (``Principal`` is a seam for identity-source-agnosticism, not
surface-agnosticism). #365 widens it: the same ``check_quota`` call, the same
``Principal``, the same fail-open/fail-closed rules now also guard REST
writes, because a principal with real limits configured must not be able to
bypass them just by calling the REST route instead of the MCP tool. This
changes NOTHING for the shipped default (``NullEntitlementsProvider`` ==
unlimited, see below) -- it only starts mattering the day an operator wires
in a real ``EntitlementsProvider``, which is exactly when an unenforced REST
surface would become a silent bypass. ``quota_denial_to_rate_limit_error``
below is REST's side of rendering a denial, the twin of
``http_transport._quota_exceeded_result`` for MCP.

Reuses ``src/core/rate_limiter.py``'s ``TokenBucketRateLimiter`` for every
time-windowed limit (``calls_per_minute`` / ``calls_per_month`` /
``writes_per_day``) instead of building a second limiter -- same
Redis-when-configured/in-memory-otherwise backend #213 already ships and
already sits in this app's request path for REST and (via
``RateLimitingMiddleware``, connection-wide) for ``/mcp`` itself. This module
adds per-identity, per-limit-kind buckets on top, keyed by
``entitlement:<principal_type>:<principal_id>:<limit_name>`` so they never
collide with #213's own ``key:<key_id>`` / ``ip:<ip>`` buckets.

Fail-open vs fail-closed (#309 design constraint #2)
-----------------------------------------------------
Two different failures look similar but must be handled oppositely:

1. **The principal genuinely has no budget left** (the rate-limiter backend
   or the document-count query answered normally, and the answer is "over
   limit"). This is enforcement working as intended -- FAIL CLOSED, reject
   the call with a structured, actionable error (see ``QuotaDenial`` /
   ``http_transport._quota_exceeded_result``).
2. **The metering/entitlements infrastructure itself is unreachable**
   (entitlements provider raised, the rate-limiter's Redis backend raised, the
   document-count query raised). This is NOT the same thing as "over limit" --
   treating an outage as "everyone is instantly out of quota" would let a sink
   blip take down the entire MCP surface, worse than the abuse this feature
   prevents. FAIL OPEN: allow the call, and log loudly (``logger.error`` with
   ``exc_info=True``, not a swallowed ``debug``) so the outage is visible to
   whoever operates the deployment. Every ``except Exception`` block below
   returns ``None`` (== "not denied") after logging, never re-raises and never
   synthesizes a denial.

Metering is not on the critical path, with one unavoidable exception
-----------------------------------------------------------------------
``publish_usage_event`` (bottom of this module) is fire-and-forget --
scheduled via ``asyncio.create_task`` and never awaited by the dispatcher, so
a slow or failing metering sink cannot add latency to, or fail, a tool call
(issue's "Metering" section + acceptance criterion "Disabling the metering
sink does not affect tool-call success or latency").

The *enforcement* counters above are a deliberate, narrow exception to "no
I/O in the critical path": ``calls_per_minute`` cannot be enforced without
first learning the CURRENT count, which means a synchronous round trip to
the counter's backend is unavoidable -- there is no way to decide "allowed"
without it. This is the same tradeoff #213's ``RateLimitingMiddleware``
already makes for every request on this app (a synchronous
``check_rate_limit`` call before ``call_next``), on the exact same backend
(Redis ``INCR``, or an in-process lock for the in-memory fallback) -- both
sub-millisecond operations already inside this service's established latency
budget, not a new one. It only runs at all when a principal's entitlements
are non-``None`` (see ``Entitlements.unlimited`` / the early return below) --
an unlimited caller (the default, #309 design constraint #1) touches neither
the rate limiter nor the database.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.core.exceptions import RateLimitError
from src.core.rate_limiter import TokenBucketRateLimiter, get_rate_limiter
from src.services.auth import Principal
from src.services.entitlements import Entitlements, get_entitlements_provider
from src.utils import get_logger

logger = get_logger(__name__)

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_DAY = 24 * 60 * 60
# A calendar month has no fixed length; a rolling 30-day window is the same
# approximation the issue's own "total tool calls" framing implies (there is
# no monthly-billing-cycle concept anywhere else in this repo to anchor to
# instead). Module-level so a deployment or a test can override it.
_SECONDS_PER_MONTH = 30 * _SECONDS_PER_DAY

# Tools that can INCREASE a workspace's document count. ``max_documents`` is
# checked only against these -- not against every ``permission="write"``
# tool the way ``writes_per_day`` is. ``delete_document`` and
# ``refresh_stale_source`` both carry ``permission="write"`` too, but
# blocking them at the cap would be self-defeating: a caller already at
# ``max_documents`` who is denied ``delete_document`` has no way to ever get
# back under the cap. Deliberately a set here, not a new ``ToolDef`` field --
# this is dispatcher-level policy about what "the document cap" protects
# against, not a fact about the tool itself worth carrying in the shared
# registry (server.py's ``_TOOLS`` is NOT touched by #309; see the issue's
# design constraint #5).
_DOCUMENT_INCREASING_TOOLS = frozenset({"upload_document"})

WRITE_PERMISSION = "write"


@dataclass(frozen=True)
class QuotaDenial:
    """A rejected call's details -- everything ``http_transport.py`` needs to
    build an actionable rejection (#309 design constraint #3: "The caller
    learns which limit it hit and when it resets — not a bare 'denied'").

    ``reset_at`` is a Unix timestamp for the three time-windowed limits, and
    ``None`` for ``max_documents`` -- that limit has no time window; it clears
    when the caller deletes documents or an operator raises the cap, not on a
    schedule, so there is no honest "resets at" instant to report.
    """

    limit_name: str
    limit: int
    reset_at: float | None
    upgrade_url: str | None


async def _check_counter(
    limiter: TokenBucketRateLimiter,
    *,
    key: str,
    limit_name: str,
    limit: int,
    window_seconds: int,
    upgrade_url: str | None,
) -> QuotaDenial | None:
    """Check-and-consume one time-windowed limit via the shared rate limiter.

    Fails OPEN (returns ``None``, logs loudly) on any backend error --
    see the module docstring's fail-open/fail-closed section.
    """
    try:
        result = await limiter.check_rate_limit(key=key, limit=limit, window_seconds=window_seconds)
    except Exception:
        logger.error(
            "quota_backend_unavailable",
            limit_name=limit_name,
            bucket_key=key,
            exc_info=True,
        )
        return None
    if result.allowed:
        return None
    logger.warning(
        "quota_exceeded",
        limit_name=limit_name,
        bucket_key=key,
        limit=limit,
    )
    return QuotaDenial(
        limit_name=limit_name,
        limit=limit,
        reset_at=result.info.reset_at,
        upgrade_url=upgrade_url,
    )


async def _check_max_documents(
    principal: Principal,
    entitlements: Entitlements,
    workspace_ids_provider: Callable[[], Awaitable[list[str]]] | None,
) -> QuotaDenial | None:
    """Check the ``max_documents`` cap. Requires knowing which workspaces
    count toward ``principal``'s quota -- ``workspace_ids_provider`` is a
    zero-arg async callable supplied by the caller (``http_transport.py``)
    so this stays lazy: it is only ever invoked when a ``max_documents``
    limit is actually configured, keeping the common (unlimited, or
    limited-but-not-on-documents) path free of the extra DB round trip.

    No provider (e.g. the OAuth path, which has no workspace resolution to
    offer yet -- see ``http_transport._call_tool_oauth``'s docstring) is
    treated as an infrastructure gap, not a quota breach: fails OPEN with a
    loud log, same as a backend error below.

    Known approximation -- check-then-act, not atomic (#366)
    -----------------------------------------------------------
    The ``SELECT COUNT(*)`` below and the write that actually creates the new
    document row happen in TWO SEPARATE, UNSYNCHRONIZED steps: this function
    reads the count in its own short-lived session and returns; the caller
    (``upload_document``'s handler/route) does the S3 upload and the
    persistence insert LATER, with no lock held across the gap. Two
    concurrent callers who both read the count while it sits at
    ``max_documents - 1`` will BOTH see "under the cap", BOTH proceed, and
    BOTH insert -- the classic TOCTOU race. This is deliberately NOT fixed
    here:

    - The overshoot is BOUNDED, not unbounded: at most N documents over the
      cap, where N is the number of requests racing at that exact boundary
      at that exact moment -- not "the cap stops meaning anything". For a
      soft usage cap (this is explicitly not a security boundary -- nothing
      behind ``max_documents`` protects data confidentiality or integrity,
      only "how many documents can this identity accumulate"), a bounded,
      rare overshoot is an acceptable approximation, the same tradeoff class
      as the fail-open behavior above.
    - A real fix means moving enforcement from "check before dispatch" to
      "enforce at the write itself" -- inside ``document_intake.py``'s
      persistence step, which would need a new write-time error path threaded
      back through four modules (intake -> both callers -> both surfaces'
      error rendering). That is a real design change to WHERE enforcement
      lives, not a bug fix to this function, and is explicitly out of scope
      here.
    - Holding a lock across the gap instead is not an option: the S3 upload
      happens BETWEEN this check and the eventual insert (see
      ``document_intake.intake_document``), and holding a DB lock across an
      external network call to S3 is the kind of thing that turns a slow S3
      request into a stuck transaction/connection-pool exhaustion incident --
      strictly worse than the race it would close.

    Reachability today: same as every other check in this module, this is
    completely unreachable in the shipped default (``NullEntitlementsProvider``
    means ``entitlements.unlimited`` is always true, so ``check_quota`` never
    even calls this function) -- see ``TestMaxDocumentsRace`` in
    ``tests/unit/test_quotas.py`` for a test that pins the overshoot itself
    (two concurrent callers at the boundary both passing), not just this
    comment describing it.
    """
    if workspace_ids_provider is None:
        logger.warning(
            "quota_max_documents_skipped_no_workspace_context",
            principal_type=principal.principal_type,
            principal_id=principal.principal_id,
        )
        return None
    try:
        workspace_ids = await workspace_ids_provider()
        if not workspace_ids:
            # Nothing this principal can write to -- the write itself will be
            # rejected downstream (or there's nothing to count); not this
            # check's job to guess at either.
            return None
        from src.services.database import get_database

        database = await get_database()
        current = await database.get_document_count_for_workspaces(workspace_ids)
    except Exception:
        logger.error(
            "quota_backend_unavailable",
            limit_name="max_documents",
            principal_type=principal.principal_type,
            principal_id=principal.principal_id,
            exc_info=True,
        )
        return None
    assert entitlements.max_documents is not None  # caller only reaches here when set
    if current < entitlements.max_documents:
        return None
    logger.warning(
        "quota_exceeded",
        limit_name="max_documents",
        principal_type=principal.principal_type,
        principal_id=principal.principal_id,
        current=current,
        limit=entitlements.max_documents,
    )
    return QuotaDenial(
        limit_name="max_documents",
        limit=entitlements.max_documents,
        reset_at=None,
        upgrade_url=entitlements.upgrade_url,
    )


async def check_quota(
    principal: Principal,
    tool_name: str,
    tool_permission: str,
    *,
    workspace_ids_for_max_documents: Callable[[], Awaitable[list[str]]] | None = None,
) -> QuotaDenial | None:
    """Return a ``QuotaDenial`` if ``principal`` is over budget for a call to
    ``tool_name`` (whose registry permission is ``tool_permission``); ``None``
    if the call may proceed.

    Checked in order calls_per_minute -> calls_per_month -> writes_per_day ->
    max_documents. Each ``_check_counter`` call both checks AND consumes (the
    token-bucket contract, see ``rate_limiter.py``) -- so a call that fails a
    LATER check in this sequence has already consumed a token from an
    EARLIER, passed check (e.g. a call that passes ``calls_per_minute`` but
    then trips ``writes_per_day`` has still spent one of its per-minute
    calls). This mirrors how #213's own middleware works (consumption happens
    at check time, before the handler's outcome is known) and is the correct
    tradeoff here too: the call really was attempted, so it really should
    count -- inventing a "refund the token" step for a downstream rejection
    would let a caller probe every limit for free by design.
    """
    try:
        entitlements = await get_entitlements_provider().get_entitlements(principal)
    except Exception:
        # Entitlements lookup itself is infrastructure -- an outage there
        # must not be indistinguishable from "this principal has zero
        # budget". Fail OPEN (#309 design constraint #2).
        logger.error(
            "entitlements_lookup_failed",
            principal_type=principal.principal_type,
            principal_id=principal.principal_id,
            exc_info=True,
        )
        return None

    if entitlements.unlimited:
        # The default-open path (#309 design constraint #1): zero I/O below
        # this line for a principal with no entitlement record configured.
        return None

    limiter = get_rate_limiter()
    bucket_prefix = f"entitlement:{principal.principal_type}:{principal.principal_id}"
    is_write = tool_permission == WRITE_PERMISSION

    if entitlements.calls_per_minute is not None:
        denial = await _check_counter(
            limiter,
            key=f"{bucket_prefix}:calls_per_minute",
            limit_name="calls_per_minute",
            limit=entitlements.calls_per_minute,
            window_seconds=_SECONDS_PER_MINUTE,
            upgrade_url=entitlements.upgrade_url,
        )
        if denial is not None:
            return denial

    if entitlements.calls_per_month is not None:
        denial = await _check_counter(
            limiter,
            key=f"{bucket_prefix}:calls_per_month",
            limit_name="calls_per_month",
            limit=entitlements.calls_per_month,
            window_seconds=_SECONDS_PER_MONTH,
            upgrade_url=entitlements.upgrade_url,
        )
        if denial is not None:
            return denial

    if is_write and entitlements.writes_per_day is not None:
        denial = await _check_counter(
            limiter,
            key=f"{bucket_prefix}:writes_per_day",
            limit_name="writes_per_day",
            limit=entitlements.writes_per_day,
            window_seconds=_SECONDS_PER_DAY,
            upgrade_url=entitlements.upgrade_url,
        )
        if denial is not None:
            return denial

    if tool_name in _DOCUMENT_INCREASING_TOOLS and entitlements.max_documents is not None:
        denial = await _check_max_documents(
            principal, entitlements, workspace_ids_for_max_documents
        )
        if denial is not None:
            return denial

    return None


async def _emit_usage_event(principal: Principal, tool_name: str, *, allowed: bool) -> None:
    """The actual metering "publish" -- today, a structured log line; the
    seam a real deployment plugs a durable sink (billing pipeline, usage
    warehouse, ...) into. Wrapped in its own try/except so a broken sink
    can never surface as a task exception (which ``asyncio`` would otherwise
    log as "Task exception was never retrieved" but which must never be
    allowed to become an unhandled error anywhere near the request path)."""
    try:
        logger.info(
            "mcp_tool_usage",
            principal_type=principal.principal_type,
            principal_id=principal.principal_id,
            tool=tool_name,
            allowed=allowed,
            ts=time.time(),
        )
    except Exception:  # pragma: no cover - logging itself must never raise
        logger.error("usage_event_publish_failed", exc_info=True)


# Strong references to in-flight fire-and-forget tasks. asyncio's own docs
# warn that a Task with no reference held elsewhere is eligible for garbage
# collection mid-execution ("Save a reference to the result of this
# function"); publish_usage_event's whole point is that its caller holds NO
# reference (it isn't awaited), so this module-level set holds one instead --
# each task removes itself via its done-callback once it finishes, so this
# never grows unbounded.
_background_tasks: set[asyncio.Task] = set()


def publish_usage_event(principal: Principal, tool_name: str, *, allowed: bool) -> None:
    """Fire-and-forget usage metering (#309 "Metering" section: "A metering
    failure must never fail or delay a tool call").

    Deliberately NOT awaited by any caller -- ``asyncio.create_task``
    schedules ``_emit_usage_event`` on the running loop and returns
    immediately, so even a sink that is slow (a real future implementation
    might do network I/O here) adds zero latency to the tool call's own
    response, and ``_emit_usage_event``'s own try/except means a failing
    sink never produces an unhandled exception either. See
    ``tests/unit/test_quotas.py::TestPublishUsageEvent`` for the pinned
    "does not block, does not raise" behavior.
    """
    task = asyncio.create_task(_emit_usage_event(principal, tool_name, allowed=allowed))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
