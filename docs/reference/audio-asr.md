# How audio ASR works (#128)

This page is for senior engineers reviewing optional speech-to-text for
MP3 / WAV / M4A. It covers **processing logic**, **why ASR is optional**,
**why `docker-compose.asr.yml` exists**, and a full **Added / Modified**
component table.

## 1. Scope

| Item | Value |
| --- | --- |
| Formats | `.mp3` → `audio/mpeg`; `.wav` → `audio/wav`; `.m4a` → `audio/mp4` + `audio/x-m4a` |
| Surface | REST only — MCP rejects audio |
| Default behavior | Upload accepted; extract returns placeholder |
| Real STT | Opt-in: Compose overlay or `uv sync --extra asr` |
| Engine | `faster-whisper` (CPU `base` / `int8` by default) |
| Size / duration | Global 50 MB upload cap; `ASR_MAX_DURATION_SECONDS=7200` |

## 2. Processing logic

### Flow

```
POST /v1/documents (audio/*)
        │
        ▼
inh-public-api-svc
  · MIME allow-list + 50 MB size
  · extension / MIME check
  · magic sniff (#117; MP3 skips ID3v2 tag → album-art PNG safe)
  · store object in S3
  · publish document.uploaded on MQ
  · HTTP 201, status=pending
        │
        ▼
inh-ingestion-svc (Temporal document_ingestion)
  · fetch bytes from S3
  · extract_text  (timeout 60 min if audio/*, else 5 min)
  ·   └── EXTRACTORS["audio_asr"] → _extract_audio_text
  · chunk (prose) → embed (TEI) → Postgres + Weaviate
  · status = processed | failed
```

### Step-by-step

| # | Stage | Where | What runs |
| --- | --- | --- | --- |
| 1 | Intake | `inh-public-api-svc` → `document_intake.py` + `inh_contracts.file_types.sniff_content_type` | Validate type/size; sniff bytes; store; enqueue. No Whisper here. |
| 2 | Workflow start | `inh-ingestion-svc` → Temporal `document_ingestion` | Consume MQ event; fetch audio; choose extract timeout via `_extract_timeout_for_content_type`. |
| 3 | Extract | `extract.py` → `_extract_audio_text` | See decision table below. |
| 4 | Index | chunk / store activities | Index transcript or placeholder like any text doc. |

### Extract decision table (`_extract_audio_text`)

| If… | Then… |
| --- | --- |
| `import faster_whisper` fails | Return `[audio: <name>, transcription unavailable]`; doc continues to `processed` |
| Model load soft-fails | Same placeholder |
| Duration > `ASR_MAX_DURATION_SECONDS` (7200) | Raise non-retryable error → document `failed` |
| `MemoryError` | Propagate (never wrap as placeholder) |
| Else | Lazy-load `WhisperModel` once per process; write temp file; PyAV duration probe; `transcribe`; join with `[t=MM:SS]` every ~30s |

## 3. Why optional

| | Default image (OCR) | Opt-in ASR |
| --- | --- | --- |
| Python weight | Small | Large (`faster-whisper`, onnxruntime, …) |
| Model cache | None | ~148 MB (`base`) |
| CPU cost | Seconds | Minutes per file |

Product rule: audio is a first-class REST type for everyone; **real STT is
operator opt-in**. Same degradation shape as PNG OCR (placeholder), different
deploy shape (OCR baked in; ASR not).

## 4. Why `docker-compose.asr.yml`

Optional feature needs an optional **deploy switch**, not a second stack.

```bash
docker compose -f docker-compose.yml -f docker-compose.asr.yml up --build -d
```

Compose merges the overlay onto the base file. **Only `inh-ingestion-svc`
is overridden.**

| Concern | Without overlay | With overlay |
| --- | --- | --- |
| Ingestion Dockerfile | `Dockerfile` (`--extra ocr`) | `Dockerfile.asr` (`--extra ocr --extra asr`) |
| `faster-whisper` present | No | Yes |
| Extract result | Placeholder | Real transcript |
| HF model volume | No | `asr_hf_cache` → `HF_HOME=/var/cache/huggingface` |
| Public API / Postgres / Weaviate / … | Unchanged | Unchanged |

Public API does not need the overlay — it already accepts audio and runs
sniff. Host equivalent of the overlay: `uv sync --extra asr` under
`services/inh-ingestion-svc`.

## 5. Components added or modified

### Added

| Path | Role |
| --- | --- |
| `docker-compose.asr.yml` | Opt-in Compose overlay (ASR image + env + HF cache) |
| `services/inh-ingestion-svc/Dockerfile.asr` | Ingestion image with OCR + `asr` extra |
| `services/inh-ingestion-svc/tests/test_audio_asr.py` | ASR unit tests (placeholder, markers, duration, timeout) |
| `docs/reference/audio-asr.md` | This how-it-works page |
| `docs/superpowers/plans/2026-08-12-audio-asr-optional-extra.md` | Sprint / implementation plan |

### Modified

| Path | Role |
| --- | --- |
| `services/inh-contracts/src/inh_contracts/file_types.py` | Register mp3/wav/m4a; `_id3v2_payload_offset`; ID3-aware cross-format sniff |
| `services/inh-contracts/tests/test_file_types.py` | Registry + ID3/APIC accept / PNG reject tests |
| `services/inh-ingestion-svc/src/temporal/activities/extract.py` | Wire `audio_asr`; `_extract_audio_text`; Whisper singleton; markers; duration guard |
| `services/inh-ingestion-svc/src/temporal/workflows/document_ingestion.py` | `_extract_timeout_for_content_type` — 60 min for `audio/*` |
| `services/inh-ingestion-svc/src/config/settings.py` | `ASR_MODEL_SIZE`, `ASR_DEVICE`, `ASR_COMPUTE_TYPE`, `ASR_MAX_DURATION_SECONDS` |
| `services/inh-ingestion-svc/pyproject.toml` | `[project.optional-dependencies] asr` |
| `services/inh-ingestion-svc/uv.lock` | Lock entries for `faster-whisper` stack |
| `services/inh-ingestion-svc/tests/test_temporal_activities.py` | Extractor / placeholder pins |
| `services/inh-ingestion-svc/Readme.md` | Enable ASR notes |
| `services/inh-public-api-svc/tests/unit/test_document_intake.py` | Audio inherits 50 MB cap |
| `services/inh-public-api-svc/tests/contract/test_mcp_contract.py` | MCP rejects audio MIME types |
| `services/inh-public-api-svc/tests/unit/test_docs_sync.py` | Docs sync pins |
| `docs/reference/file-types.md` | Generated table + extras prose |
| `docs/reference/configuration.md` | ASR env table |
| `docs/getting-started/local.md` | Optional ASR enable section |
| `docs/examples/README.md` | Audio upload example |
| `docs/index.md` / `README.md` / `CHANGELOG.md` | Surface + Unreleased entry |
| `scripts/generate_supported_formats.py` | UTF-8 / newline stability for table regen |
| `mkdocs.yml` | Nav entry for this page (when linked) |

### Intentionally unchanged

| Path | Why |
| --- | --- |
| `services/inh-ingestion-svc/Dockerfile` | Default image stays lean (OCR only, no Whisper) |
| `services/inh-public-api-svc/src/services/document_intake.py` | Generic sniff path; ASR logic lives in contracts + ingestion extract |

## 6. Related links

- [Local enable / air-gap](../getting-started/local.md#optional-asr-speech-to-text-for-audio-uploads)
- [Configuration — Optional ASR](configuration.md#optional-asr-128)
- [Supported file types](file-types.md)
