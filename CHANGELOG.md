# Changelog

All notable changes to Inherent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`workspace_ownership_lookup_degraded_total` metric for
  inh-public-api-svc's workspace-ownership lookups (#184).**
  `DatabaseService.get_user_workspace_ids`'s Mongo and Postgres-fallback
  sub-lookups, and `user_owns_workspace_in_mongo`'s raise-on-failure check,
  now bump this counter (labeled `source`: `mongo` / `postgres_fallback` /
  `mongo_ownership_check`) alongside their existing log line, mirroring the
  `AUDIT_MESSAGES_DROPPED_TOTAL` precedent (inh-ingestion-svc, #18). Before
  this, a degraded lookup was warning-log-only and could run degraded
  indefinitely with nothing to alert on. Metric emission is
  observability-only and never changes the swallow-vs-raise behavior of
  either call path.

- **XLSX and PPTX upload/extraction support (#118, #119).** Both are now
  `FILE_TYPE_REGISTRY` entries (#117): XLSX
  (`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
  `.xlsx`, REST-only) extracts row-aware, sheet-boundary-preserving text
  (`## Sheet: <name>` headers, pipe-delimited rows, computed formula values
  via `openpyxl` `data_only=True`, merged cells carrying a `[merged A1:D1]`
  marker, the sheet name + header row periodically re-emitted so a
  downstream chunker splitting mid-sheet still has context). PPTX
  (`application/vnd.openxmlformats-officedocument.presentationml.presentation`,
  `.pptx`, REST-only) extracts slide-boundary text (`## Slide <n>: <title>`
  headers, in-order text frames, pipe-delimited table rows, speaker notes
  under `Notes:`) via `python-pptx`. Both are core (non-optional)
  dependencies of `inh-ingestion-svc` (`openpyxl`, `python-pptx`),
  `hard_fail` degradation. Legacy `.xls`/`.ppt` (a different, OLE2-based
  binary format) have no registry entry and continue to 400 with the
  standard unsupported-type message rather than being silently mis-parsed.
  Cost guards (evaluated-cell/slide count, per-value length, total emitted
  text length) are enforced incrementally while streaming rows/slides, not
  after materializing the full output, so a pathological file fails fast
  with bounded memory instead of risking OOM; every extraction failure
  (corrupt/truncated file, password-protected file, a cap breach, a
  mismatched OOXML sibling reaching DOCX's extractor) is deterministic given
  the uploaded bytes and raises a non-retryable error, so Temporal fails the
  document after one attempt instead of burning its retry budget on an
  outcome already known at attempt 1. XLSX, PPTX, and the existing DOCX
  format all share the same ZIP local-file-header magic bytes (`PK\x03\x04`)
  — the intake-time byte sniff cannot distinguish them from each other by
  design. A mismatched filename extension is caught at upload
  (`ExtensionMismatchError`, 400); an extensionless upload, OR one renamed to
  match the false declared type (e.g. real XLSX bytes as `report.docx`),
  reaches extraction instead, where the wrong OOXML part set fails to parse
  with a clear, filename-bearing, non-retryable error — never silently
  mis-read as another format.
- **Structured text ingestion: YAML, TOML, XML (#121).** Registered in
  `FILE_TYPE_REGISTRY` (`application/yaml`/`text/yaml`, `application/toml`,
  `application/xml`/`text/xml`; `.yaml`/`.yml`/`.toml`/`.xml`), `rest+mcp`,
  `chunking_hint="structured"`. YAML and TOML are ingested as decoded text
  with no parse step, so malformed input is still searchable rather than
  rejecting the upload. XML tags are stripped via the same BeautifulSoup
  `html.parser` path as HTML — a deliberate, XXE-safe choice (no DTD/entity
  resolution); attribute values are dropped, only element text survives.
- **Source code: explicit extension contract (#122).** A 21-extension
  allowlist (`.py .js .ts .tsx .jsx .go .java .rs .c .h .cpp .cs .rb .php
  .swift .kt .scala .sh .sql .r .lua`) plus accepted MIME aliases
  (`text/x-python`, `application/javascript`, `application/x-sh`,
  `application/sql`, and more — see `docs/reference/file-types.md`) replace
  the previous accidental `text/plain`-sniffing path. A generic or absent
  `Content-Type` (`application/octet-stream`) now falls back to the
  filename's extension when it's registered
  (`inh_contracts.file_types.get_spec_for_upload`) — completing the
  fallback-classifier design `FileTypeSpec.extensions` was reserved for at
  #117. The fallback applies only to a GENERIC content type, never to a
  specific-but-unrecognized one, so it cannot widen acceptance of arbitrary
  unknown text files. `rest+mcp`, `chunking_hint="code"` (new
  `ChunkingHint` value, not yet consumed pending #129).
- **Transcripts: SRT and WebVTT subtitle extraction (#127).** Registered as
  `application/x-subrip`/`.srt` and `text/vtt`/`.vtt`, `rest+mcp`,
  `chunking_hint="prose"`. Cue numbers and per-cue timestamp lines are
  stripped and cue text is joined into prose, with a coarse `[t=MM:SS]`
  citation marker reinserted every 10 cues so an agent can still cite
  roughly when something was said without per-line timestamp noise
  polluting retrieval. A file with no recognizable cue timestamps fails
  extraction explicitly rather than emitting empty/garbage text.
- **Ingestion-source Temporal memo on `DocumentIngestionWorkflow` starts
  (#141).** `core.document.uploaded.v1` now optionally carries `source`
  (`connector:<provider>` | `public-api` | `manual`), `connection_id`, and
  `sync_id` (`inh_contracts.events.DocumentUploadMessage`, additive/backward
  compatible, `max_length=500` each — legacy messages without these fields
  still validate; an oversized value is rejected as a validation error rather
  than silently truncated, so it hits the existing poison/dead-letter path
  instead of ever reaching Temporal). Both `TemporalWorkflowTrigger` start
  paths (`trigger_workflow`, `trigger_workflow_async` in
  `services/inh-ingestion-svc/src/temporal/trigger.py`) attach a Temporal
  memo built from these fields so the Temporal UI workflow summary shows
  where an ingestion came from; a message with no `source` memos as
  `"unknown"` rather than failing the start. `inh-public-api-svc` (both the
  REST upload route and the `upload_document` MCP tool — the only in-repo
  publisher of this event) always sets `"source": "public-api"`. Memo only —
  no namespace search-attribute registration required; workflow input itself
  is untouched.
- **File-type support registry — single contract for validation, sniffing,
  extraction, and docs (#117).** Adding a supported file type used to require
  coordinated edits across 5+ places (`ALLOWED_MIME_TYPES` in
  `inh-public-api-svc`, the MCP tool's own text-type subset, ingestion's
  extraction if/elif chain, three docs pages, and two test files), with
  nothing enforcing agreement between them. `FILE_TYPE_REGISTRY`
  (`services/inh-contracts/src/inh_contracts/file_types.py`, imported by both
  services) is now the one place a type is declared — extensions, MIME
  types, magic-byte signature, upload surfaces (REST/MCP), extraction
  dispatch key, and a `chunking_hint` field the upcoming format-aware
  chunker (#129) will consume. `docs/reference/file-types.md`'s
  supported-types table is generated from the registry
  (`scripts/generate_supported_formats.py`) and CI-verified against it
  (`services/inh-public-api-svc/tests/unit/test_docs_sync.py`) so the table
  can no longer silently drift from the code, closing the same drift class
  as #9 and the registry lesson as #100. All 8 existing formats migrated
  with no behavior change to correctly-labeled uploads.

- **Email, EPUB, RTF, and ODT upload support (#124, #125, #126).** Four new
  `FILE_TYPE_REGISTRY` entries, REST-only: `.eml` (`message/rfc822`, stdlib
  `email` — headers From/To/Cc/Date/Subject always extracted, body prefers
  text/plain and falls back to text/html through the existing HTML
  extractor, attachments are not extracted but their filenames and count
  are recorded in a clearly labeled section so an agent knows content was
  elided, nested `message/rfc822` parts inspected one level only);
  `.epub` (`application/epub+zip`, stdlib `zipfile` — chapters extracted
  in `content.opf` **spine order**, numbered by SPINE POSITION (not
  extraction-success count, so one skipped chapter never renumbers the
  rest), preferring each chapter's own `<title>`/`<h1>` as its heading, and
  run through the existing HTML extractor rather than a second parser;
  manifest hrefs are percent-decoded (EPUB OPF spec); nav/cover items
  skipped; DRM/encrypted EPUBs fail with a clear `error_message` instead of
  crashing); `.rtf` (`application/rtf` + `text/rtf` alias, new core
  dependency `striprtf`, magic-byte check anchored to the first few bytes so
  ordinary prose that mentions RTF's own signature isn't mislabeled);
  `.odt` (`application/vnd.oasis.opendocument.text`, stdlib `zipfile` +
  `content.xml` walked ODF-structure-aware via `ElementTree` — `no odfpy`
  needed — excluding `text:tracked-changes` (retracted/deleted revision
  text) and `office:annotation` (private reviewer comments) from indexed
  text, and mapping `text:s`/`text:tab` to real whitespace rather than
  markup residue). EPUB and ODT share the ZIP `PK\x03\x04` signature with
  DOCX — verified DOCX uploads still validate correctly with both
  registered (`test_docx_still_validates_with_epub_and_odt_registered`).
  Legacy `.doc` (`application/msword`) and Outlook `.msg`
  (`application/vnd.ms-outlook`) are explicitly rejected on BOTH REST and
  MCP (`upload_document`, including when `content_type` is omitted and
  would otherwise default from the file extension) with `400`/`Error` and
  an actionable "convert to .docx" / "export to .eml" message rather than
  accepted and garbled — sourced from one shared
  `inh_contracts.EXPLICITLY_UNSUPPORTED` table so the two surfaces cannot
  disagree.

- **Retrieval-eval hard gate, baseline ratcheting, and trend history (#139).**
  Implements the "v2" items ADR 0003 deferred (run-over-run regression deltas,
  a CLI/CI gate). The compose retrieval-eval baseline diff
  (`corpus/retrieval_baseline.json`) is no longer print-only: any per-mode
  metric regressing beyond `EVAL_GATE_TOLERANCE` (default 0.02) now fails the
  build (`tests/evals/eval_gate.py`), on top of the existing absolute-floor
  backstop. A green gate on `main` ratchets the baseline up to
  `max(current, baseline)` per mode/metric (never down) and appends a line to
  a new `corpus/retrieval_history.jsonl` trend log
  (`.github/workflows/integration.yml`); a failed gate on `main` or nightly
  files/updates a tracking issue instead of only failing silently in CI logs.
  Still post-merge only, not a PR gate (the full Compose stack stays too
  slow/expensive to run on every PR). Golden corpus expanded with
  `exact_id`/`stale_version`/`paraphrase`/`abstention` query categories
  (new fixtures `error-codes.txt`, `release-notes-v1.txt`/`v2.txt`) and
  per-category reporting, so "beats baseline" covers more than generic
  doc-lookup queries. No new eval-scoring dependency — metrics stay
  dependency-free/in-process per ADR 0003's no-LLM-judge boundary.

- **Retrieval baseline published in `README.md` (#158).** The enforced
  retrieval-quality floor is now rendered as a per-mode table in the README by
  `tests/evals/render_baseline_table.py`, regenerated by the
  `eval-baseline-ratchet` job in the same commit that ratchets the baseline, so
  the advertised numbers can never drift from the gated ones. Renders the
  baseline rather than `retrieval_history.jsonl` to keep README diffs
  meaningful — history appends on every main-branch run, the baseline moves
  only on a real improvement — and carries no commit SHA, since a ratcheted
  baseline is a per-metric `max()` whose values may come from different
  commits. `README.md` added to `integration.yml`'s `paths-ignore` so the
  ratchet PR's own merge cannot re-trigger the workflow. (#158)

- **Documentation site.** MkDocs Material site published to GitHub Pages
  from `docs/`, with REST API / MCP tools / configuration reference pages
  and on-site release notes rendered from this changelog. New `Docs` CI
  check builds with `--strict` on every PR. Release tagging + docs-currency
  rules added to `CLAUDE.md` and `docs/maintainers/releasing.md`. (#115)

- **Ingestion eval hardening (REQ-EVL-2).** `test_chunk_token_budget` asserts
  no chunk from any golden fixture exceeds the embedding model's token budget
  (using the existing `estimate_tokens()`/`_token_budget_char_cap()` math
  against the real sample documents, not just unit-tested in isolation).
  `text_whitespace_ratio` (a production `DataQualityService` *warning*) is now
  an eval-only *hard fail* for the bundled fixtures — a golden document
  producing noisy extraction is an extractor regression, not unpredictable
  real-world input, without changing production severity for real documents.

- **Benchmark JSON report artifacts (REQ-EVL-3).** Both services' live Compose
  benchmarks now persist a JSON summary (p50/p95/p99/QPS for search,
  docs/sec for ingestion, plus commit SHA) instead of only printing to stdout
  — `search-benchmark-report.json` / `ingestion-benchmark-report.json`,
  uploaded as CI artifacts by `integration.yml` alongside the existing
  retrieval-eval report. Visibility only; the existing loose SLO assertions
  are still what fails a build.

- **Per-document result diversification (#146).** New `enable_diversification`
  flag round-robins search results across `document_id` before truncating to
  the page size, so one long, many-chunk document can no longer silently
  crowd every other relevant document out of the result page — measured on a
  new golden-corpus category (`multi_doc_crowding`, `q14`): recall@5
  0.5 → 1.0, nDCG@5 ~0.61 → ~0.88-0.92 across all three search modes, with
  every pooled per-mode metric flat or improved and none regressed. Shipped
  opt-in (default `False`), gated behind the same eval-gate policy as the
  #47 advanced methods (documented improvement + maintainer approval before
  defaulting on) because it changes ranking order for every multi-chunk
  query, not just crowded ones. **Flipped to default `True` later in this
  same `[Unreleased]` section once both gate conditions were met — see the
  `### Changed` entry below and
  [ADR 0004](https://github.com/inherent-prime/inherent/blob/main/docs/adr/0004-per-document-diversification.md)
  and its amendment.**

### Changed

- **Config defaults single-sourced to stop silent drift (#202).** The
  Postgres database name is now `DEFAULT_DATABASE_NAME` in
  `inh-public-api-svc`'s `config/constants.py`, feeding both the
  `database_url` default and `cloud_sql_database` instead of being spelled
  `"knowledge_base"` twice; both services' `embedder.py` now derive
  `_DEFAULT_URL`/`_DEFAULT_DIM` from `Settings.model_fields[...].default`
  rather than re-hardcoding the TEI URL and embedding dimension as separate
  literals; `inh-ingestion-svc`'s dead `TASK_QUEUE_NAME` constant (a stale
  duplicate of `settings.temporal_task_queue`) is removed. Same values, no
  behavior change — asserted by test. Recovered from an unmerged branch
  authored by Prime and rebased onto the current registry-derived
  `ALLOWED_MIME_TYPES`.
- **Two more cross-service config defaults single-sourced: bucket name and
  mongodb_uri (#176).** Same drift shape #132 fixed for the S3 region:
  `inh-ingestion-svc`'s `storage_bucket` defaulted to `""` while
  `inh-public-api-svc`'s `aws_s3_bucket` defaulted to `"inherent-documents"`.
  Public-api is the writer (it stamps its own bucket into both the DB row
  and the MQ upload payload); ingestion only reads (`BaseStorageBackend`
  exposes `read_file`/`file_exists`/`get_size`, no write path), falling back
  to its own `storage_bucket` default only for a bucket-less event. So the
  empty default didn't misdirect a write — it made ingestion unable to
  resolve ANY bucket for such an event, raising `RuntimeError("No bucket
  specified")` and leaving the document stuck `failed`. Both now default
  from the shared `inh_contracts.defaults.DEFAULT_S3_BUCKET`
  (`"inherent-documents"`) — ingestion's default changes from `""` to that
  value, so a bucket-less event now resolves instead of failing. Both
  services'
  `mongodb_uri` defaults (`.../27017` vs `.../27017/main`) are now also
  single-sourced (`DEFAULT_MONGODB_URI`, no path segment); functionally a
  no-op either way since both services select the database explicitly via
  `mongodb_db_name`, not the URI path. Anti-drift contract tests added on
  each side (extends `test_settings_config_dedup_contract.py`).
- **⚠️ BREAKING (behavior) — format-aware chunking driven by the registry
  `chunking_hint` (#129).** `chunk_text` previously resolved
  sentences/paragraphs/tokens purely from config — the same rule for a
  one-page memo and a 10,000-row XLSX. **Measured cost, using the SHIPPED
  defaults** (`CHUNKING_STRATEGY=sentences`, `MAX_CHUNK_SIZE=1000`,
  `CHUNK_OVERLAP=200`, `EMBEDDING_MAX_TOKENS=512` → effective 787-char
  budget — every value `.env.example`/`settings.py` actually ship, not a
  hypothetical `tokens` config): a 10,000-row XLSX (510,258 extracted chars)
  produced exactly **ONE chunk**, because the sentence splitter (`[.!?]`)
  never finds a boundary in pipe-delimited rows with no sentence-ending
  punctuation — and `embedder.py`'s TEI call uses `truncate=True` at the
  embedding model's ~256-token input limit, so **~99.8% of that single
  chunk's content was silently discarded before a vector was ever
  computed.** This is not a worst case; it is what the out-of-the-box
  configuration does today. A synthetic `.eml` under the same defaults
  produced 14 chunks, of which exactly 1 carried both `From:` and
  `Subject:`. `chunk_text` now resolves its strategy by precedence:
  per-document override > registry `chunking_hint`
  (`inh_contracts.FILE_TYPE_REGISTRY`, #117) > global `CHUNKING_STRATEGY`.
  Every one of the 14 currently-registered formats has a hint, so
  **`CHUNKING_STRATEGY` no longer governs chunking for any of them** — it is
  now consulted only for a content type with no registry entry (already a
  rare, near-error path since #117 hard-fails unregistered types at
  extraction). **Upgrade:** there is currently no per-document lever to force
  one strategy uniformly after this change — `DocumentIngestionInput.
  chunking_strategy` exists at the workflow/activity layer but neither the
  REST `POST /v1/documents` route nor the MCP `upload_document` tool expose
  it yet (tracked separately, #198); a deployment that relied on
  `CHUNKING_STRATEGY` for non-default behavior across every format has no
  workaround until #198 lands. Hint dispatch: `tabular` (csv, xlsx)
  row-based chunking that never splits a row and carries the table header
  (+ XLSX sheet heading, "(continued)" suffix stripped from injected copies)
  into every chunk, packing reserves room for that injection up front so
  content never exceeds the configured budget; `structured` (json, pptx)
  section-based chunking split at the extractor's own `## ` markers,
  degrading to size-based chunking when none exist; `prose` (txt, markdown,
  docx, eml, epub, rtf, odt, pdf, html) unchanged sentence chunking unless
  the text opens with a `Key: value` header block that's at least 2 lines
  long (an email's From/To/Cc/Date/Subject), in which case that block is
  carried into every chunk instead of only the one positionally containing
  it — each field/line capped independently at 200 chars rather than
  truncating the whole block from the tail, so a large recipient list
  trims itself rather than dropping `Subject:` (the field emitted last,
  and the one with the most retrieval value); the sentence chunker's
  `overlap` is clamped to at most half of the header-reserved budget so a
  large header can't collapse chunking stride toward zero. `media` (png)
  unchanged size-based chunking. Any injected context (table header,
  section heading) is capped at min(500, max_size / 3) chars and a slicer
  for an oversized single row/line guarantees at least `max_size / 5` chars
  of real forward progress per chunk regardless of context size — both
  scale with the configured budget instead of using a fixed absolute cap,
  which could otherwise turn one oversized row into one chunk per
  character at a small `MAX_CHUNK_SIZE` or embedding token budget. Measured
  after (same shipped defaults): the same XLSX produces 801 chunks, 801/801
  (100%) self-describing, at **+6.9%** total content chars (context
  injection, no overlap on the row-based path); the same `.eml` produces
  17/17 (100%) self-describing chunks at **+27%** chars (the injected
  header cost, now correctly bounded regardless of recipient-list size —
  a large recipient count no longer explodes chunk count: two independent
  reviews measured a naive unclamped overlap turning a 32-chunk email into
  348 chunks with only 1/348 still carrying `Subject:`; this is fixed).
  Every chunk now records the strategy that produced it in
  `metadata.chunking_strategy` (`rows` / `sections` / `prose_header` /
  `sentences` / `paragraphs` / `tokens`) for eval attribution, persisted in
  Postgres `document_chunks` metadata JSONB and as a new Weaviate
  `chunking_strategy` TEXT property, `index_searchable=False` since it's a
  closed set of internal names, not prose to keyword-match (added to
  `_get_chunk_properties`; existing collections pick it up via the existing
  `_reconcile_collection_properties` add-missing-property path — additive,
  no manual migration needed). Surfacing it through `inh-public-api-svc`'s
  search response is tracked separately (#196) — that service's GraphQL
  query has an explicit field list that doesn't select it yet. See
  `docs/reference/configuration.md`'s "Format-aware chunking" subsection,
  `docs/examples/README.md`'s note on the relaxed chunk-offset invariant,
  and the module docstring in
  `services/inh-ingestion-svc/src/temporal/activities/chunk.py`.
  **Existing indexed documents are unaffected by this release** — they keep
  their old chunk boundaries until re-ingested/refreshed; there is no
  migration or backfill in this change, so search results mix old- and
  new-style chunks until a workspace's documents are re-ingested. Follow-up
  work tracked separately: #196 (surface `chunking_strategy` through
  search), #198 (wire the per-document override through the upload
  surface), #199 (tabular/structured judgments in the retrieval-eval
  corpus — the tabular hint's row-based chunking, the largest behavioral
  change here, is currently invisible to the eval gate).

- **⚠️ BREAKING (behavior) — per-document result diversification now on by
  default (#146).** `enable_diversification` (added opt-in, off by default,
  in the entry above) now defaults to `True`: both eval-gate conditions this
  ADR required — documented eval improvement (recall@5 0.5 → 1.0 on the
  `multi_doc_crowding` golden-corpus category, every other pooled per-mode
  metric flat or improved) and maintainer approval — are now met (ADR 0004's
  2026-08-06 amendment). Search results for every caller now round-robin
  across `document_id` before truncating to the page size instead of a plain
  score-sorted truncate, so a query where one document's chunks previously
  filled the whole result page will now surface other genuinely relevant
  documents too; ranking order can shift for any multi-chunk-per-document
  query, not just crowded ones, even when nothing about that query's own
  relevance changed. Reproduced concretely by this same release's #129
  chunking change: `sample.json` going from 1 to 4 chunks crowded the
  keyword-mode top-5 on the golden corpus (`keyword.mrr` 0.8205 → 0.7821,
  `keyword.ndcg@5` 0.7137 → 0.6835, both beyond the 0.02 gate tolerance,
  `recall@5` unchanged in all three modes — the right document stayed
  retrievable, only its rank slipped); diversification exists to prevent
  exactly this. The mechanism (`SearchService._diversify_by_document`, the
  wider Weaviate over-fetch in `_build_graphql`) is unchanged code — only the
  default moved. **Upgrade:** an operator who wants the pre-#146 ranking sets
  `ENABLE_DIVERSIFICATION=false`. See
  [ADR 0004](https://github.com/inherent-prime/inherent/blob/main/docs/adr/0004-per-document-diversification.md)
  and its amendment.

### Fixed

- **MCP `upload_document` tool schema described a stale, false
  `content_type` contract (#193).** The `_TOOLS["upload_document"]`
  description and its `content_type` schema property both hardcoded
  "must be one of text/plain, text/markdown (default), text/csv, text/html"
  and "must be text/*" — stale since #121/#122/#127 added YAML/TOML/XML,
  source code, and SRT/WebVTT: ~30 types are accepted today, several not
  `text/*`-prefixed (`application/typescript`, `application/x-sh`,
  `application/sql`, `application/yaml`), and the default is derived from
  the filename's extension, not a flat `text/markdown`. This is the surface
  an MCP agent reads via `list_tools` before ever opening `docs/`. Both
  strings are now built from `SUPPORTED_TEXT_MIME_TYPES`
  (`inh_contracts.file_types.mcp_mime_types()`) at module load, so they
  cannot restate a stale subset again. The module docstring's matching
  stale list was also corrected to describe the mechanism instead of a
  literal type enumeration.

- **MCP `upload_document` schema advertised a client-visible `content_type`
  default, defeating #197's extension-derived default for any client that
  auto-fills an omitted argument from its advertised default (#193 review
  follow-up).** The `content_type` schema property still carried
  `"default": "text/markdown"` after the #193 fix above — a JSON Schema
  `default` is sent to the CLIENT, and several real MCP clients pre-fill an
  omitted argument from it before the server ever observes an omission, so
  `content_type = declared_content_type or _default_upload_content_type(...)`
  received an explicit `"text/markdown"` on every such client's upload
  regardless of the real extension, silently reintroducing #197's "code
  file mislabelled as prose" defect through the schema (measured: `main.go`
  with `content_type` absent stored `text/x-go`/`chunking_hint=code`; with
  the client-filled `text/markdown` default it stored
  `text/markdown`/`chunking_hint=prose`). The `default` is removed; the
  fallback behavior is documented in the property's `description` text only
  (never advertised as a schema default), and an explicitly-declared
  `content_type` is still always honored as-is, never re-derived from the
  filename.

- **PDF and JSON extractors retried deterministic (unfixable) failures 3x
  (#195).** `_extract_pdf_text`/`_extract_json_text`
  (`services/inh-ingestion-svc/src/temporal/activities/extract.py`) left
  `pypdf.PdfReader()` construction and `json.loads` completely unwrapped — a
  corrupt/truncated/password-protected PDF or malformed JSON upload raised
  the library's raw exception directly, and Temporal's default 3-attempt
  `RetryPolicy` retried it 3x before giving up on an outcome already known
  at attempt 1 (the same defect the #118/#119 review fixed for XLSX/PPTX/
  DOCX). Both now raise a non-retryable `ApplicationError` naming the likely
  cause (corrupt/truncated/password-protected/wrong format). PDF's wrap is
  scoped to ONLY `PdfReader()` construction — matching `_extract_xlsx_text`'s
  `load_workbook`-only precedent — so a `MemoryError` from a pathological
  PDF during page iteration stays retryable (a load-dependent condition, not
  a property of the bytes) instead of being permanently dead-lettered.
  Review follow-up: `DocumentIngestionWorkflow.run`
  (`services/inh-ingestion-svc/src/temporal/workflows/document_ingestion.py`)
  interpolated `str(e)` on the Temporal `ActivityError` wrapping every
  activity failure — `ActivityError`'s own message is always the generic,
  hardcoded `"Activity task failed"`; the real cause lives on `.cause` (the
  same trap `chunk_edit.py` already documents and works around). Every
  extraction failure's `error_message`, dead-letter row, and completion
  event read `"Activity task failed"` regardless of this fix, and
  `_classify_error("Activity task failed")` matches none of its keywords —
  classifying as `"unknown"` and never surfacing via
  `GET /dead-letter?error_type=extraction_failed`. Now uses
  `str(getattr(e, "cause", None) or e)` at all four sites, so the real cause
  (this fix's non-retryable `ApplicationError`, or any other activity
  failure) actually reaches the document's `error_message` and classifies
  correctly. Pattern sweep found the same unwrapped-extractor defect,
  unfixed, in EPUB, RTF, ODT, and SRT/WebVTT extraction — filed separately
  as #206 (out of this issue's scope).
- **EPUB, RTF, ODT, and SRT/WebVTT extractors retried deterministic
  (unfixable) failures 3x (#206).** `_extract_epub_text` (10 sites),
  `_extract_rtf_text` (3), `_extract_odt_text` (6), and
  `_extract_subtitle_text` (1)
  (`services/inh-ingestion-svc/src/temporal/activities/extract.py`) raised a
  bare `RuntimeError` on corrupt/truncated zips, DRM-protected EPUBs,
  missing/unparseable container.xml/content.opf/content.xml, a missing
  spine or `office:body`, a missing `striprtf` dependency, an unparseable
  RTF/EPUB/ODT payload, or no cues with a recognizable timestamp line — all
  deterministic given the uploaded bytes, so Temporal's default 3-attempt
  `RetryPolicy` retried an outcome already known at attempt 1. Same fix
  shape as #195 and the #118/#119 precedent: all 20 sites now raise a
  non-retryable `ApplicationError` naming the likely cause. Every EPUB/ODT
  except clause is scoped to a narrow exception type
  (`zipfile.BadZipFile`/`KeyError`/`ET.ParseError`), so a `MemoryError` from
  a pathological zip or oversized XML part is never caught and stays
  retryable; RTF's `rtf_to_text` wrap explicitly re-raises `MemoryError`
  before its broad `except Exception`, mirroring `_extract_pdf_text`'s
  construction-only wrap (#195 review follow-up). Pattern sweep of the rest
  of this file plus both services found one related, unfixed defect (not in
  this issue's scope): `_extract_docx_text`'s own broad except wraps its
  paragraph-iteration loop with no `MemoryError` carve-out, unlike the
  PDF/EPUB/ODT precedent it should be following — filed as #215.
- **MCP upload labelled every source-code file `text/x-python` when
  `content_type` was omitted (#197).** `_default_upload_content_type`
  (`services/inh-public-api-svc/src/mcp_server/server.py`) took
  `spec.mime_types[0]` as "the" MIME type for a resolved spec — correct for
  every format registered before #122, each of which describes exactly one
  format, but the `code` spec (#122) pools 22 MIME aliases across 21
  distinct languages under one registry entry, so `mime_types[0]` was always
  `"text/x-python"` regardless of the real language: `app.js`, `lib.go`,
  `Main.java`, `q.sql`, `s.sh`, and `x.rs` all resolved to `text/x-python`.
  `inh_contracts.file_types.FileTypeSpec` gained a `mime_type_by_extension`
  override (populated for the `code` spec only) and a
  `mime_type_for_extension` helper that consults it, falling back to
  `mime_types[0]` unchanged for every single-format spec. Functionally
  harmless (every code MIME routes to the same extractor) but a wrong
  language label on a retrieved code chunk is an integrity problem for an
  agent citing search results. Pinned cross-surface in
  `tests/contract/test_failure_parity.py::TestUploadCodeContentTypeLabelParity`
  (REST's client-declared `Content-Type` and MCP's now-fixed default must
  store the identical label for the same file).
- **`HEALTH_CHECK_TIMEOUT_SECONDS` was declared but had zero call sites;
  setting it silently did nothing (#203).** `inh-public-api-svc`'s
  `/health/ready` endpoint read hardcoded `DATABASE_HEALTH_CHECK_TIMEOUT` /
  `WEAVIATE_HEALTH_CHECK_TIMEOUT` constants instead of
  `settings.health_check_timeout_seconds` — an operator raising the timeout
  to stop a liveness/readiness probe from flapping against a slow Postgres
  or Weaviate changed nothing; the probe kept timing out at the hardcoded
  5.0s. Replaced with two independent knobs,
  `database_health_check_timeout_seconds` (env
  `DATABASE_HEALTH_CHECK_TIMEOUT_SECONDS`) and
  `weaviate_health_check_timeout_seconds` (env
  `WEAVIATE_HEALTH_CHECK_TIMEOUT_SECONDS`), rather than reconnecting the
  single dead knob — the readiness check already treats the two
  dependencies as having different latency tolerances (100ms vs 500ms
  "high latency" thresholds; Weaviate vector search is expected to be
  slower), so one shared timeout would force one dependency's probe to
  inherit the other's budget. No behavior change: both new knobs default to
  `5.0`, identical to the two constants they replace, and the removed
  `HEALTH_CHECK_TIMEOUT_SECONDS` had no reader to begin with (both
  services' `model_config` uses `extra="ignore"`, so a value an operator had
  set for it is silently dropped, not an error). Also fixes a deeper
  instance of the same defect found while wiring this up:
  `SearchService.is_connected()` hardcoded its own 5.0s `httpx` timeout
  independently of the `wait_for` wrapping it in `health.py`, so raising the
  new Weaviate setting above 5.0 still would not have worked; `timeout` is
  now a required parameter the caller must supply explicitly.
- **⚠️ BREAKING (deploy) — S3 region default drift between inh-ingestion-svc
  and inh-public-api-svc (#132).** public-api's `AWS_S3_REGION` defaulted to
  `eu-central-1` while ingestion's `AWS_REGION` defaulted to `nbg1` (a Hetzner
  Object Storage location code, never actually reachable as an app default);
  a deployment that set only one of these two differently-named env vars
  silently left the other service on its own default, so uploads and reads
  could target different regions/buckets. Both now default from a single
  `inh_contracts.defaults.DEFAULT_S3_REGION` (`us-east-1`, matching the
  deployed default already in `docker-compose.yml` / `infra/server.tf` /
  `.env.example`), and public-api now also accepts a lone `AWS_REGION` (with
  `AWS_S3_REGION` still taking precedence when both are set), so setting
  ingestion's var alone — as `docs/deploy/production.md` step 3 already
  instructs — now configures both services identically. An anti-drift
  contract test on each side pins the shared constant and the alias fallback.
  **Upgrade:** any deployment that did not explicitly set `AWS_REGION` /
  `AWS_S3_REGION` changes its S3 signing region on upgrade (`nbg1` /
  `eu-central-1` → `us-east-1`); set the var explicitly before upgrading if
  your bucket lives elsewhere.
- **⚠️ BREAKING (behavior) — re-indexing a document while its prior ingestion
  workflow was still open stalled ~10min instead of completing (#110).**
  `DocumentIngestionWorkflow` is started with a fixed id
  (`ingest-{document_id}`) so status queries can address a run by
  document_id. A re-index/refresh enqueued while the prior run for that
  document_id was still open (edited-content re-upload, or the
  `/documents/{id}/refresh` endpoint under load) collided on that id:
  Temporal's default `id_conflict_policy` raised `WorkflowAlreadyStartedError`,
  which propagated out of the MQ handler
  (`TemporalWorkflowTrigger.trigger_workflow_async`,
  `services/inh-ingestion-svc/src/temporal/trigger.py`) so the message was
  never ACKed. Every `RedisMQService` redelivery hit the same still-open run
  and failed again, so the caller waited out however long the stale run took
  to close on its own (~10min observed in CI run 29222060795 — not a fixed
  timeout). The MQ upload/refresh path now passes
  `id_conflict_policy=WorkflowIDConflictPolicy.TERMINATE_EXISTING`, so a
  fresh re-index/refresh always supersedes a stale in-flight run instead of
  colliding with it — the newest content wins immediately instead of after a
  stall. **This changes two caller-visible behaviors**: (1) rapid successive
  re-index/refresh requests for the same document now mean only the LAST one
  completes — an in-flight ingestion can be terminated by an unrelated
  actor's re-index/refresh for the same document, where previously both ran
  to completion in sequence; (2) `POST /ingest?wait=true` can now return
  `409 {"status": "superseded_by_newer_request"}` if a concurrent
  upload/refresh for the same document terminates the run it was waiting on,
  where previously that call always blocked until its own run finished.
  Termination does not stop an already-dispatched Temporal ACTIVITY (only the
  workflow) — a fencing token (`processed_documents.active_run_id`, migration
  016) on the store activities stops a superseded run's late write from
  clobbering the newer run's already-committed content; a dead-letter retry
  (`POST /dead-letter/{id}/retry`) deliberately keeps the OLD
  reject-on-collision behavior instead, since it may be replaying stale
  content. A background sweep (`worker.py::_periodic_staging_cleanup`, every
  15 min) now also cleans `ingestion_staging` rows a terminated run orphans,
  since termination skips the workflow's own cleanup step. The claim itself
  (`create_pending_document`) is ordered by each run's Temporal start time
  (`active_run_claimed_at`, migration 017), not by which claim write commits
  last — otherwise an earlier-starting, terminated run's still-in-flight
  claim could land after, and overwrite, a later-starting run's claim,
  fencing the legitimate newest run out of its own store step (found in
  follow-up review). `update_document_status` is fenced the same way, so a
  terminated run's stale status write can't leave `status='processing'`
  stuck forever on a document whose content is otherwise correct.
  **Upgrade:** run migrations `016_active_run_fencing.sql` and
  `017_active_run_claim_ordering.sql` (`scripts/migrations/`, applied
  automatically by `postgres-init` / `run_migrations.sh`) before deploying —
  the store and status-write activities depend on the `active_run_id` /
  `active_run_claimed_at` columns existing. No configuration changes
  required.
- **⚠️ BREAKING (API) — genuinely mislabeled uploads (byte/type or
  filename/type contradictions) are now rejected at intake; unregistered
  types reaching extraction hard-fail instead of silently garbling (#117).**
  No correctly-labeled upload of any of the 8 supported types is newly
  rejected — that was the bar an internal review held this to, catching and
  fixing four false-positive rejections before merge (see PR discussion):
  sibling text/* mislabeling (`README.md` sent as `text/plain`, `data.csv`
  as `text/plain`, etc. — `text/plain` is a truthful, IANA-valid
  Content-Type for any text file and is routinely what real clients send),
  a PDF whose bytes start with a BOM/leading whitespace before `%PDF-`
  (pypdf parses these fine; the sniff now scans a 1024-byte window instead
  of requiring the signature at byte 0), and a structural bug that would
  have made the registry reject EVERY `.docx` upload the moment a sibling
  OOXML format (#118 XLSX) is registered (shared-signature formats are now
  explicitly tolerated as "cannot disambiguate, allow both" rather than
  "reject both"). What IS newly rejected: (1) bytes that contradict the
  declared type at the binary-signature level (e.g. PNG bytes declared
  `text/plain`, or a file declared `application/pdf` whose bytes are not a
  PDF) — `POST /v1/documents` and the MCP `upload_document` tool (sharing
  the same `intake_document` pipeline) now return `400 Bad Request` before
  anything is stored, no document row, no S3 object; (2) a filename with a
  recognized BINARY extension (`.pdf`, `.docx`, `.png`, ...) that contradicts
  the declared type (e.g. `report.pdf` declared `text/plain`) — a
  text-format extension never triggers this, only a binary one; (3) a
  content type accepted at upload but missing from ingestion's extraction
  dispatch, which fell through to `content.decode("utf-8", errors="ignore")`
  producing empty/corrupted chunks, now fails the document with a clear
  `error_message` instead. Also **benign widening**, not a tightening:
  `Content-Type` matching is now case-insensitive and strips parameters
  (`TEXT/PLAIN` and `text/plain; charset=utf-8` were both previously
  rejected by exact string match and are now accepted as `text/plain`) —
  same 8 supported types, just more of their real-world spellings
  recognized. Text extraction also switched from `errors="ignore"` (which
  silently DELETED any non-UTF-8 byte) to `charset-normalizer` encoding
  detection. **Upgrade note**: a client uploading a file whose declared
  type, filename, and bytes genuinely disagree with each other — previously
  silently "working" with garbled or truncated extracted text — now gets a
  `400` at upload or a `failed` document status instead; this is the
  intended fix, not a regression. A client sending any consistent,
  correctly-labeled upload of the 8 supported types is unaffected.

- **Retrieval-eval baseline ratchet silently never ran (#139 follow-up).**
  `eval-baseline-ratchet` pushed its ratchet commit straight to `main`, but
  branch protection rejects direct `github-actions[bot]` pushes — every
  attempt failed with `remote rejected (protected branch hook declined)`, so
  the committed baseline stayed at its seeded zeros on every green run since
  #139 shipped, leaving the relative gate a no-op (only the absolute
  `RETRIEVAL_MIN_RECALL5` floor was ever live). The job now ratchets on a
  dedicated branch and opens (or updates) a PR instead of pushing to `main`,
  with auto-merge requested so a clean ratchet still needs no human action
  (`.github/workflows/integration.yml`). The PR-based fix went through two
  more rounds of cross-review before landing: the open-PR check
  (`gh pr view`) also matched an already-merged PR on the same reused branch,
  which would have silently skipped `gh pr create` forever after the first
  merge (fixed via `gh pr list --state open`); the branch's own
  baseline/history are now pulled forward before recomputing when a prior
  ratchet PR is still open, instead of resetting to `main`'s older copy,
  so a not-yet-merged rise is never dropped; and `--force-with-lease` now has
  the remote branch actually fetched first, since leasing against a ref that
  was never fetched was rejected as stale on any run after the first. The
  default `GITHUB_TOKEN` also doesn't trigger `ci.yml` on the PR it opens
  (GitHub excludes actions performed with it from firing other workflow
  runs), so the job now prefers an optional `RATCHET_PR_TOKEN` repo secret
  and falls back to a normal maintainer-merged PR if that secret isn't set.
  Seeded the first `corpus/retrieval_history.jsonl` line from a real
  measured run on `main` (commit `201363a`) instead of zeros, so the
  relative gate is live immediately; `corpus/retrieval_baseline.json` was
  seeded from that same run and then re-seeded again below once the golden
  corpus grew (see the diversification entry) — the committed baseline
  reflects the later measurement, not `201363a`, and the `_comment` field
  states which commit it came from. Also corrected
  `docs/advanced-indexes.md`'s placeholder eval targets from `@10` to `@5`
  — the compose gate only ever computes `recall@5`/`nDCG@5`, so a future
  advanced method cleared against `@10` numbers would not be measurable
  against the gate that exists.

- **Rate limiting never applied per-API-key in production (#149).** Starlette's
  `add_middleware` makes the *last*-added middleware run *first*; `main.py`
  registered `AuthenticationMiddleware` before `RateLimitingMiddleware`/
  `AuditLoggingMiddleware`, so the real dispatch order was
  `RateLimit → Audit → Auth`, the reverse of the file's own comment.
  `RateLimitingMiddleware` therefore always read `request.state.api_key_info`
  before auth had set it, bucketing every request — valid key or not — at the
  30/min unauthenticated-IP tier and causing cascading 429s under load.
  Reordered registration to match the documented flow; added
  `tests/integration/test_middleware_order.py` against the assembled app so a
  future reordering regresses here, not in production traffic. Also hardens
  the originally-suspected cause: a key-validation backend error is now
  distinguished (`request.state.auth_error`) from a simple missing/invalid
  key, logged at `warning` with an `auth_backend_error` metric instead of
  silently falling to `debug`, and bucketed at the moderate `DEFAULT_RATE_LIMIT`
  rather than the harshest unauthenticated tier.
- **Ingestion audit-log workflow retried forever — its Temporal namespace was
  never registered (#148).** `temporalio/auto-setup` only auto-creates
  `default`; the ingestion audit worker dispatches to a separate `audit`
  namespace (`TEMPORAL_AUDIT_NAMESPACE`) that nothing ever created, so every
  audit-log write failed with `NotFound` and retried in a tight loop for the
  lifetime of the stack. Added a `temporal-init` one-shot compose service
  (`docker-compose.yml`, `docker-compose.release.yml`) that registers it via
  `tctl`, gated on an idempotent `describe` check and wired as a dependency of
  the ingestion service. `tctl namespace describe/register <name>` silently
  ignores a trailing positional namespace and operates on `default` instead —
  the check uses the global `--namespace` flag ahead of the subcommand, and
  only treats a `describe` failure as "missing" when the error says so,
  refusing to register blindly (and blocking ingestion boot) on any other
  failure.
- **`uv.lock` drift from the unused-deps removal.** Both services' lock files
  still listed `aiobreaker`/`psycopg[binary]` (`inh-public-api-svc`) and
  `packaging` (`inh-ingestion-svc`) as locked dependencies after those were
  dropped from `pyproject.toml`; `uv sync --frozen` (used in CI) installs
  straight from the lock without re-checking it against `pyproject.toml`, so
  the stale packages kept installing silently. Regenerated both lock files —
  no behavior change, but the image install surface actually shrinks now as
  intended.
- **Chunk-edit Weaviate vector left stale after an edit.**
  `WeaviateService.update_chunk` updated the `content` property but never
  passed a new `vector=`; chunk collections have no server-side vectorizer,
  so the old embedding stayed attached to the new text and semantic search
  kept matching stale content after a `PATCH /chunks/{document_id}/{chunk_index}`
  edit. Re-embeds the new content and writes the fresh vector; `content_hash`
  now advances alongside `content` in both stores (`update_chunk_postgresql`
  already did this — #9; Weaviate did not) and `ingested_at` now advances in
  both stores too (neither did before this fix — a just-edited chunk
  otherwise reports `is_stale=false` from the search path (Weaviate-backed)
  and `is_stale=true` from the chunk/lineage path (PG-backed) at once). The
  Weaviate-update Temporal activity also now re-raises on failure instead of
  swallowing it into a false success: a permanent Weaviate failure surfaces
  as a 5xx to the caller and is recorded as a compensating `ingestion_events`
  row (itself retried, with CRITICAL-log + metric on exhaustion) instead of
  silently leaving PostgreSQL and the vector store diverged with no signal
  and no record. (#137)

### Security

- **MCP's `call_tool` dispatcher now checks `key_info.is_expired()` itself,
  closing a dispatcher-level failure-parity gap with REST (#180).** REST's
  `require_api_key` (`services/inh-public-api-svc/src/services/auth.py`)
  re-checks expiry after `validate_api_key` returns, rather than trusting the
  DB layer alone. MCP's dispatcher
  (`services/inh-public-api-svc/src/mcp_server/server.py`) previously
  dispatched straight to the tool handler once `validate_api_key` returned a
  non-`None` key, trusting every current and future `DatabaseService`
  implementation to independently filter expired rows. The real
  Postgres-backed implementation already does this (so it was not
  exploitable through it), but any alternate implementation that returned an
  already-expired `key_info` would have been silently accepted by MCP while
  the identical request was correctly rejected by REST. Pinned in
  `tests/contract/test_failure_parity.py::TestExpiredKeyDispatcherParity`.

- **`POST /ingest` (inh-ingestion-svc) accepted a caller-supplied
  `storage_path` alongside a caller-supplied `workspace_id` with no check
  that the two were related — a cross-tenant read path that #175/#177 had
  explicitly scoped this route out of on the reasoning that it is "a
  creation endpoint with nothing to own yet."** That reasoning was wrong:
  the endpoint doesn't just create a `processed_documents` row, it READS an
  object the caller names. A caller holding the single shared
  `INGESTION_API_KEY` could pair another tenant's genuine `storage_path`
  with their OWN `workspace_id`; the pipeline fetched those bytes,
  extracted, chunked, embedded, and filed the result into the attacker's
  own Weaviate tenant — readable afterwards through the attacker's own
  legitimate key, via ordinary search, with no need to compromise the
  victim's workspace at all.

  There is no existing PostgreSQL row for this endpoint to resolve
  ownership against (unlike every other route #175/#177 hardened), so the
  fix checks the one thing that IS knowable up front: the storage layout is
  workspace-prefixed, so `storage_path` must name the claimed `workspace_id`
  as its own path prefix (`workspaces/{workspace_id}/...` or
  `{workspace_id}/...` — both conventions are live in this codebase, see
  `src/api/ownership.py::require_storage_path_workspace_prefix`). The path
  is normalized (`posixpath.normpath`) before the prefix is read off it, so
  a `..`-disguised path can't nominally satisfy the check while actually
  resolving into a different workspace's subtree. Mismatch → **403**;
  `workspace_id` is additionally hardened the same three-layer way #177's
  post-review fix established (`Field(..., min_length=1)` on the request
  body, the shared `require_workspace_id` boundary check, and the resolver
  itself raising on a blank claim rather than silently widening).

  **This is NOT tenant-safety and must not be described as such.**
  `INGESTION_API_KEY` is one shared secret with no key→workspace binding
  (#177 follow-up, still open) — every tenant presents the identical
  secret. This fix proves `storage_path` is CONSISTENT with the
  `workspace_id` the caller CLAIMS; it does not, and cannot, prove the
  caller is ENTITLED to claim that `workspace_id`. A caller who knows or
  guesses a genuine `workspaces/{victim}/...` path can still pair it with
  `workspace_id={victim}` and pass this check. Only the key→workspace
  binding tracked in #177 closes that gap; until it lands, `POST /ingest`
  remains only as safe as every other route this codebase gates with
  `verify_api_key` alone. Pattern sweep (both services, for the same
  "caller-supplied fetch target with no workspace check" shape) found one
  more instance NOT covered by this fix and NOT fixable the same way:
  `storage_backend="azure"` on this same route fetches via an arbitrary
  caller-supplied `storage_url` instead of `storage_path`, bypassing this
  check entirely — no workspace-prefixed invariant exists for an arbitrary
  URL to be checked against. Filed as #214 rather than silently expanding
  this fix's scope or leaving it unmentioned. (#210)
- **`storage_backend="azure"` (the #214 bypass above) is now OFF by
  default.** `fetch_document` and `extract_text` (`inh-ingestion-svc`) both
  treat `storage_backend="azure"` as "fetch `storage_url` directly" — there
  is no real Azure Blob client anywhere in this codebase, and no in-repo
  caller ever sets `storage_backend="azure"` (`inh-public-api-svc`'s intake
  path always emits `s3`). Both activities now reject this branch with a
  clear error unless the new `ALLOW_URL_BASED_INGESTION` setting (default
  `false`) is explicitly turned on, closing #214's `storage_path`-less
  vector for every deployment that hasn't opted in. **This is not full
  closure**: an operator who does turn it on is knowingly left with only
  #34's SSRF guard (blocks metadata/loopback/RFC1918 targets, permits any
  other reachable http/https URL) between a caller-supplied URL and their
  tenant's Weaviate store — the gate controls whether the vector exists at
  all, not what it can reach once enabled. Both activities carry their own
  copy of the gate rather than trusting the workflow's earlier step, since
  Temporal activities are independently retryable/replayable. (#214)
- **`POST /ingest` started `DocumentIngestionWorkflow` with no `source`
  memo, the one of three start sites #141 didn't cover.** `trigger.py`'s two
  MQ-driven start sites have carried a `{"source": ..., "connection_id":
  ..., "sync_id": ...}` Temporal memo since #141, so operators can tell a
  connector sync apart from a manual/API trigger in the Temporal UI summary
  panel. This direct HTTP path showed no memo at all — not even
  `"unknown"` — because its request model (`IngestRequest`) has no
  `source`/`connection_id`/`sync_id` fields to build one from. Rather than
  add unused fields with nothing to populate them, this route now calls the
  same memo-shape function (extracted to module-level
  `build_ingestion_source_memo` in `trigger.py`, `_build_source_memo`
  delegates to it) with a hardcoded `source="api-direct"` — this path is
  inherently a direct/manual trigger with no connector to attribute it to.
  (#178)
- **Seven more inh-ingestion-svc endpoints trusted a caller-supplied
  `workspace_id`/`document_id`/`job_id` with no ownership check, closing the
  `GET /dead-letter` → `PATCH /chunks` cross-tenant harvesting vector #134
  flagged as unresolved — and closing a bypass an adversarial review found
  in this fix's own first draft.** `DELETE /documents/{document_id}` (#175)
  and six more routes (#177) — `GET /ingest/{document_id}/status`,
  `GET /lineage/{document_id}`, `GET /dead-letter`,
  `GET /dead-letter/{job_id}`, `POST /dead-letter/{job_id}/retry`,
  `POST /dead-letter/{job_id}/abandon` — were gated only by `verify_api_key`.
  `DELETE /documents` additionally used the caller-supplied
  `workspace_id`/`user_id` UNVERIFIED to pick the Weaviate tenant to delete
  from and never scoped the PostgreSQL delete at all. `GET /dead-letter` was
  the sharpest edge: `workspace_id` was an *optional* filter, so omitting it
  returned dead-letter rows — each carrying a genuine `(document_id,
  workspace_id, user_id)` triple — across every tenant. A caller holding
  only the shared `INGESTION_API_KEY` could harvest a real cross-tenant pair
  there and present it to `PATCH /chunks/{document_id}/{chunk_index}`:
  #134's guard checks `(document_id, workspace_id)` *consistency*, which a
  harvested pair genuinely satisfies, so it would pass and let the caller
  overwrite (and re-embed) a victim's chunk.

  All seven routes now mirror #134's match-or-404 guard
  (`src/api/ownership.py`: `resolve_owned_document`,
  `resolve_owned_dead_letter_job`): resolve the row against PostgreSQL
  first, 404 unless its stored `workspace_id` matches the caller's claim
  (same response for missing vs. foreign-workspace, so existence doesn't
  leak), and use only the *resolved* workspace_id/user_id downstream —
  never the caller-supplied ones. `GET /dead-letter` also makes
  `workspace_id` a *required*, always-enforced filter instead of an
  optional one.

  **This fix's own first draft was itself bypassable and was caught before
  merge, not after.** `Query(..., min_length=1)` alone rejects a fully
  empty `?workspace_id=` but not a whitespace-only one, and — the actual
  hole — presence validation on the route said nothing about what
  `DatabaseService.get_dead_letter_jobs` then did with the value: it
  guarded its WHERE clause with a bare `if workspace_id:`, falsy for `""`,
  so `?workspace_id=` skipped the filter entirely and returned every
  tenant's rows. `workspace_id` is now validated at three independent
  layers before it can reach a query: `min_length=1` on the `Query`, a
  shared `require_workspace_id` boundary check (strips and rejects
  whitespace-only values, used by every route above), and
  `get_dead_letter_jobs`/`delete_document` themselves now REQUIRE
  `workspace_id` and raise rather than silently widening scope on a falsy
  value — so a future caller of either DB method can't reintroduce the same
  fail-open shape by skipping the route-level check. Every denial (blank
  `workspace_id`, unknown document/job, workspace mismatch) is now logged
  as `workspace_access_denied` (mirrors inh-public-api-svc's
  `services/auth.py`), where none of this was visible before.

  A PostgreSQL failure during any of these lookups propagates as a 5xx;
  none of them fail open.

  **This remains workspace<->row CONSISTENCY, not caller<->workspace
  ENTITLEMENT.** `verify_api_key` is one shared secret with no
  key→workspace binding (unlike the public API's `resolve_workspace_read`),
  so these checks only prove the caller named a workspace/document pair
  that is genuinely consistent in PostgreSQL — not that the caller is
  actually entitled to that workspace. Giving `INGESTION_API_KEY` real
  key→workspace binding is a separate, larger design decision #177's issue
  body tracks as a follow-up; it is NOT resolved by this change. (#175,
  #177)
- **⚠️ BREAKING (API) — `workspace_id` is now a required, non-blank query
  param on six more inh-ingestion-svc routes, and on
  `DELETE /documents/{document_id}` it is now verified, not just
  accepted.** Required by the fix above; a request omitting `workspace_id`,
  or passing it blank/whitespace-only (`?workspace_id=`, `?workspace_id=%20`),
  on `GET /ingest/{document_id}/status`, `GET /lineage/{document_id}`,
  `GET /dead-letter`, `GET /dead-letter/{job_id}`,
  `POST /dead-letter/{job_id}/retry`, or `POST /dead-letter/{job_id}/abandon`
  now gets **422** instead of the previous (unscoped) behavior. Every
  existing caller of these inh-ingestion-svc-internal endpoints must add
  `?workspace_id=<ws>`. (#175, #177)
- **`edit_chunk` wrote to Weaviate with no workspace scoping.**
  `PATCH /chunks/{document_id}/{chunk_index}` (inh-ingestion-svc) was gated
  only by `verify_api_key`, with `workspace_id`/`user_id` left unset on
  `ChunkEditInput` — the downstream Weaviate write derived its
  collection/tenant from empty strings. The endpoint now resolves
  `document_id` against PostgreSQL, 404s unless its stored `workspace_id`
  matches the caller's claimed one, and forwards only the resolved
  `workspace_id`/`user_id` into `ChunkEditInput` so a self-consistent pair
  always lands the write in the document's real tenant. This proves
  workspace<->document *consistency*, not caller<->workspace *entitlement*:
  `verify_api_key` is one shared secret with no key->workspace binding
  (unlike the public API's `resolve_workspace_read`), so a caller that
  already holds a valid `(document_id, workspace_id)` pair for a workspace
  it doesn't own is still not stopped by this fix on its own — seven more
  inh-ingestion-svc endpoints shared that gap, including a
  `GET /dead-letter` → `PATCH /chunks` harvesting vector; see the (#175,
  #177) entry above, which closes that specific harvesting vector by making
  `GET /dead-letter` unable to hand out a genuine cross-tenant pair (verified
  filtering, not just an accepted parameter — see that entry for the
  bypass an adversarial review caught in the first attempt). It does NOT
  close the underlying consistency-vs-entitlement gap those entries both
  describe. (#134)
- **⚠️ BREAKING (API) — `workspace_id` is now a required query param on
  `PATCH /chunks/{document_id}/{chunk_index}`.** Required by the fix above;
  a request omitting it now gets **422** instead of editing the chunk. Every
  existing caller of this inh-ingestion-svc-internal endpoint must add
  `?workspace_id=<ws>`. (#134)

### Removed

- **Unused runtime dependencies dropped** to shrink the install surface of
  both service images: `aiobreaker` and `psycopg[binary]` from
  `inh-public-api-svc` (DB access is async-only via `asyncpg`; no circuit
  breaker or sync driver is imported), and `packaging` from
  `inh-ingestion-svc` (not imported anywhere). No behavior change.
- **Dead `PLAN_RATE_LIMITS` pricing-tier constant removed** from
  `inh-public-api-svc/src/config/constants.py` (and its `config/__init__`
  re-export). It hardcoded commercial plan pricing (`starter`/`pro`/`team`/
  `enterprise`, `$149`–`$2K+`/month) that this OSS repo has no billing system
  for and that was read nowhere — per-key limits come from `ApiKey.rate_limit`
  (default `DEFAULT_RATE_LIMIT`/`RATE_LIMIT_DEFAULT`). No behavior change
  (#151).

### Security

- **⚠️ BREAKING (auth) — MCP now enforces workspace-scoped API key binding,
  and REST/MCP document lookups no longer leak cross-workspace existence
  (#138).** REST's `_resolve_workspace` binds a workspace-scoped key to
  exactly its one workspace; MCP's `_get_workspace_ids` derived access from
  the user's full owned-workspace set instead, letting a scoped key reach any
  workspace its owner also owned via an MCP tool call — silently allowed
  where REST 403'd the identical request. Both surfaces now share one rule
  (`get_authorized_workspace_ids` in `src/services/auth.py`), which also
  fails CLOSED if a key's bound workspace is no longer owned by its user
  (e.g. deleted/transferred after the key was issued) instead of trusting a
  stale binding. This check is a MONGO-ONLY membership lookup
  (`DatabaseService.user_owns_workspace_in_mongo`) — deliberately not the
  broader `get_user_workspace_ids` (which unions Mongo with a Postgres
  upload-history fallback and would keep granting access via that fallback's
  stale rows for a workspace transferred away from its original owner); a
  Mongo failure during this check now raises instead of silently granting or
  denying, so a scoped key's requests error rather than bypass revocation
  during a Mongo outage. **Existing MCP callers using a workspace-scoped key
  may see new errors**: a `workspace_id` argument naming a different (but
  owner-owned) workspace now returns an `Error: ...` result instead of
  succeeding, with wording that now matches REST's (`API key is scoped to
  workspace 'X' and cannot access workspace 'Y'`) instead of a generic "you
  don't have access". **The reverse also happens**: `upload_document` with no
  `workspace_id` and a key scoped to one workspace out of several the owner
  holds previously errored ("multiple workspaces; pass workspace_id") and now
  succeeds directly into the bound workspace. Every document-scoped MCP tool
  (`get_document`, `list_chunks`, `explain_lineage`, `delete_document`,
  `refresh_stale_source`, `get_document_context`) now answers a document that
  exists in an unauthorized workspace with the SAME undifferentiated
  `Error: Document 'X' not found` used for a document that doesn't exist at
  all — closing a cross-workspace existence oracle the previous distinct
  "you don't have access to document" message created (REST's equivalent
  routes were never affected: the workspace-scoped DB query already returned
  `None`, hence `404`, in both cases). `search_documents` / `search_memory` /
  `get_citations` / `list_documents` responses now state the actual set of
  workspaces covered (never claim "all workspaces" when a scoped key narrowed
  to one) and carry a `workspaces_searched` field in their structured JSON
  payload so a caller can verify coverage programmatically; `list_documents`
  gains a structured JSON block it did not have before.

## [0.5.0] — 2026-07-13

Repository-level release tag, continuing from the last published tag
`v0.4.1` (an out-of-band ingestion-svc hotfix — see below). `v0.1.0`/
`v0.1.0-rc1` and `v0.2.0` were never fully published (see
[releasing.md](https://github.com/inherent-prime/inherent/blob/main/docs/maintainers/releasing.md) for the image-publishing flow);
this is the first repository-level tag published since `v0.4.1`. Per-service
package versions (independent of this tag) moved to `inh-contracts` 2.0.0,
`inh-ingestion-svc` 0.5.0, and `inh-public-api-svc` 0.2.0 alongside this tag.

### Fixed

- **Re-uploading identical content no longer re-indexes it (#109).** A
  content-hash dedup match (#75) means the exact bytes are already ingested, so
  the shared `document_intake` (REST + MCP) now returns the existing document
  as-is instead of resetting its row to `pending` and re-running
  extract→chunk→embed→index. Besides saving the agent redundant compute, this
  removes a hazard: because the ingestion workflow id is fixed per document,
  a redundant re-index could serialize behind the in-flight run and strand the
  document non-`processed` for minutes under load. Filename-dedup and
  edited-content re-uploads (#60) differ in content_hash and still re-index; a
  match on a `failed` document still re-indexes to recover. The deeper
  fixed-workflow-id re-index stall (still reachable via edited-content re-upload
  and refresh under load) is tracked in #110. Also un-blocks the Compose e2e
  release gate (`integration.yml`), which had been red since the per-key rate
  limiter (#5) 429'd the throughput-heavy compose suite — the CI stack now runs
  rate-limiting disabled (local/dev + release parity unchanged).
- **Compensating mark-failed writes are retried (#99).** When an MQ publish
  fails and the compensating `mark_document_failed` write also fails, the mark
  is now retried with exponential backoff (3 attempts) via the new
  `src/services/compensation.py` helper. Exhaustion emits a CRITICAL log and
  bumps the new `document_compensation_exhausted_total{operation}` Prometheus
  counter instead of silently orphaning the row as `pending` while the
  response says `failed`. Applies to all three compensation sites: upload
  intake (shared REST + MCP), REST refresh, MCP refresh. The #99 contract in
  `tests/contract/test_failure_parity.py` is now enforced (xfail removed) and
  the refresh double-failure pair is pinned on both surfaces. Durable lesson
  recorded in [docs/developer/learnings.md](https://github.com/inherent-prime/inherent/blob/main/docs/developer/learnings.md).

A milestone-by-milestone push to make Inherent a self-hostable, permission-aware
agent **memory substrate** an organization can run on day one. Delivered as a
stack of reviewable PRs (merge order: #65 → #66 → #67 → #68 → #69 → #70, on top
of the already-merged M0–M2 #62/#63/#64). See
[docs/maintainers/org-readiness-requirements.md](https://github.com/inherent-prime/inherent/blob/main/docs/maintainers/org-readiness-requirements.md)
and [ADR 0001](https://github.com/inherent-prime/inherent/blob/main/docs/adr/0001-agent-memory-substrate.md).

### Changed

- **MCP tool registry (#100).** Every MCP tool is now declared exactly once in
  a `_TOOLS` registry (schema + permission + handler); `list_tools`,
  permission enforcement, and dispatch all derive from it. Previously a tool
  had to be registered in 4 disjoint places, so it could be advertised but
  unusable (or callable but hidden) with no compile-time or test signal. No
  behavior change — same tools, schemas, and permissions.

### Added

- **REST ↔ MCP failure-parity contract suite** (`tests/contract/
  test_failure_parity.py`): dependency-failure tests (MQ down, vector store
  down) asserting both surfaces leave the same document state and surface an
  error. The #98 contract (MCP refresh must mark a document failed, not strand
  it 'pending', on an MQ outage) is now **enforced** — its fix landed in #96
  (see below). One `xfail` pin remains for #99 (upload's compensating
  mark-failed is not retried), to flip to enforced the moment that fix lands.
- **CLAUDE.md defect-prevention rules** from the #98/#99/#100 retrospective:
  pattern sweep after bug fixes, dual-surface failure parity, compensated
  state mutations, registry-only MCP tool registration, and friction/unfiled-
  defect reporting.
- **Evals v1 — traffic-mined retrieval evals (#91).** Operators can now get a
  defensible retrieval-quality number for their own corpus without authoring a
  golden set. Search responses carry an `event_id`; consuming agents (or the
  new `docs/examples/eval_trial.py` trial-labeling script) report a verdict via
  `POST /v1/evals/feedback` (MCP: `report_feedback`), and positive feedback
  auto-promotes the query into a labeled eval case. `POST /v1/evals/runs`
  replays the active cases as a keyword-vs-semantic-vs-hybrid mode comparison
  scored with dependency-free recall@k / MRR / nDCG; `GET /v1/evals/scorecard`
  (MCP: `get_retrieval_health`) gives the day-one summary (answer rate, corpus
  gaps, labeled-case count). Rounds out with `GET /v1/evals/cases`,
  `PATCH /v1/evals/cases/{id}`, `GET /v1/evals/runs/{id}`, and
  `DELETE /v1/evals/events`. Capture is on by default (write-behind, never
  blocks or fails a search), per-tenant opt-out via
  `EVAL_CAPTURE_DISABLED_WORKSPACES`, raw events purge after
  `EVAL_RETENTION_DAYS` (default 30) or on demand. Adds migration `015_evals.sql`.
  Design boundary — retrieval-layer evals only, no answer/task grading, no LLM
  judge, no second service — is recorded in
  [ADR 0003](https://github.com/inherent-prime/inherent/blob/main/docs/adr/0003-traffic-mined-retrieval-evals.md); quickstart in
  [docs/getting-started/local.md](https://github.com/inherent-prime/inherent/blob/main/docs/getting-started/local.md#6-judge-retrieval-quality-evals).
- **Document delete — REST + MCP (#87 P1).** An agent can finally retract
  knowledge: `DELETE /v1/documents/{id}` and the MCP `delete_document` tool
  remove a document's Weaviate objects (tenant-scoped), its PostgreSQL row +
  chunks (transactional, with workspace stat decrement), and best-effort the
  stored S3 bytes. Both surfaces share one deletion orchestrator; vectors are
  deleted before the database row so a mid-flight failure stays retryable
  instead of leaving orphaned vectors in search. Requires **write** permission
  and is workspace-scoped — cross-workspace documents read as not-found. The
  `Readme.md` REST/MCP tables were refreshed to match the implemented surface.
- **Complete REST ↔ MCP API parity (#87 P2/P3, #96).** Closes the remaining
  parity gaps so an agent has full CRUD + retrieval on both surfaces. Adds
  `GET /v1/chunks/{doc_id}/{chunk_id}` single-chunk fetch (read, workspace-
  scoped), the MCP `get_document` and `list_chunks` metadata/chunk tools
  (read), and the MCP `upload_document` tool — text ingestion (markdown/plain/
  csv/html) sharing the REST upload's validate/dedup/store/enqueue pipeline via
  the new `document_intake` service; binary formats stay REST-only by design.
  `POST /v1/search` already returns a `citation` on every result, so "memory
  search" and "citations" parity needed no new endpoint. Also **fixes #98**:
  MCP `refresh_stale_source` now compensates its pending-reset with a
  mark-failed on MQ publish failure, matching the REST refresh twin (the
  failure-parity contract above is now enforced).

### Defect-register remediation (in progress)

A codescan-driven pass fixing correctness, isolation, and durability defects.

- **Completion contract restored in worker mode (#88)** — the
  `document.processed` / `document.failed` event is now published from inside
  `DocumentIngestionWorkflow` as a final Temporal activity, so fire-and-forget
  workflow starts still notify `core.document.processed.v1` (the switch to
  `trigger_workflow_async` had silently dropped it). The now-dead publish in
  the synchronous trigger path was removed so the contract has one owner.
- **Lineage table ships with migrations (#89)** — new migration
  `014_ingestion_events.sql` creates the `ingestion_events` table that
  `lineage.py` writes and the public API's lineage endpoint reads; previously
  every pipeline step warned with `UndefinedTable` and lineage was never
  recorded. Migration 013 was also amended to create `dead_letter_jobs`
  first — no migration created it either, so 013 failed outright on a fresh
  migration-provisioned database.
- **Idle Redis polls are silent (#90)** — redis-py ≥ 8 raises `TimeoutError`
  when a blocking `XREADGROUP` expires with no messages; the subscriber now
  treats that as the normal empty poll (no error log, no 1s penalty sleep)
  instead of ~20 error lines/minute per idle deployment, and the client is
  created with explicit `socket_timeout` / `health_check_interval` so blocking
  reads can't race the socket timeout.

- **⚠️ BREAKING (data) — collision-free Weaviate naming.** Workspace/user ids
  are now base32-encoded into collection/tenant names instead of stripping
  punctuation, which previously let ids differing only in punctuation
  (`ws-123` / `ws_123` / `ws123`) collapse onto one tenant — a cross-tenant
  leak (#1). Derivation is now injective. **Existing Weaviate collections use
  the old names and must be re-indexed** (drop + re-ingest) to migrate; Postgres
  is unaffected.
- **Auth** — a workspace-scoped API key can no longer be used against a
  different workspace via the `X-Workspace-Id` header, even one its owner also
  owns; the key's binding is authoritative.
- **Durable ingestion** — the `store_in_postgresql`, `store_in_weaviate`, and
  `ensure_tenant_ready` Temporal activities now re-raise on failure so the
  configured `RetryPolicy` actually fires (they previously swallowed errors
  into a success return → no retry, instant dead-letter, and NULL-tenant docs).
- **Poison messages** — a malformed upload event is now dead-lettered and
  ACKed instead of re-raising into an infinite MQ redelivery loop; the worker
  and api trigger are wired with `db_service` so dead-lettering is not a no-op.
- **Rate limiting** — unauthenticated / invalid-key requests (and all traffic
  during a transient auth-DB outage) are now bounded per client IP instead of
  bypassing the limiter; a Redis backend (selected when `REDIS_URL` is set)
  keeps limits correct across autoscaled instances, and the in-memory fallback
  now warns that limits are per-process. New `RATE_LIMIT_UNAUTHENTICATED`.
- **⚠️ BREAKING (deploy) — release Compose hardening.** `docker-compose.release.yml`
  now **refuses to start** unless `POSTGRES_PASSWORD` and `INGESTION_API_KEY`
  are set (no more shipped `postgres` / `dev-ingestion-key` defaults), and all
  backing datastores (Postgres, Mongo, Weaviate, Valkey, S3) publish their
  ports on `127.0.0.1` only. Set both variables (see `.env.example`) before
  `docker compose up`. **Weaviate now runs with API-key auth** (anonymous access
  off): set `WEAVIATE_API_KEY` too — both services authenticate to Weaviate with
  it (Bearer token), and the ports stay loopback-bound as defense-in-depth.

### M0–M2 (merged: #62, #63, #64)
- **Boundary** — agent-memory-substrate ADR + org-readiness plan (#46).
- **Foundation** — one-command `make quickstart`; OSS bootstrap creating the
  workspace in both Postgres + MongoDB; idempotent/non-destructive migrations;
  repo-level `make check`; Compose ingestion→search integration test
  (#3, #16, #5, #19, #15).
- **Durable ingestion** — document lifecycle status (no more 404-while-pending),
  durable upload→ingestion handoff, idempotent reindex + duplicate-chunk fix,
  failure-injection coverage (#7, #6, #60, #11, #31).

### M3 — Content fidelity (#65)
- README/upload/extraction format alignment + pdf/docx fixtures (#9).
- Configurable, model-aware chunking with a documented token budget (#10).
- Extraction & chunking quality evals (#34); coverage reporting added to CI.

### Deferred follow-ups (#66)
- Non-blocking ingestion with backpressure + dead-letter recording (#8, #18).
- PNG upload via OCR (Tesseract, optional extra, graceful fallback) (#61).
- Shared `inh-contracts` package for Weaviate naming + event schemas; both
  Docker build contexts moved to the repo root (#12, #17).

### M4 — Measurable retrieval (#67)
- Measurable hybrid baseline with documented scoring + score provenance (#45).
- Concurrent, bounded, ranking-safe multi-workspace search (#13).
- Golden corpus + ranking regression evals (recall@k/MRR/nDCG) (#33, #35).
- Latency/throughput benchmarks with loose SLO guards (#36).

### M5 — Trust (#68)
- Chunk-level authorization + provenance; **fixed a context-window
  cross-tenant leak** (neighbour chunks now scoped to the requesting user) (#41).
- Auth/tenancy/permission regression suite (#32).
- Freshness-aware memory (`is_stale`) + source refresh endpoint (#42).
- Claim-level citations + lexical `verify_claim` + `/v1/verify-claim` (#39).
- RAG poisoning / prompt-injection risk signals (non-blocking) (#44).
- Adaptive retrieval quality gate + bounded fallback (#43).
- Fix: reconcile Weaviate chunk-property schema on existing collections so
  search doesn't break on upgraded deployments (caught by the live E2E).

### M6 — Agent surface (#69)
- Renamed `src/mcp` → `src/mcp_server` (stopped shadowing the `mcp` SDK).
- MCP permission + feature parity with REST; shared `build_search_request` (#14).
- Memory primitive tools over MCP + REST: `search_memory`, `get_citations`,
  `verify_claim`, `explain_lineage`, `refresh_stale_source`; lineage endpoint (#40).
- REST/MCP contract regression suite (#30).
- Fix: `explain_lineage` reads provenance from the chunk columns (live-caught).

### M7 — Governance + DX (#70)
- Eval result reporting + baseline (#37); per-core-module coverage floors (#38);
  release acceptance matrix + `make release-check` (#29).
- Eval-gated advanced-index **scaffolding** (graph/hierarchy/rerank), off by
  default, no implementation (#47).
- Root pre-commit (#20); normalized dev-tool pins across services (#21);
  documented pytest markers + test profiles (#22); CI caching + step summaries
  (#27); developer-experience / local-setup issue templates (#28).

### Notes
- Every milestone was validated by a live `docker compose` end-to-end run
  (upload → ingest → embed → index → search, plus dedup/status/ranking/benchmark)
  in addition to offline suites.
- MVP-by-intent: heuristic poisoning risk (#44), lexical `verify_claim` (#39),
  and advanced indexes (#47, scaffolding only) are deliberate starting points to
  tighten behind the eval gates.

### Post-merge fixes
- search no longer 500s when a workspace isn't indexed yet — a query that races
  ahead of Weaviate class/tenant creation (or a brand-new/empty workspace) now
  returns empty results instead of an error; the retrieval regression guard was
  calibrated to the real fresh-stack baseline. Fixed the Integration (compose)
  CI workflow on `main`.
- document upload dedup no longer floods search with re-uploaded duplicates
  (#75). Dedup previously keyed only on `(workspace, filename)`, so re-uploading
  identical content under a different name created a new `document_id` with
  duplicate chunks/embeddings that monopolized top-k and pushed out distinct
  documents. Upload now computes `sha256(file_bytes)` and reuses the existing
  `document_id` on a content match (any filename) before falling back to
  filename dedup — verbatim copies collapse onto one document. Adds migration
  `010_document_content_hash.sql` (nullable `content_hash` column + lookup
  index) plus unit coverage and a compose E2E content-flood regression test.

## [0.4.1] — 2026-07-04

Out-of-band repository-level hotfix tag, published ahead of the 0.5.0
org-readiness release above (this entry was backfilled retroactively — the
tag shipped without a changelog entry at the time).

### Fixed

- **Ingestion failed permanently on NUL bytes in extracted text (#84).**
  Postgres `text`/`varchar` columns reject the NUL (0x00) byte, so
  `StagingService.write_text()` raised `ValueError`, the `extract_text`
  activity retried 3x deterministically, and the workflow failed — leaving
  the document stuck with no chunks or embeddings. The quality check already
  flagged this as a `no_binary_content` warning but only at warning severity,
  so the pipeline proceeded with the raw text anyway. The activity now strips
  NUL bytes after the quality check runs (so the diagnostic still sees the
  raw signal) and before `write_text`; a document that is entirely NUL bytes
  still fails the existing empty-text guard. Bumps `inh-ingestion-svc`
  0.4.0 → 0.4.1.
