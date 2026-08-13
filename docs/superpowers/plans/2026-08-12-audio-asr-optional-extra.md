# Implementation plan: Audio ASR (#128)

**Issue:** [#128 — Audio — speech-to-text behind an optional asr extra](https://github.com/inherent-prime/inherent/issues/128)  
**Status:** Sprint 0–4 complete — #128 implementable end-to-end (close via PR)  
**Depends on:** #117 (done), #127 SRT/VTT timestamp contract (done)  
**Deploy choice:** **Optional `asr` extra** (opt-in) — not baked into the default ingestion image

---

## Problem (plain English)

Inherent can ingest text transcripts (SRT/WebVTT) but not raw meeting
recordings or voice notes (mp3 / wav / m4a). Users must transcribe outside
the product first.

#128 adds speech-to-text the same way PNG OCR works: an optional Python
extra, graceful placeholder when the extra is missing, REST-only upload,
and cost guards so long audio cannot starve the worker.

---

## Contract

| Item | Value |
| --- | --- |
| MIME / extensions | `audio/mpeg` (`.mp3`), `audio/wav` (`.wav`), `audio/mp4` + `audio/x-m4a` (`.m4a`) |
| Optional extra | `asr` → `faster-whisper` |
| Missing extra / model | Placeholder `[audio: <name>, transcription unavailable]` — **not** hard fail |
| Degradation | `placeholder` |
| Surface | REST-only (not MCP) |
| Timestamps | Coarse `[t=MM:SS]` markers (same shape as #127) |
| Duration cap | 7200 seconds (2 hours) |
| Size cap | **Inherit global 50 MiB** (`MAX_UPLOAD_SIZE_BYTES`); do **not** set `max_size_bytes=100 MiB` (that would *raise* the upload limit). Use `max_size_bytes=None` or explicit `50 * 1024 * 1024` for clarity. |
| Default model | **`base`**, CPU, `int8` |

---

## Deploy decision: optional `asr` extra (opt-in)

ASR is heavier than OCR (`faster-whisper` + ~148 MB `base` weights + long
CPU jobs). Do **not** install it in the default ingestion image.

| Mode | Behavior |
| --- | --- |
| **Default Compose / image** | No `asr` extra. Audio uploads are accepted (once registered) and extraction returns the **placeholder**. |
| **Opt-in** | Operator enables ASR (Compose profile / overlay / `uv sync --extra asr`), ensures Hugging Face model cache is available. Real transcription runs. System `ffmpeg` optional (PyAV decodes wav/mp3). |

### What gets installed where

| Layer | What | Notes |
| --- | --- | --- |
| Python extra | `faster-whisper` | Only on machines that enable ASR |
| Audio decode | PyAV (`av`, pulled by `faster-whisper`) | **System `ffmpeg` not required** for wav/mp3 in the Sprint 0 spike; still document as optional ops aid |
| Model weights | Whisper `base` (default) | ~148 MB on disk; downloaded on first use into HF cache, or pre-cached on the VM |

You do **not** manually download a Faster Whisper zip. You install the
optional extra; weights download (or are pre-baked into a cache volume)
on the host that runs ingestion.

### Default image vs opt-in (why opt-in)

| | Default image (rejected for #128) | Opt-in (chosen) |
| --- | --- | --- |
| Fresh `compose up` | Real STT | Placeholder only |
| Image size / build | Higher | Default stays lean |
| Fits “optional extra” | Extra always present | Extra truly optional |
| Ops story | Simpler, heavier for everyone | Document how to enable |

OCR stays baked (small). ASR stays opt-in (heavy).

---

## Model choice

Stack is CPU-first. Ship for CPU, not GPU demos.

| Model | Role |
| --- | --- |
| **`base` (default)** | Best CPU cost/quality for OSS + typical VM. Multilingual. |
| `base.en` | Env override for English-only fleets |
| `small` | Prod quality knob; slower on CPU |
| `tiny` | CI / smoke only |
| `medium` / `large-v3` | Out of v1 (needs GPU) |

**Shipped defaults:**

```text
ASR_MODEL_SIZE=base
ASR_DEVICE=cpu
ASR_COMPUTE_TYPE=int8
ASR_MAX_DURATION_SECONDS=7200
```

Lazy-load a **process-level singleton** `WhisperModel` so every file does
not re-download or re-load weights.

---

## Acceptance criteria (from #128)

- [ ] Registry entries per #117
- [ ] Extraction tests skip/mock when `asr` extra is absent (pattern: `test_image_ocr.py`)
- [ ] Placeholder degradation test-pinned when the extra is missing
- [ ] Duration/size cap exceeded → document `failed` with actionable error
- [ ] Docs (extras section alongside OCR) and CHANGELOG updated
- [ ] MCP does not accept audio; REST does

---

## Sprint plan

Tests first every sprint. DoD = tests green; docs/CHANGELOG when behavior
ships. Pattern: mirror OCR (`optional_extra`, `degradation=placeholder`).

### Sprint 0 — Decisions & spike — **DONE** (2026-08-12)

**Goal:** Lock numbers so coding does not thrash.

See [Sprint 0 report](#sprint-0-report) below for evidence.

**Locked decisions**

1. MIME set unchanged: `audio/mpeg`, `audio/wav`, `audio/mp4`, `audio/x-m4a`.
2. Duration cap **7200s**. Size: **inherit 50 MiB global** (not 100 MiB).
3. Model default **`base`** / `cpu` / `int8`. Opt-in deploy confirmed.
4. Decode via **PyAV**; system ffmpeg optional. Mount HF cache when ASR enabled.

**Exit:** Met. Ready for Sprint 1.

---

### Sprint 1 — Registry + intake (no real STT yet) — **DONE** (2026-08-12)

**Goal:** Audio is a first-class REST type; size gate works; MCP rejects.

**Done**

1. `FileTypeSpec`s for `mp3` / `wav` / `m4a` — REST-only, `optional_extra="asr"`,
   `degradation="placeholder"`, `chunking_hint="prose"`, `extractor="audio_asr"`,
   `max_size_bytes=None` (inherit 50 MiB).
2. Magic: wav=`RIFF` (window 12), m4a=`ftyp` (window 16), mp3=`None`.
3. Stub `_extract_audio_text` → `[audio: <name>, transcription unavailable]`.
4. MCP rejects all four MIME aliases; oversized audio → 400 with 50 MB message.
5. Docs table regenerated; examples 400 string + file-types extras prose updated;
   CHANGELOG Unreleased entry.

**Exit:** Met. Ready for Sprint 2 (`asr` extra + real placeholder-on-ImportError).

---

### Sprint 2 — Placeholder degradation + `asr` extra wiring — **DONE** (2026-08-12)

**Goal:** OCR twin for “extra missing.”

**Done**

1. `test_audio_asr.py` — ImportError / model-load fail / empty / transcribe
   error → placeholder; mocked success returns transcript; `MemoryError`
   propagates.
2. `[project.optional-dependencies] asr = ["faster-whisper>=1.1.0,<2"]` +
   lock update (default `uv sync` does **not** install it).
3. `_extract_audio_text` try/import + lazy singleton + temp-file transcribe;
   placeholder on soft failures.
4. Docs: README, examples upload section, CHANGELOG.

**Exit:** Met. Ready for Sprint 3 (markers, duration/size caps, timeouts, settings).

---

### Sprint 3 — Transcription + timestamps + cost guards — **DONE** (2026-08-12)

**Goal:** Core AC of #128.

**Done**

1. Settings: `ASR_MODEL_SIZE` / `ASR_DEVICE` / `ASR_COMPUTE_TYPE` /
   `ASR_MAX_DURATION_SECONDS` (defaults `base` / `cpu` / `int8` / `7200`).
2. Time-based `[t=MM:SS]` markers every 30s of audio (`_format_asr_transcript`).
3. Duration over cap → `ApplicationError(AudioDurationExceeded, non_retryable)`
   via PyAV probe (early) or `TranscriptionInfo.duration`.
4. `extract_text` timeout: 60 min for `audio/*`, 5 min otherwise
   (`_extract_timeout_for_content_type`).
5. Tests in `test_audio_asr.py` (13 passed); config reference + CHANGELOG.

**Exit:** Met. Ready for Sprint 4 (Compose overlay / HF cache docs / close #128).

---

### Sprint 4 — Opt-in deploy path, docs, close — **DONE** (2026-08-12)

**Goal:** Operable on laptop + VM without guessing.

**Done**

1. `services/inh-ingestion-svc/Dockerfile.asr` — OCR + `--extra asr`; default
   `Dockerfile` unchanged.
2. `docker-compose.asr.yml` overlay — rebuilds ingestion, mounts
   `asr_hf_cache` at `HF_HOME=/var/cache/huggingface`, passes ASR env defaults.
3. Docs: `docs/getting-started/local.md` (Optional ASR), configuration.md,
   ingestion Readme, examples README, CHANGELOG.
4. Adversarial checklist (below) reviewed.

**Close #128:** include `Closes #128` on the merge PR (not closed from this
plan alone).

**Exit:** Met. Sprint 5 remains optional follow-ups.


---

### Sprint 5 (optional follow-ups — separate issues)

- Short spoken eval fixture → retrieval finds a distinctive phrase.
- Runbook for `ASR_MODEL_SIZE=small` on larger VMs.
- GPU path (`cuda` + `float16`) behind env — not default.
- Concurrency limit / semaphore (ASR is heavy).
- `ASR_LANGUAGE=en` to speed CPU.

---

## Sprint map

```text
S0  Decide model/caps/opt-in   →  DONE — base + int8 + 2h + optional extra
S1  Registry + REST/MCP        →  DONE — types accepted/rejected, size gate
S2  asr extra + placeholder    →  DONE — OCR twin, docs extras
S3  Transcribe + markers+caps  →  DONE — STT, timeout, non-retryable guards
S4  Opt-in deploy + CHANGELOG  →  DONE — overlay + HF cache docs
S5  Optional harden/eval       →  small model, GPU, concurrency
```

---

## Sprint 0 report

**Date:** 2026-08-12  
**Environment:** Windows host, CPython 3.12.13 via `uv`, no system `ffmpeg` on PATH  
**Package:** `faster-whisper==1.2.1` + `av==18.0.0` (PyAV)  
**Fixture:** 20 s mono 16 kHz PCM WAV (synthetic tone — measures pipeline cost, not quality); plus a 3 s MP3 written via PyAV

### 1. MIME + caps vs codebase

| Decision | Evidence | Lock |
| --- | --- | --- |
| MIME set | Issue #128 contract | `audio/mpeg`, `audio/wav`, `audio/mp4`, `audio/x-m4a` |
| Global upload cap | `MAX_UPLOAD_SIZE_BYTES = 50 * 1024 * 1024` in `inh-public-api-svc` constants | **50 MiB** |
| Plan’s old “~100 MiB” | Would *raise* the global cap via `spec.max_size_bytes` | **Rejected** — use `None` / inherit 50 MiB |
| Duration | Issue example | **7200 s** |
| Why size alone is not enough | At ~128 kbps, 50 MiB ≈ ~50 min audio; at lower bitrates duration can hit 2 h first | Keep **both** guards |

No registry entry today sets `max_size_bytes` — audio should follow that pattern (`None`).

### 2. CPU spike timings (`device=cpu`, `compute_type=int8`)

Cold load includes first Hugging Face download. Warm load is post-cache.

| Model | Cold load (s) | Transcribe 20 s tone (s) | RTF (tx/audio) | On-disk cache |
| --- | --- | --- | --- | --- |
| `tiny` | 22.66 | 0.65 | 0.032 | ~78 MB |
| **`base`** | 28.85 | 1.29 | 0.065 | **~148 MB** |
| `small` | 72.45 | 3.34 | 0.167 | ~486 MB |

| Check | Result |
| --- | --- |
| Warm load `base` (cached) | **1.77 s** → singleton load is mandatory |
| MP3 without system ffmpeg | **OK** (PyAV encode + transcribe) |
| System `ffmpeg` on PATH | **Absent** — not required for wav/mp3 in this spike |

**Extrapolations (optimistic — tone audio, not real speech):**

- 2 h @ `base` RTF 0.065 ≈ **~8 min** CPU transcribe (plus load)
- 2 h @ `small` RTF 0.167 ≈ **~20 min**

Real speech is typically slower; treat these as lower bounds. Confirms:

- Keep shipped default **`base`**
- Raise audio `extract_text` timeout to **45–60 min** (today’s **5 min** would fail long files)
- Process-level **singleton** `WhisperModel` (warm load ~2 s vs cold ~30 s + download)

### 3. Opt-in deploy path

| Finding | Detail |
| --- | --- |
| Default Dockerfile | Bakes `--extra ocr` + `tesseract` only — **leave unchanged** |
| Compose profiles | **None** in `docker-compose.yml` today |
| Recommended Sprint 4 shape | `docker-compose.asr.yml` overlay **or** first `profiles: [asr]` service variant; mount HF cache volume; build with `--extra asr` |
| Non-Docker VM | `uv sync --extra asr` in ingestion venv + HF cache dir |

Default stack → placeholder. Opt-in → real STT.

### 4. Ops note (draft for Sprint 4 docs)

```text
# Enable ASR on a host that runs inh-ingestion-svc
uv sync --extra asr          # installs faster-whisper (+ av/PyAV)
# optional: apt/choco install ffmpeg  # not required for wav/mp3 in spike

# Model cache (first run downloads ~148 MB for base)
# Linux/macOS: ~/.cache/huggingface
# Windows: %USERPROFILE%\.cache\huggingface
# Compose: mount that path (or a dedicated volume) into the ingestion container

ASR_MODEL_SIZE=base
ASR_DEVICE=cpu
ASR_COMPUTE_TYPE=int8
ASR_MAX_DURATION_SECONDS=7200
```

Air-gapped VM: pre-seed the HF cache with `Systran/faster-whisper-base` (or run one online warmup and copy the cache).

### Sprint 0 exit checklist

- [x] MIME set confirmed
- [x] Size/duration caps locked (50 MiB inherit + 7200 s)
- [x] Model locked (`base` / cpu / int8) with timing evidence
- [x] ffmpeg need clarified (PyAV sufficient for wav/mp3)
- [x] Opt-in path confirmed (no default bake; Compose profile/overlay TBD in S4)
- [x] Plan file updated

**Next:** Sprint 1 — registry + intake tests.

---

## Key code touchpoints

| Area | Location |
| --- | --- |
| Registry | `services/inh-contracts/src/inh_contracts/file_types.py` |
| Extractor | `services/inh-ingestion-svc/src/temporal/activities/extract.py` |
| OCR pattern to mirror | `_extract_image_text`, `tests/test_image_ocr.py` |
| Timestamp helpers | `_timestamp_to_marker`, `_TIMESTAMP_MARKER_EVERY_N_CUES` (#127) |
| Intake size override | `document_intake` already reads `spec.max_size_bytes` |
| Activity timeout | `document_ingestion.py` `extract_text` (~5 min today) |
| Optional deps | `services/inh-ingestion-svc/pyproject.toml` |
| Default image (leave alone) | `services/inh-ingestion-svc/Dockerfile` — keep OCR-only extras; no `asr` bake |

---

## Defect-prevention checklist (before merge)

- [x] Pattern sweep: OCR-extras docs also mention ASR (README, examples,
      file-types, local.md, configuration.md, ingestion Readme)
- [x] Dual-surface: REST accept + MCP reject pinned (Sprint 1 tests)
- [x] Deterministic duration/size failures are `non_retryable=True` (Sprint 3)
- [x] `MemoryError` not wrapped into non-retryable ApplicationError / placeholder
- [x] Placeholder path never leaves document state contradicting success
- [x] Default image does **not** ship `asr` / Whisper weights (overlay only)
- [x] Adversarial review: 60 min audio timeout avoids starvation of the
      default 5 min path; duration errors do not burn retry budget

---

## Definition of done

- [x] All new tests green; ASR tests mock/skip when extra absent
- [x] Placeholder pinned without the extra
- [x] Over-cap → failed + actionable error
- [x] Docs + CHANGELOG updated; opt-in enable path documented
- [x] Default image does **not** ship `asr` / Whisper weights
- [ ] #128 closable — use `Closes #128` on the merge PR
