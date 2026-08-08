# Supported file types

Every format Inherent accepts is declared once, as a `FileTypeSpec` entry in
[`FILE_TYPE_REGISTRY`](https://github.com/inherent-prime/inherent/blob/main/services/inh-contracts/src/inh_contracts/file_types.py)
(`services/inh-contracts`, shared by both services). REST upload validation,
the MCP `upload_document` tool's text-type allow-list, ingestion's extraction
dispatch, and the table below all derive from that one registry — none of
them maintain their own copy, so they cannot disagree about what "supported"
means (#117).

## Table

The table is generated from the registry, not hand-maintained. Regenerate it
after changing `FILE_TYPE_REGISTRY`:

```bash
uv run --project services/inh-contracts python scripts/generate_supported_formats.py
```

A CI test (`services/inh-public-api-svc/tests/unit/test_docs_sync.py`) fails
the build if this table and the registry ever disagree.

<!-- BEGIN GENERATED FILE TYPES TABLE (#117; run scripts/generate_supported_formats.py to refresh) -->

| Type | Extension(s) | MIME type(s) | Surfaces | Chunking hint | Extra required |
| --- | --- | --- | --- | --- | --- |
| txt | `.txt` | `text/plain` | rest + mcp | prose | — |
| markdown | `.md`, `.markdown` | `text/markdown` | rest + mcp | prose | — |
| csv | `.csv` | `text/csv` | rest + mcp | tabular | — |
| html | `.html`, `.htm` | `text/html` | rest + mcp | prose | — |
| pdf | `.pdf` | `application/pdf` | rest | prose | — |
| json | `.json` | `application/json` | rest | structured | — |
| docx | `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | rest | prose | — |
| xlsx | `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | rest | tabular | — |
| pptx | `.pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | rest | structured | — |
| png | `.png` | `image/png` | rest | media | `ocr` |
| eml | `.eml` | `message/rfc822` | rest | prose | — |
| epub | `.epub` | `application/epub+zip` | rest | prose | — |
| rtf | `.rtf` | `application/rtf`, `text/rtf` | rest | prose | — |
| odt | `.odt` | `application/vnd.oasis.opendocument.text` | rest | prose | — |
| yaml | `.yaml`, `.yml` | `application/yaml`, `text/yaml` | rest + mcp | structured | — |
| toml | `.toml` | `application/toml` | rest + mcp | structured | — |
| xml | `.xml` | `application/xml`, `text/xml` | rest + mcp | structured | — |
| code | `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.go`, `.java`, `.rs`, `.c`, `.h`, `.cpp`, `.cs`, `.rb`, `.php`, `.swift`, `.kt`, `.scala`, `.sh`, `.sql`, `.r`, `.lua` | `text/x-python`, `application/javascript`, `text/javascript`, `application/typescript`, `text/x-go`, `text/x-java-source`, `text/x-rustsrc`, `text/x-csrc`, `text/x-chdr`, `text/x-c++src`, `text/x-csharp`, `text/x-ruby`, `text/x-php`, `text/x-swift`, `text/x-kotlin`, `text/x-scala`, `application/x-sh`, `text/x-sh`, `application/sql`, `text/x-sql`, `text/x-r-source`, `text/x-lua` | rest + mcp | code | — |
| srt | `.srt` | `application/x-subrip` | rest + mcp | prose | — |
| vtt | `.vtt` | `text/vtt` | rest + mcp | prose | — |

<!-- END GENERATED FILE TYPES TABLE -->

**Surfaces**: `rest` = `POST /v1/documents` accepts it. `rest + mcp` =
also accepted by the MCP `upload_document` tool (text-only by design — see
[MCP tools](mcp-tools.md)). Every binary format is REST-only: MCP tool
arguments are JSON strings, so raw bytes cannot cross that boundary.

**Chunking hint**: the strategy family the format-aware chunker uses for this
type (`prose`, `tabular`, `structured`, `media`).

**Extra required**: a pyproject optional-dependency group that must be
installed for extraction to produce real text instead of degrading. Today
only `image/png` has one (`ocr` — pytesseract + Pillow, plus the `tesseract`
system binary): without it, OCR falls back to a placeholder string instead of
failing the upload.

## Validation at upload

An upload carries three independent signals: the declared `Content-Type`,
the filename, and the actual bytes. Beyond the MIME allow-list, #117 added
two cross-checks so a disagreement between any pair of these is caught
before anything is stored:

- **Bytes vs. declared type.** Binary formats are magic-byte sniffed against
  the declared `Content-Type`: the uploaded bytes must start with that
  format's real file signature, or the upload is rejected with
  `400 Bad Request`. Catches e.g. PNG bytes declared as `text/plain`, or a
  file declared `application/pdf` whose bytes aren't actually a PDF. Text
  formats have no binary signature of their own to check directly, but are
  still caught if their bytes match a *different* format's known signature.
- **Filename vs. declared type.** A filename extension the registry
  recognizes (e.g. `.pdf`) must belong to the SAME type as the declared
  `Content-Type`, or the upload is rejected. Catches e.g. a file named
  `report.pdf` uploaded as `text/plain`, even when the bytes ARE valid plain
  text (so the byte-level sniff above has nothing to object to). An
  extension the registry does not recognize (or no extension at all) is not
  treated as evidence of anything — `Content-Type` stays authoritative for
  formats #117 doesn't cover yet.

Between the two checks, any pairwise disagreement among {declared type,
filename, bytes} is caught by at least one of them.

A content type reaching the ingestion extractor with no registry entry (or a
registry entry whose extractor isn't wired up) fails the document with a
clear `error_message` — there is no default "decode it as text and hope"
fallback. A format whose extraction needs an optional dependency that isn't
installed (currently only PNG OCR, via the `ocr` extra) degrades to a
placeholder instead of failing, per that format's `degradation` setting.

## Adding a new format

Landing support for a new type ([#118](https://github.com/inherent-prime/inherent/issues/118)
XLSX, [#119](https://github.com/inherent-prime/inherent/issues/119) PPTX, ...
— see the file-type backlog tracked from
[#117](https://github.com/inherent-prime/inherent/issues/117)) is:

1. One `FileTypeSpec` entry in `services/inh-contracts/src/inh_contracts/file_types.py`.
2. One extraction function + `EXTRACTORS` entry in
   `services/inh-ingestion-svc/src/temporal/activities/extract.py`.
3. Run `scripts/generate_supported_formats.py` to refresh this page.

REST validation, MCP exposure (if the type's `surfaces` include `mcp`),
extraction dispatch, and this doc all pick it up with no other edits.
