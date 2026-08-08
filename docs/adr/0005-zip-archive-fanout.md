# ADR 0005 — ZIP Archive Expansion: Fan-Out Contract

- **Status:** Accepted
- **Date:** 2026-08-06
- **Deciders:** maintainers
- **Closes:** #130
- **Related:** [ADR 0001](0001-agent-memory-substrate.md), #110, #117

## Context

An agent that holds a bundle (repo export, docs folder) as one ZIP must today
upload every member individually. ZIP support means one uploaded archive
becomes N ingested documents — a fan-out the pipeline was not built for.
Three existing contracts assume one-document-per-upload and must each get an
explicit answer, not an implicit one:

1. **`DocumentUploadMessage`** (`services/inh-contracts/src/inh_contracts/events.py:22-126`,
   contract v1.0.0) carries exactly one `document_id`. Nothing in the schema
   or its consumer (`trigger.py`) expects a message to represent more than
   one document.
2. **Workflow ids are fixed per document.** `TemporalWorkflowTrigger` starts
   every ingestion at `id=f"ingest-{upload_message.document_id}"`
   (`services/inh-ingestion-svc/src/temporal/trigger.py:284`, `:424`). #110
   (`docs/developer/learnings.md:13-166`) spent three rounds fixing what
   happens when a second event collides with that id: `TERMINATE_EXISTING`
   supersedes the running workflow
   (`trigger.py:51`, `:296-298`, `:447-449`), and a database-level fencing
   token (`processed_documents.active_run_id` / `active_run_claimed_at`,
   migrations `016_active_run_fencing.sql` / `017_active_run_claim_ordering.sql`,
   guarded at write time in
   `services/inh-ingestion-svc/src/services/database.py:851-899`) stops the
   terminated run's already-dispatched store activity from clobbering the
   newer run's content, because terminating a Temporal workflow does not
   stop work it already dispatched. Any fan-out design that mints new kinds
   of ids inherits this exact hazard from scratch unless it reuses the
   mechanism #110 already built and proved.
3. **Dedup is content- and filename-keyed, per workspace.** `intake_document`
   (`services/inh-public-api-svc/src/services/document_intake.py:36`) hashes
   the upload and, in its dedup section
   (`document_intake.py:130-198`), looks up `get_document_id_by_content_hash`
   (workspace, content_hash) first, then `get_document_id_by_filename`
   (`services/inh-public-api-svc/src/services/database.py:304-333` and
   `:334-363` respectively), and reuses the existing `document_id` —
   including a same-content short-circuit that skips re-ingestion entirely
   (`document_intake.py:160-190`). A fan-out design must say whether a member
   is a first-class participant in this dedup contract or a second, weaker
   one.

`FILE_TYPE_REGISTRY` (`services/inh-contracts/src/inh_contracts/file_types.py`,
#117) is the fourth contract in scope, and it already anticipates this ADR:
its `extensions` field is documented as reserved for "a future
extension-based consumer (e.g. #130's ZIP member classification)"
(`file_types.py:116-118`), and its `docx` entry's magic-byte comment already
states that a bare 4-byte ZIP signature (`PK\x03\x04`) cannot distinguish a
ZIP from an OOXML sibling and "disambiguating... needs inspecting the
archive's internal `[Content_Types].xml`" (`file_types.py:233-241`) — the
same ambiguity a member classifier must resolve.

## Decision

**Expansion happens once, synchronously, at REST intake — before any MQ
message or Temporal workflow exists.** `POST /v1/archives` (new endpoint,
REST-only per the issue's proposed surface) reads the uploaded ZIP's central
directory, applies the limits in §6, and then calls the *existing*
`intake_document` function once per admitted member — its own source is not
modified. Each member becomes one ordinary `DocumentUploadMessage` and one
ordinary `ingest-{document_id}` workflow. The new code is the loop that
drives those N calls and accounts for what each one does (§5) — not
`intake_document` itself, and not MQ, Temporal, storage, or dedup, which stay
exactly as they are for a standalone upload.

### 1. Fan-out: N ordinary messages, not one message that fans out inside the workflow

Rejected alternative: keep the archive as one `DocumentUploadMessage` and
fan out to N documents inside `DocumentIngestionWorkflow`. This was rejected
because:

- `DocumentIngestionInput` is single-document shaped end to end
  (`services/inh-ingestion-svc/src/temporal/models.py:19-36`) — teaching the
  workflow to write N `processed_documents` rows from one input duplicates,
  inside the workflow, exactly the dedup/validation logic
  `intake_document` already owns on the REST side, and now two call sites
  must agree on it (the surface-friction pattern this repo has been bitten
  by before, e.g. #9, #100).
- The dedup lookups (`get_document_id_by_content_hash`,
  `get_document_id_by_filename`) live in `inh-public-api-svc`'s
  `DatabaseService`, not `inh-ingestion-svc`'s. Fanning out inside the
  workflow means either duplicating those queries into ingestion-svc's
  `DatabaseService` (a second implementation of #75's dedup contract) or
  reaching back into public-api-svc from a Temporal activity — both worse
  than doing the lookup once, where it already lives, before any message is
  published.
- Zip-bomb limits (§6) are cheap to enforce against ZIP central-directory
  metadata without inflating member bytes. Enforcing them matters most
  *before* paying for decompression, S3 upload, and a Temporal workflow
  start per member — i.e. at intake, not after a workflow has already been
  scheduled.

**Consequence of this choice: `DocumentUploadMessage` does not change.**
Contract v1.0.0 stays byte-for-byte as it is; no `parent_document_id` field,
no schema version bump. A member is indistinguishable, on the wire, from a
document uploaded standalone. The only new persistent concept is the
archive's own bookkeeping (§4), which never touches MQ or Temporal.

### 2. Identity and dedup: a member is a full peer of a standalone upload

A member's `document_id` is resolved by calling the *same*
`get_document_id_by_content_hash` → `get_document_id_by_filename` →
`uuid4()` fallback chain `intake_document` already runs
(`document_intake.py:142-198`), keyed on `(workspace_id, content_hash)` /
`(workspace_id, original_filename)` exactly as today. **Two different
archives containing the same file, uploaded into the same workspace, collapse
onto the same document_id** — content-hash dedup does not know or care that
the bytes arrived inside a ZIP. This is not a new policy; it is the existing
#75 contract applied without exception. A member's `original_filename` (the
value stored on `processed_documents`, used for filename dedup and display)
is its full path *within* the archive (e.g. `notes/todo.md`, not `todo.md`),
so two same-named members in different folders of one archive do not collide
against each other under filename dedup, and so provenance stays legible.

Rejected alternative: archive-scoped dedup (a document identity keyed on
`(archive_id, member_path)` instead of workspace content). Rejected because
it would let the same byte-identical file be ingested, chunked, and embedded
once per archive it happens to appear in — the exact duplication #75 exists
to prevent — and because it requires a second dedup index with its own
consistency rules alongside the one that already exists.

**Two members at different paths inside the *same* archive with identical
bytes are not a special case either.** The first member's `intake_document`
call creates a `pending` row and its `document_id`; the second member's
content-hash lookup (`get_document_id_by_content_hash`,
`database.py:334-363`, which has no status filter) finds that row and, since
its status is not `'failed'`, takes the identical-content short-circuit
(`document_intake.py:171-190`) — no second S3 object, no second pending-row
reset, no second MQ publish. This is #75 dedup working exactly as designed,
not a defect: the join table in §4 records **both** archive-member rows
(one per path) against the **same** `document_id`, so the response is
explicit about the collapse (`10 submitted_members`, two of them sharing one
`document_id`) instead of silently presenting 8 rows for a 10-path archive.

### 3. The re-upload / #110 interaction — the sharpest edge, resolved by construction, but only for the members that actually reach it

**What happens when a re-uploaded archive re-expands:** the majority case —
a member whose bytes are byte-for-byte **unchanged** between the two archive
uploads — never reaches #110's machinery at all. Content-hash dedup resolves
the same `document_id` both times, and when the existing document's status
is not `'failed'`, `intake_document`'s identical-content short-circuit
(`document_intake.py:171-190`) returns immediately: **no MQ message is
published, no workflow is started.** Re-uploading an unchanged member is a
pure read (one dedup lookup) with zero write-side effects — #110's
collision-and-supersede path is never exercised because there is nothing to
collide with.

The #110 collision genuinely arises for two narrower cases, both of which
already re-publish and re-enqueue under **standalone** upload today, with no
ZIP involved:

- A member whose bytes **changed** between the two archive uploads. Content-
  hash dedup misses (a new hash), but filename dedup still resolves the same
  `document_id` (`document_intake.py:146-148`) — #60's edited-content-reindex
  behavior — and a fresh `document.uploaded` is published against a
  `document_id` whose prior `ingest-{document_id}` workflow may still be
  open.
- A retry of a member whose prior run left it `status = 'failed'`. The
  identical-content short-circuit explicitly excludes `'failed'` documents
  (`document_intake.py:171-173`), so even byte-identical content re-publishes
  and re-enqueues.

For both, the fresh publish supersedes any still-open prior run exactly as
#110 already guarantees for a standalone re-upload: `TERMINATE_EXISTING`
(`trigger.py:290-299`) plus the `active_run_id` / `active_run_claimed_at`
fencing pair (`database.py:851-899`, `:1006-1109`) stop the terminated run's
late write from clobbering the new one. **No new fencing code is required at
the member level** in either case — a re-expanded archive's changed-or-
retried members hit exactly the collision-and-supersede path
`test_reindex_fencing.py` already covers for a standalone document; they are
not distinguishable from one, because nothing about how they were uploaded
reaches the fencing layer.

**The archive's own bookkeeping (§4) never participates in this race at
all**, because it is never given a Temporal workflow: it is written once at
intake and re-derived (never mutated in place) on every read. A second
upload of "the same" archive gets a brand-new `archive_id` — archive
identity is not deduped in v1 (§4) — so there is no archive-level collision
to fence in the first place. All of the interesting concurrency this ADR has
to answer for happens at the member/`document_id` layer, where #110 already
owns it.

### 4. Archive bookkeeping: a many-to-many join, and a derived-status envelope

A member can belong to more than one archive — the same file, unchanged,
appears in `project-v1.zip` and `project-v2.zip` — and one
`processed_documents` row cannot name more than one parent through a single
column. **The schema is a join table, not a foreign key on the document
row:**

- `archive_uploads`: `archive_id` (uuid, PK), `workspace_id` (indexed),
  `user_id`, `original_filename` (the ZIP's own filename), `skipped_members`
  (JSONB array of `{path, reason}` — never admitted, §5), `failed_at_intake_members`
  (JSONB array of `{path, reason}` — admitted-attempt, infra-caused failure
  before a workflow ever started, §5), `created_at`.
- `archive_members` (new join table): `archive_id` (FK → `archive_uploads`),
  `document_id` (references `processed_documents.document_id`),
  `member_path` (the member's full in-archive path). Primary key
  `(archive_id, member_path)` — a path is unique within one archive's
  listing; `document_id` is **not** unique within an archive (§2's
  intra-archive-duplicate case) and **not** unique across archives (the
  `v1.zip`/`v2.zip` case this section opens with). Indexed on `archive_id`
  (rollup reads) and on `document_id` (reverse lookup: which archives a
  document belongs to).

No `processed_documents.parent_archive_id` column — the join table is the
single source of the archive↔member relation, in both directions, for
however many archives a member happens to belong to.

**`GET /v1/archives/{archive_id}` reads `archive_members WHERE archive_id =
:archive_id` and reports exactly what THAT archive admitted — `v1.zip`'s
rollup is unaffected by anything `v2.zip` later did with the same file,**
because the two are different rows keyed by different `archive_id`s, not
different views over one shared column.

**Archive status is computed on every read from the current `status` of its
admitted members — it is never itself written:**

- The `archive_members` row count for this `archive_id` is `0` (every
  member was skipped or failed at intake, §5) → status `failed`. This guard
  runs first and is checked against the row count, not the live document
  count, so it cannot be affected by later deletions (below) — it answers
  "was anything ever admitted," which is a historical fact, not a current
  one.
- Otherwise, over the *currently existing* member `processed_documents` rows
  (see deletion handling below): any `status = 'pending'` → archive
  `pending` (still in flight; the response still lists each member's current
  status individually, so a caller is never blind to an early failure just
  because the archive as a whole isn't final); no member `pending`, all
  `processed` → `complete`; no member `pending`, all `failed` → `failed`; no
  member `pending`, a mix → `partial`.

**A member deleted after admission** (`DELETE /v1/documents/{id}`,
`services/inh-public-api-svc/src/api/v1/documents.py:137`) has an
`archive_members` row with no matching `processed_documents` row. It is
reported in `submitted_members` as `status: "deleted"` and **excluded** from
the pending/processed/failed tally above — an archive is not held `pending`
forever, or reported `failed`, purely because a caller deliberately deleted
one of its members. If every admitted member has since been deleted, status
reports `complete` (the `archive_members` row count was `> 0`, so the
`failed`-vacuous-truth guard above does not apply; this is a documented
best-effort default, not a provably correct reconstruction of history — a
derived-never-stored status cannot distinguish "all admitted members
finished successfully, then were deleted" from a hypothetical it has no
record of, and this ADR accepts that gap rather than inventing a stored
audit trail to close it).

Rejected alternative: maintain a stored `status` column on `archive_uploads`,
updated by a new consumer of `document.processed` / `document.failed`.
Rejected because **no such consumer exists today** — `DocumentCompletionMessage`
is published for `intg-svc`, an external system, to update its own MongoDB
(`services/inh-contracts/src/inh_contracts/events.py:129-134`); nothing
inside this repo subscribes to it. Building one is a second, independently
fallible write path that must itself be reconciled if it drifts from the
member rows it summarizes — exactly the state/response divergence class
CLAUDE.md's defect-prevention rules exist to keep out. A derived read has no
divergence to have: it is definitionally always consistent with the member
rows it just queried.

**Known cost of "derived, never stored":** there is no archive-level
completion timestamp. `processed_documents.processed_at`
(`services/inh-ingestion-svc/src/services/database.py:202`) exists per
member but is not currently selected by any public-api-svc query — answering
"when did this archive finish" needs `max(processed_at)` over its members,
which `GET /v1/archives/{archive_id}` does not compute in v1. This is a
response-shape addition (aggregate over rows the rollup already fetches,
plus wiring `processed_at` into public-api-svc's `DatabaseService`, which
does not read it today), not a schema change — left as a named follow-up,
not required for v1's status correctness.

### 5. Partial failure and per-member intake errors: a member is admitted, skipped, or failed-at-intake — never silently orphaned

**Nine of ten members extract and ingest; one fails.** Its own workflow run
marks it `failed` and publishes `document.failed`
(`services/inh-ingestion-svc/src/temporal/workflows/document_ingestion.py:496`,
`:534`, `:612`) — the archive's derived status (§4) is `partial`, and
`GET /v1/archives/{archive_id}` names the failed member's `document_id` and
`path` so the caller can retry it individually
(`POST /v1/documents/{id}/refresh`, existing) without re-uploading the
archive. **This part follows the existing single-document failure path
unmodified** — a member failure post-enqueue is not a new failure mode.

**The harder case is failure *inside* the intake loop itself, before a
member ever reaches Temporal.** `intake_document` is called unmodified, but
it is not unconditionally successful: it raises `BadRequestError` for
conditions §8's classifier does not screen (a zero-byte member —
`document_intake.py:108-109` — is ordinary in a docs bundle or repo export,
passes the ratio check trivially, and resolves to a registered extension
with `magic=None`, so §8 admits it; a per-member size over a format's
`max_size_bytes` override — `:113-118`), and `ServiceUnavailableError` when
S3, the database, or MQ is down (`:201-211`, `:218-242`). Calling
`intake_document` per member **without** catching these turns one bad byte
range or one infra blip into a request that 400s or 503s after members
`1..k-1` already have S3 objects, pending rows, and published MQ messages —
contradicting this section's own "admits 9 of 10 and says so" promise, and
leaving those already-admitted documents with no archive record if the
`archive_uploads` row is only written after the loop. **This was the ADR's
own contradiction; it is decided here, not deferred:**

1. **`archive_uploads` is written FIRST**, before any member is touched —
   `archive_id`, `workspace_id`, `user_id`, `original_filename`,
   `created_at`, empty `skipped_members` / `failed_at_intake_members`. Every
   member — admitted, skipped, or failed-at-intake — is therefore attributed
   to an `archive_id` that exists in the response the caller already has, no
   matter where in the loop anything goes wrong.
2. **`archive_members` is written incrementally**, one row per admitted
   member, immediately after `intake_document` returns a `document_id` —
   not batched at the end of the loop. A document that has S3/DB/MQ side
   effects always has a matching `archive_members` row committed in the same
   step; there is no window where a document exists with no archive
   attribution.
3. **The loop catches `intake_document`'s two exception types, per member,
   and classifies — this is where the archive path's new branching actually
   lives** (§1's "not modified" claim is about `intake_document`'s own
   source, not the calling loop):
   - `BadRequestError` → the member is a **content-level** rejection,
     discovered one step later than §8's classifier could catch it. Append
     `{path, reason}` to `archive_uploads.skipped_members` (same bucket and
     semantics as a classifier-level skip — "never admitted, contract-level
     reason") and continue to the next member.
   - `ServiceUnavailableError` → an **infra-level** failure, not specific to
     this member's content, and likely to recur for every remaining member
     too. Append `{path, reason}` to `failed_at_intake_members`, then **stop
     the loop** rather than retrying a dependency that just failed against
     every remaining path — every member not yet attempted is appended to
     `failed_at_intake_members` too, with reason `"archive intake aborted:
     <underlying error>"`, so no path is ever left unaccounted for in all
     three buckets combined.
4. **The endpoint always returns `201`** once `archive_uploads` exists (even
   after an aborted loop), never a `4xx`/`5xx` that would hide the members
   already admitted — the same precedent `intake_document` itself already
   sets for a single upload (an MQ-publish failure after a successful S3
   upload returns `201` with `status="failed"` in the body,
   `document_intake.py:273-309`, "the file IS stored, so this is not a
   request failure"). The archive-level analogue: once any member's side
   effects exist, the only honest response is one that names them, not an
   HTTP error that discards the fact they happened.

**Skipped and failed-at-intake are not "failed" in the archive-status sense
(§4).** Neither ever gets a `document_id`, an `archive_members` row, a
workflow, or a chance to be retried via `/refresh` — they are a different
question ("did this path become a document at all") from the one archive
status answers ("did this path's document finish ingesting"). Both lists are
always present in the response, independent of `status`, mirroring the
skip/fail distinction #117 already draws between "no registry entry"
(contract-level) and "extraction raised" (execution-level) — applied at two
different layers (classification vs. intake) instead of one.

### 6. Limits — zip-bomb guards, checked before decompression, enforced through decompression

All four checked from ZIP central-directory metadata (`ZipInfo.file_size`,
`ZipInfo.compress_size` — read without inflating any member) before any
member's bytes are decompressed, so a hostile archive is rejected for the
cost of an `O(member count)` metadata scan, not the decompression work the
limits exist to prevent:

| Limit | Value | Why |
|---|---|---|
| Archive (compressed) size | 50 MB — the existing `MAX_UPLOAD_SIZE_BYTES` (`services/inh-public-api-svc/src/config/constants.py:75`) | No override; the archive itself is one REST upload and already subject to the global cap. |
| Member count | 500 | Bounds the worst case of one REST request enqueueing 500 Temporal workflow starts + 500 MQ publishes, and bounds the synchronous request latency of §5's intake loop (see "Request latency" below) — comfortably above a real docs-folder/repo-export bundle. |
| Total uncompressed size across all members | 500 MB (10x the compressed cap) | Bounds decompression amplification while allowing a legitimately large bundle; a 10x average ratio is far above what prose/code/PDF content compresses to. |
| Per-member compression ratio | 100:1 (`file_size / compress_size`) | Standard zip-bomb heuristic; a single member exceeding it fails the whole archive before any bytes are inflated, not just that member. |

**The central-directory check alone is necessary but not sufficient — it
must be paired with a bounded reader at decompression time, or it is a
property of one library function, not a guarantee.** A crafted ZIP can
declare a small `file_size` and `compress_size` in both the local file
header and the central directory while actually containing far more
compressed data than declared (the declared numbers are metadata, not a
constraint zlib enforces): the central-directory checks above pass, and a
naive read of "the member's bytes" inflates however much the compressed
stream actually contains. The reason CPython's `zipfile.ZipExtFile` happens
to stop early in the common case — it tracks `_left` against the declared
`file_size` and truncates the read there — is an implementation detail of
one specific reader, not a documented contract; a bounded-total-size
attacker who avoids that reader entirely (`shutil.unpack_archive`, an
`unzip` subprocess, a streaming decoder, a non-seekable `ZipFile`) sees the
declared-metadata checks above pass and gets uninflated decompression to
whatever the stream actually contains. **Decision: member extraction reads
through a bounded wrapper that aborts the member the instant *actual bytes
emitted by the decompressor* exceed that member's own declared `file_size`,
and aborts the whole request the instant the *actual, running* total across
all members exceeds 500 MB — counting bytes the decompressor produced, never
the declared/central-directory numbers.** This is a control on the
inflation step itself, not a trust placed in any particular library's
truncation behavior.

**Request latency.** §5's intake loop performs one S3 PUT, one DB write, and
one MQ publish per admitted member, synchronously, inside the HTTP request —
at 500 members, even a conservative ~50-100ms per member serially is 25-50
seconds inside one request, close to or past common gateway/client timeout
defaults (30-60s). The 500-member cap above is set partly for this reason,
not only for fan-out-storm containment. This ADR does not redesign the
endpoint as async-accept-then-poll (that is a materially bigger decision —
it would change the response contract from "the archive exists with an
initial member list" to "poll before you know what was admitted," and
deserves its own review) — the implementation issue must instead process
members with bounded concurrency (e.g. a small semaphore, not a bare
sequential loop) to keep the common case well under a typical timeout, and
document that a near-cap archive can legitimately take tens of seconds.

### 7. Nesting: depth 0 — a ZIP member that is itself an archive is never expanded

A member whose extension or sniffed bytes resolve to the ZIP family (`.zip`,
or any OOXML sibling once #118/#119 land, since they share `PK\x03\x04`) is
**skipped, not recursed into** — recorded in `skipped_members` with reason
`"nested archive"`. This is a depth limit of zero, not a depth *counter*: no
recursion budget exists to exhaust, so there is nothing a nested-bomb
attacker can tune against. Revisiting this needs a real use case (an agent
that genuinely bundles archives-of-archives) and a scoped follow-up, not a
default-on recursive expander.

### 8. Member classification through FILE_TYPE_REGISTRY

A ZIP member has no HTTP `Content-Type` header — only a path inside the
archive. Classification therefore runs **extension-first**: `get_spec_for_extension`
(`file_types.py:283-293`, already reserved for exactly this per the
docstring cited in Context) resolves the member's extension to a
`FileTypeSpec`, and the member's own bytes are then sniffed against that
spec's `magic` the same way `sniff_content_type` sniffs a declared REST
`Content-Type` — reusing the check, not the entry point (a ZIP member has no
`declared_mime` to pass in; the extension-resolved spec's canonical MIME
type fills that role). A member whose extension is unregistered, or whose
sniff disagrees, is skipped with the specific reason recorded — never
silently dropped, matching the issue's proposal. A `.docx` member is
unambiguous under this scheme even though its magic bytes collide with plain
ZIP (`file_types.py:233-241`), because extension resolves it before magic
bytes are ever consulted — the exact ambiguity that comment flags is not
reachable here.

**Registered extensions today are `.txt .md .markdown .csv .html .htm .pdf
.json .docx .png`** — a repo-export ZIP, the Context section's own opening
example, skips every `.py .js .ts .yaml .toml .go`, so `skipped_members`
would dominate that exact scenario if this shipped today. This is a timing
gap, not a permanent one: sibling issues are adding several of these
extractors concurrently with this ADR, and each new `FILE_TYPE_REGISTRY`
entry (#117's stated design goal) makes every existing and future ZIP
member-classification call site — this one included — pick it up with no
change here. Stated plainly so it is not discovered as a surprise at launch:
v1 archive support is honest about "docs/PDF/notes bundle," not yet "code
repo export," until those extractors land.

The registry itself needs one additive change, left to the follow-up issue:
`FileTypeSpec` gains a way to mark an entry as a *container* (e.g. `zip`
registered with `surfaces=frozenset({"rest"})`, no single-document
`extractor`) so REST-level validation and docs generation see it, while
`test_every_registry_extractor_key_is_wired`
(`services/inh-ingestion-svc/tests/test_temporal_activities.py`) is updated
to exclude container entries — a ZIP archive is never itself dispatched to
an ingestion-svc extractor, because it never becomes a `document_id` of its
own (§1).

## Boundary: what this is not

- **Not a new event contract.** `DocumentUploadMessage` v1.0.0 is unchanged;
  no consumer of that contract needs to change to support ZIP.
- **Not a new fencing mechanism.** Member-level re-ingestion races are the
  existing #110 collision, unmodified, and only reached by changed or
  previously-failed members (§3) — the archive envelope has no fencing
  because it has no workflow to fence.
- **Not recursive.** A ZIP inside a ZIP is a skipped member, not a second
  level of fan-out.
- **Not MCP.** REST-only, per the issue's proposed surface — MCP's
  `upload_document` tool is inline-UTF-8-text-only by construction
  (`file_types.py:66-70`) and cannot transport a ZIP's binary bytes at all.
- **Not a change to single-document upload.** `POST /v1/documents` and
  `intake_document`'s own source are called BY the archive path per member,
  unmodified; nothing about a standalone upload's behavior differs after
  this ADR. What IS new is the calling loop's handling of that function's
  existing exceptions (§5) — a new caller's new branches, not a change to
  the callee.
- **Not an async job.** Intake stays synchronous, request-scoped, with
  bounded member concurrency (§6) — not an accept-then-poll redesign.

## Consequences

- `intake_document`'s own source gains exactly one new caller (the archive
  expansion loop) and is not modified — the dedup, storage, pending-row, and
  MQ-publish logic a member goes through is the same code, same tests, same
  failure modes as a standalone upload. The new branching is in the loop
  that calls it (§5): catching `BadRequestError` as a content-level skip and
  `ServiceUnavailableError` as an infra-level, loop-aborting failure.
- The #110 fencing/supersede mechanism gets its guarantees exercised at
  higher volume (up to 500 concurrent member workflows per archive) but not
  extended — its existing test suite (`test_reindex_fencing.py`) is the
  correctness bar a re-uploaded archive must clear, not a new one. Most
  re-uploaded members never reach it at all (§3): the identical-content
  short-circuit means an unchanged member publishes nothing.
- New schema surface: `archive_uploads` and `archive_members` (two new
  tables; no new column on `processed_documents`), additive migrations,
  filed as follow-up issues below. The join table, not a single foreign key,
  is required because a member can belong to more than one archive (§4).
- New failure-parity obligation: the archive intake path is a new upload
  surface and must be added to
  `services/inh-public-api-svc/tests/contract/test_failure_parity.py`
  (CLAUDE.md dual-surface rule) — though its only surface is REST, so parity
  here means the archive path's own three failure branches (limit-exceeded
  at intake with no `archive_uploads` row yet, a per-member `BadRequestError`
  routed to `skipped_members`, and a per-member `ServiceUnavailableError`
  routed to `failed_at_intake_members` and aborting the loop) each leave the
  state/response pairing §5 specifies — not a REST/MCP comparison.
- Operators get a bounded, auditable answer to "why didn't file X in my ZIP
  show up": every path in the archive resolves to exactly one of admitted
  (`archive_members`), `skipped_members`, or `failed_at_intake_members` —
  never silently absent — and a `partial` archive status makes a
  post-enqueue member failure visible without forcing a re-upload of the
  whole bundle.
- **Known, accepted gaps, not silently deferred:** no archive-level
  completion timestamp without adding `max(processed_at)` to the rollup
  query and wiring `processed_at` into public-api-svc's `DatabaseService`
  (§4); a deleted member's effect on a fully-deleted archive's derived
  status is a best-effort default, not a reconstruction of history (§4);
  registered `FILE_TYPE_REGISTRY` extensions do not yet cover the
  Context section's own code-repo-export motivating example (§8, expected to
  close as sibling extractor issues land); synchronous per-member intake
  latency is bounded by the 500-member cap and bounded concurrency, not
  eliminated (§6).
- **Revisit when:** a real caller needs nested archives, needs MCP-surfaced
  archive upload, member counts/sizes routinely approach the v1 limits, or
  the accepted gaps above start causing real operator confusion — each is a
  scoped follow-up against this ADR's decisions, not a reason to reopen the
  fan-out/dedup/fencing model itself.

## Follow-up issues

- #186 — `archive_uploads` + `archive_members` migration.
- #187 — `FILE_TYPE_REGISTRY` container support (`zip` entry) + member classification helper.
- #188 — `POST /v1/archives`: bounded-concurrency intake, limits (incl. bounded-reader decompression), per-member skip/fail-at-intake classification, member fan-out through `intake_document`.
- #189 — `GET /v1/archives/{archive_id}`: derived status rollup over `archive_members`, including the empty-admitted-set and deleted-member rules.
