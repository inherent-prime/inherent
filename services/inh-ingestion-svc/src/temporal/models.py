"""Data models for Temporal workflow inputs and outputs.

These dataclasses are used for type-safe communication between
workflow steps and activities. All models are serializable
for Temporal's workflow history.

IMPORTANT: No model should carry large payloads (file bytes, full text,
chunk lists). Large data is staged in PostgreSQL (ingestion_staging table)
and referenced by workflow_run_id. This keeps every gRPC payload < 1KB
and avoids the 4MB Temporal limit.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class DocumentIngestionInput:
    """Input for the document ingestion workflow.

    Maps directly from DocumentUploadMessage Pub/Sub schema.
    """

    document_id: str
    workspace_id: str
    user_id: str
    filename: str
    original_filename: str
    content_type: str
    size_bytes: int
    storage_backend: Literal["local", "s3", "gcs", "azure"]
    storage_path: str
    storage_bucket: str | None = None
    storage_url: str | None = None
    timestamp: str = ""

    # Optional per-document chunking overrides. When None, the workflow
    # resolves each value from application settings (see settings.py:
    # chunking_strategy / max_chunk_size / chunk_overlap). This lets a
    # caller tune chunking per upload without changing global config.
    chunking_strategy: Literal["tokens", "sentences", "paragraphs"] | None = None
    max_chunk_size: int | None = None
    chunk_overlap: int | None = None


@dataclass
class WorkflowResult:
    """Result of the document ingestion workflow."""

    document_id: str
    success: bool
    chunks_created: int = 0
    error: str | None = None
    processing_time_ms: int = 0


@dataclass
class RecordDeadLetterInput:
    """Input for the record_dead_letter activity (#8 dead-letter recording).

    Carries everything needed to write a dead_letter_jobs row and to
    reconstruct the original MQ message so the retry API can re-publish it
    faithfully. ``original_message`` is the full upload-event payload dict.
    """

    document_id: str
    workspace_id: str
    user_id: str
    workflow_run_id: str | None
    original_message: dict
    error_message: str
    error_type: str


@dataclass
class ResolveDeadLetterJobsInput:
    """Input for the resolve_dead_letter_jobs activity (#249).

    Deliberately scoped to ``document_id`` only, not a dead-letter job id --
    a successful ingestion of document X resolves X's outstanding retried
    dead-letter rows regardless of which job/run recorded them, and this
    avoids threading a job id through the re-published upload-event payload
    (see DatabaseService.resolve_dead_letter_jobs_for_document's docstring).
    """

    document_id: str


@dataclass
class PublishCompletionInput:
    """Input for the publish_completion activity (#88).

    Carries the workflow outcome plus everything needed to rebuild the
    DocumentCompletionMessage contract (upload metadata travels through so
    downstream consumers can create/finalize their document records).
    """

    document_id: str
    workspace_id: str
    user_id: str
    filename: str
    original_filename: str
    content_type: str
    size_bytes: int
    storage_backend: str
    storage_path: str
    success: bool
    storage_bucket: str | None = None
    storage_url: str | None = None
    timestamp: str = ""
    chunks_created: int = 0
    error: str | None = None
    processing_time_ms: int = 0


# =============================================================================
# Activity Input/Output Models
# =============================================================================


@dataclass
class EnsureTenantInput:
    """Input for ensure_tenant_ready activity."""

    workspace_id: str
    user_id: str
    workflow_run_id: str | None = None
    document_id: str | None = None


@dataclass
class EnsureTenantOutput:
    """Output from ensure_tenant_ready activity."""

    tenant_id: int | None
    workspace_ready: bool


@dataclass
class FetchDocumentInput:
    """Input for fetch_document activity."""

    document_id: str
    storage_backend: Literal["local", "s3", "gcs", "azure"]
    storage_path: str
    storage_bucket: str | None = None
    storage_url: str | None = None
    workflow_run_id: str | None = None
    workspace_id: str | None = None


@dataclass
class FetchDocumentOutput:
    """Output from fetch_document activity.

    No content bytes — the file stays in storage and is read
    directly by extract_text.
    """

    size_bytes: int


@dataclass
class ExtractTextInput:
    """Input for extract_text activity.

    Instead of receiving raw bytes via gRPC, the activity fetches
    the file directly from storage using these refs.
    """

    workflow_run_id: str
    storage_backend: Literal["local", "s3", "gcs", "azure"]
    storage_path: str
    content_type: str
    original_filename: str
    storage_bucket: str | None = None
    storage_url: str | None = None
    document_id: str | None = None
    workspace_id: str | None = None


@dataclass
class ExtractTextOutput:
    """Output from extract_text activity.

    Text is written to staging, only the length passes through gRPC.
    """

    text_length: int


@dataclass
class ChunkData:
    """Individual chunk data for serialization."""

    document_id: str
    content: str
    chunk_index: int
    start_char: int
    end_char: int
    # Estimated token count for this chunk (see chunk.estimate_tokens).
    # Defaults to 0 for backward compatibility; the chunk activity always
    # populates it with the model-aware estimate.
    token_count: int = 0
    # RAG-poisoning / prompt-injection risk signal (#44). Heuristic, NON-BLOCKING:
    # one of "none" | "low" | "medium" | "high" plus the matched reason codes.
    # Defaults keep older staged chunks valid; the chunk activity always sets them.
    content_risk: str = "none"
    content_risk_reasons: list[str] = field(default_factory=list)
    # Which chunking strategy actually produced this chunk (#129), e.g.
    # "rows" | "sections" | "prose_header" | "sentences" | "paragraphs" |
    # "tokens". Populated by the chunk activity's dispatch, never by the
    # individual _chunk_by_* helpers themselves (single place to keep it
    # consistent with what was actually dispatched -- see _chunk_text_inner).
    # Defaults to "" so a chunk built without going through the activity
    # (unit tests constructing ChunkData directly) stays valid.
    chunking_strategy: str = ""


@dataclass
class ChunkTextInput:
    """Input for chunk_text activity.

    Reads text from staging instead of receiving it via gRPC.
    """

    workflow_run_id: str
    document_id: str
    # Nullable overrides — the chunk_text activity resolves None from settings
    # (config is resolved in the activity, not the workflow, #38).
    strategy: Literal["tokens", "sentences", "paragraphs"] | None = None
    max_chunk_size: int | None = None
    chunk_overlap: int | None = None
    workspace_id: str | None = None
    # The document's declared content type (#129). Used to resolve the
    # registry's chunking_hint (services/inh-contracts/src/inh_contracts/
    # file_types.py) when `strategy` above is not an explicit per-document
    # override. Optional/nullable so an older caller that doesn't pass it
    # (or a content type with no registry entry) degrades to the pre-#129
    # global-config dispatch instead of crashing -- see _chunk_text_inner.
    content_type: str | None = None


@dataclass
class ChunkTextOutput:
    """Output from chunk_text activity.

    Chunks are written to staging, only the count passes through gRPC.
    """

    chunk_count: int = 0


@dataclass
class StoreDocumentInput:
    """Input for store_document activities (PostgreSQL and Weaviate).

    Reads chunks from staging instead of receiving them via gRPC.
    """

    workflow_run_id: str
    document_id: str
    workspace_id: str
    user_id: str
    filename: str
    original_filename: str
    content_type: str
    size_bytes: int
    storage_backend: str
    storage_path: str
    text_length: int
    processing_time_ms: int
    tenant_id: int | None = None
    # --- Conversation ingestion extension (#306) -----------------------------
    # append/document_type/external_id/metadata are additive, defaulted so
    # DocumentIngestionWorkflow (which never sets them) is byte-identical to
    # before this extension existed.
    #
    # append=True switches store_in_postgresql/store_in_weaviate from
    # DESTRUCTIVE full-replace (DELETE all existing chunks, then re-insert --
    # correct for a re-indexed FILE, which really does replace its whole
    # content) to ADDITIVE growth: skip the delete, and increment
    # chunk_count/text_length/size_bytes on the existing row instead of
    # overwriting them. Without this distinction, calling the unmodified
    # store activities on every ~90s conversation flush would silently
    # destroy every previously-flushed turn's chunks from the second flush
    # on -- see ConversationMemoryWorkflow's module docstring.
    append: bool = False
    # document_type distinguishes a conversation's one processed_documents
    # row ('conversation') from an ordinary file ('file', the default --
    # matches the migration 020 column default so an unset value here is
    # indistinguishable from a pre-#306 caller).
    document_type: str = "file"
    # Caller-supplied conversation identifier (POST
    # /v1/conversations/{external_id}/turns) that GET/DELETE resolve back to
    # this row. None for ordinary file documents.
    external_id: str | None = None
    # Optional document-level metadata to persist verbatim into
    # processed_documents.metadata (JSONB) -- used by ConversationMemoryWorkflow
    # to record {"turn_count": ..., "last_flushed_at": ...} for
    # GET /v1/conversations/{external_id}. None (default) leaves the column
    # untouched on both insert and update, so a caller that never passes it
    # (every existing caller) sees no behavior change.
    metadata: dict | None = None


@dataclass
class StoreDocumentOutput:
    """Output from store_document activities.

    superseded (#110): True when this activity's write was skipped because
    active_run_id no longer matched workflow_run_id -- a newer workflow run
    claimed the document in the meantime (TERMINATE_EXISTING supersession,
    see src/services/database.py::store_processed_document). Distinct from a
    plain success=False: this is not an error to retry or dead-letter, it is
    the fencing check working as intended. The workflow that owns this
    activity call has, by definition, already been terminated by the time
    this can happen, so nothing acts on the distinction at the call site
    today -- it exists for observability (logs/metrics) and so tests can
    assert the fenced path was taken rather than a genuine failure.
    """

    success: bool
    chunks_stored: int
    error: str | None = None
    superseded: bool = False


@dataclass
class SetDocumentStatusInput:
    """Input for the set_document_status activity.

    Used to write best-effort 'processing'/'failed' status transitions
    during the workflow. ``status`` is a plain string ("processing",
    "failed", etc.) so it serializes cleanly across Temporal's gRPC.

    workflow_run_id (#110 follow-up): fences this write the same way the
    store activities are fenced (DatabaseService.update_document_status) --
    a terminated (superseded) run's in-flight status write must not be able
    to land after a newer run finished and leave status='processing' with no
    self-heal. Optional so a caller without a run context (none exist today,
    but keeps the DB method's signature backward compatible) still works.
    """

    document_id: str
    workspace_id: str
    status: str
    error_message: str | None = None
    workflow_run_id: str | None = None


@dataclass
class UpdateStatsInput:
    """Input for update_workspace_stats activity.

    workflow_run_id is included for future idempotency (ledger-based
    dedup to prevent double-counting on Temporal retries).
    """

    workspace_id: str
    document_delta: int
    chunk_delta: int
    size_delta: int
    workflow_run_id: str | None = None
    document_id: str | None = None


@dataclass
class CreatePendingDocumentInput:
    """Input for the create_pending_document activity (#10, #110).

    Creates a minimal 'processing' processed_documents row at workflow start so
    a failure during fetch/extract/chunk is observable via the status API
    instead of returning 'not found'. The store step later upserts the full row.

    workflow_run_id (#110): also claims the document's fencing token (see
    DatabaseService.create_pending_document / migration 016) so a later
    store commit from a DIFFERENT, superseded run for the same document_id
    can detect it's been superseded and skip its write instead of clobbering
    this run's content.

    workflow_start_time (#110 follow-up, migration 017): this run's Temporal
    start time (workflow.info().start_time -- deterministic, safe inside
    @workflow.run), used to make the claim monotonic in START order rather
    than commit order. See DatabaseService.create_pending_document's
    docstring for the failure this closes.
    """

    document_id: str
    workspace_id: str
    user_id: str
    filename: str
    original_filename: str
    content_type: str
    size_bytes: int
    storage_backend: str
    storage_path: str
    workflow_run_id: str
    workflow_start_time: datetime
    storage_bucket: str | None = None
    storage_url: str | None = None


# =============================================================================
# Staging Cleanup Models
# =============================================================================


@dataclass
class CleanupStagingInput:
    """Input for cleanup_staging activity."""

    workflow_run_id: str


# =============================================================================
# Chunk Edit Models
# =============================================================================


@dataclass
class ChunkEditInput:
    """Input for the chunk edit workflow."""

    document_id: str
    chunk_index: int
    content: str
    workspace_id: str = ""
    user_id: str = ""


@dataclass
class ChunkEditResult:
    """Result of the chunk edit workflow."""

    document_id: str
    chunk_index: int
    success: bool
    error: str | None = None


# Single source of truth for the record_chunk_edit_weaviate_failure retry
# budget, shared by the workflow (which sets this as the activity's
# RetryPolicy.maximum_attempts) and the activity itself (which checks
# activity.info().attempt against it to know whether THIS attempt is the
# last one, so it logs CRITICAL + bumps a counter only once, on true
# exhaustion, rather than on every retried attempt). Keeping this in one
# place avoids the two call sites silently drifting out of sync.
CHUNK_EDIT_COMPENSATION_MAX_ATTEMPTS = 2


# =============================================================================
# Redaction Models (#307)
# =============================================================================


@dataclass
class RedactTurnInput:
    """One conversation turn to redact, as passed into `redact_turns`.

    `role`/`ts`/`client` are optional pass-through metadata (matching #306's
    turn shape) -- `redact_turns` never inspects them, only `text` and
    `turn_id`; they ride along so the caller doesn't need a second lookup to
    reassemble a `RedactedTurn` back into a full turn record.
    """

    turn_id: str
    text: str
    role: str | None = None


@dataclass
class RedactedTurn:
    """One turn AFTER redaction -- the only form downstream should ever read.

    See the `redact_turns` module docstring (src/temporal/activities/
    redact.py): a downstream activity that re-derives turn text from
    anywhere other than this output (e.g. a workflow's raw pre-redaction
    buffer) reopens exactly the leak #307 exists to close.
    """

    turn_id: str
    text: str
    role: str | None = None
    # Per-turn counts by redaction_type (e.g. {"api_key": 1, "jwt": 2}),
    # empty when nothing in this turn matched any detector.
    redaction_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class RedactTurnsInput:
    """Input for the `redact_turns` activity (#307).

    Carries the batch of turns to redact plus enough context
    (workflow_run_id/workspace_id/document_id) to attribute an audit record
    if a turn's redaction fails -- see RedactTurnsOutput.dropped_turn_ids.
    """

    turns: list[RedactTurnInput]
    workflow_run_id: str | None = None
    workspace_id: str | None = None
    document_id: str | None = None


@dataclass
class RedactTurnsOutput:
    """Output of the `redact_turns` activity (#307).

    `redacted_turns` holds only turns that redacted successfully.
    `dropped_turn_ids` names every turn dropped because its own redaction
    pass raised (per-turn granularity -- one bad turn never fails the whole
    batch, see the module docstring in redact.py). `redaction_counts` is the
    BATCH-level sum of every turn's `RedactedTurn.redaction_counts`, for
    metric emission (#307: "Emit a metric for redactions by type").
    """

    redacted_turns: list[RedactedTurn] = field(default_factory=list)
    dropped_turn_ids: list[str] = field(default_factory=list)
    redaction_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class ChunkEditWeaviateFailureInput:
    """Input for the record_chunk_edit_weaviate_failure activity (#137).

    Carries what's needed to write a durable, queryable ingestion_events row
    (GET /lineage/{document_id}) when a chunk edit's PostgreSQL write
    succeeds but its Weaviate re-embed does not, even after retries -- the
    compensating "mark-failed" signal for that divergence, so it isn't only
    visible as a one-shot HTTP 5xx the caller may not persist.
    """

    workflow_id: str
    document_id: str
    workspace_id: str
    chunk_index: int
    error_message: str


# =============================================================================
# Conversation Memory Models (#306)
# =============================================================================
#
# Deliberately plain @dataclass, matching every other model in this file --
# NOT the Pydantic ConversationTurnMessage in inh_contracts.events, which is
# the MQ wire contract. The two layers stay separate on purpose (see
# ConversationMemoryWorkflow's module docstring): a Temporal workflow input
# must be a hand-mapped dataclass so it participates correctly in Temporal's
# own (dataclass-based) data converter and workflow-history replay, the same
# reason DocumentIngestionInput above is a dataclass and not
# DocumentUploadMessage itself.


@dataclass
class ConversationTurnSignal:
    """One turn delivered to `ConversationMemoryWorkflow.add_turn` (#306).

    Mirrors `ConversationTurnMessage` (inh_contracts.events) field-for-field
    (minus `event_type`/`contract_version`, which are MQ-envelope concerns
    the workflow signal doesn't need) -- `conversation_trigger.py` maps one
    directly onto the other.
    """

    turn_id: str
    role: str
    text: str
    ts: str
    user_id: str
    client: str | None = None


@dataclass
class ConversationMemoryInput:
    """Input for `ConversationMemoryWorkflow.run` -- both the FIRST start
    (via `signal_with_start`) and every `continue_as_new` re-start (#306).

    `tenant_id`/`document_created`/`seen_turn_ids`/`last_activity_iso` are
    the carried-forward state a `continue_as_new` needs to stay behaviorally
    continuous with the run it replaces (no need to re-resolve the tenant, no
    duplicate `document_delta=1` on the next flush, no forgetting the most
    recently seen turn_ids the moment a fresh run starts). Every one of them
    defaults to its "fresh conversation" value so the FIRST start (which
    supplies none of them) needs no special-casing at the call site.
    """

    workspace_id: str
    external_id: str
    user_id: str
    tenant_id: int | None = None
    document_created: bool = False
    # Bounded, most-recently-seen turn_ids carried across continue_as_new so
    # a duplicate delivered just before/after the boundary is still caught.
    # See ConversationMemoryWorkflow._SEEN_TURN_IDS_BOUND for the cap.
    seen_turn_ids: list[str] = field(default_factory=list)
    # Debounce/lifecycle configuration (#306). Resolved from settings by
    # `conversation_trigger.py` -- a normal Python caller, OUTSIDE the
    # workflow sandbox -- and passed in here rather than the workflow
    # calling `get_settings()` itself inside `@workflow.run`, which is a
    # Temporal determinism anti-pattern (see chunk.py's #38 comment: a
    # config value that could differ between the original run and a later
    # REPLAY of the same history must never be read directly inside
    # workflow code). On `continue_as_new`, the workflow forwards its OWN
    # already-resolved values here rather than re-reading settings, so a
    # mid-life config change cannot retroactively change a long-lived
    # conversation's behavior out from under a replay.
    flush_char_threshold: int = 4000
    flush_idle_seconds: int = 90
    continue_as_new_turns: int = 500
    idle_finalize_seconds: int = 24 * 3600


@dataclass
class ConversationTurnMeta:
    """Non-sensitive, non-text turn metadata carried alongside a redacted
    turn into `chunk_conversation` (#306).

    `RedactedTurn` (redact.py's output) deliberately carries only
    `turn_id`/`text`/`role`/`redaction_counts` -- no `ts`/`client` -- so this
    is NOT a substitute text channel (it never carries turn text at all);
    `ConversationMemoryWorkflow` zips this (sourced from its own pre-
    redaction buffer, safe because it is metadata, never the turn's TEXT)
    back onto `RedactedTurn` by `turn_id` before calling `chunk_conversation`.
    See `chunk_conversation`'s module docstring for why `text` must still
    come from `RedactedTurn` alone.
    """

    turn_id: str
    ts: str
    client: str | None = None


@dataclass
class ChunkConversationInput:
    """Input for the `chunk_conversation` activity (#306).

    `redacted_turns` is the ONLY source of turn TEXT this activity ever
    reads -- see the module docstring in conversation_chunk.py for why that
    is a security property, not a style preference. `turn_meta` supplies the
    non-text `ts`/`client` fields `RedactedTurn` doesn't carry.
    """

    workflow_run_id: str
    document_id: str
    workspace_id: str | None = None
    redacted_turns: list[RedactedTurn] = field(default_factory=list)
    turn_meta: list[ConversationTurnMeta] = field(default_factory=list)
    max_chunk_size: int | None = None
    chunk_overlap: int | None = None


@dataclass
class ChunkConversationOutput:
    """Output of the `chunk_conversation` activity (#306).

    Chunks themselves are written to staging (same `ingestion_staging`
    'chunks' key `chunk_text` uses, see staging.py) so
    `store_in_postgresql`/`store_in_weaviate` read them completely
    unmodified -- only the count crosses the gRPC boundary.
    """

    chunk_count: int = 0
