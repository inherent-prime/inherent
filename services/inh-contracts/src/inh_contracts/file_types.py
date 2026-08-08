"""File-type support registry -- the single contract for validation, sniffing,
extraction dispatch, and docs (#117).

Problem this replaces
----------------------
Before this module, "what file types does Inherent accept" was answered
independently in five places that had to be edited in lockstep, with nothing
enforcing agreement between them:

- ``services/inh-public-api-svc/src/config/constants.py`` -- ``ALLOWED_MIME_TYPES``
- ``services/inh-public-api-svc/src/mcp_server/server.py`` -- ``SUPPORTED_TEXT_MIME_TYPES``,
  derived from ``ALLOWED_MIME_TYPES`` by a ``.startswith("text/")`` string guess
- ``services/inh-ingestion-svc/src/temporal/activities/extract.py`` -- the
  ``_extract_text_inner`` content-type if/elif dispatch
- ``docs/index.md``, ``docs/reference/mcp-tools.md``,
  ``docs/reference/configuration.md``, ``docs/examples/README.md``
- ``tests/test_extraction_by_type.py``, ``tests/unit/test_upload_document.py``

Same drift class as #9 (README/validation/extraction disagreement) and the
same registry lesson as #100 (the MCP ``_TOOLS`` registry).

Both services now import ``FILE_TYPE_REGISTRY`` (or the derived helpers
below) instead of hardcoding their own list. Docs are generated from /
verified against it (``scripts/generate_supported_formats.py`` +
``services/inh-public-api-svc/tests/unit/test_docs_sync.py``). A content
type reaching ingestion with no matching entry hard-fails the document
instead of silently ``decode(errors="ignore")``-ing it.

Two contract holes this closes (see the #117 issue body):

1. MIME type is entirely client-supplied and was never checked against the
   actual bytes -- a mislabeled binary (PNG uploaded as ``text/plain``)
   passed validation and was garbled downstream. See ``sniff_content_type``.
2. A type accepted at upload but missing from the extraction dispatch fell
   through to a lossy default decode instead of a hard failure. There is no
   default branch anymore: a content type with no registry entry (or a
   registry entry with no wired extractor) is itself the signal to fail the
   document with an actionable ``error_message``.

This module is intentionally dependency-light (stdlib only): both services
depend on it, and it must never force either one to install extraction
libraries (pypdf, python-docx, pytesseract, ...) it doesn't otherwise need.
The ``extractor`` field is therefore a string dispatch KEY, not an import --
the actual function lives in ``inh-ingestion-svc``'s extraction module and is
looked up by that key (see ``EXTRACTORS`` there, and
``services/inh-ingestion-svc/tests/test_temporal_activities.py::
TestFileTypeRegistryDispatch::test_every_registry_extractor_key_is_wired``,
which pins that every key here is wired).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

# Strategy family #129's format-aware chunker will branch on. A closed Literal
# (not a free string) so a typo can't silently mint a new, unhandled hint --
# it fails at the FileTypeSpec call site instead of at chunk time.
# "code" added by #122: source files are neither prose nor tabular/structured
# data -- a future chunker wants to split on function/class boundaries, not
# sentence boundaries, so it needs its own hint rather than overloading
# "structured" (configs/JSON) or "prose" (docs). Deliberate, matching the
# Literal's own purpose (a typo mints a *rejected* value, not a silently new
# one) -- not consumed by any chunker yet (#129 is still pending), so adding
# it now cannot change current behavior.
ChunkingHint = Literal["prose", "tabular", "structured", "media", "code"]

# Which upload surface(s) accept a type. "mcp" additionally exposes it through
# the MCP `upload_document` tool, which only transports inline UTF-8 TEXT (#87
# Task 3 -- MCP tool arguments are JSON strings, so binary bytes cannot cross
# that boundary at all). Binary formats are REST-only by construction, not by
# policy choice.
Surface = Literal["rest", "mcp"]

# What happens when `optional_extra` is required but not installed.
# - "hard_fail": extraction raises; the document is marked failed.
# - "placeholder": extraction returns a placeholder string so the document
#   still flows through the pipeline (0 useful chunks, but not a hard
#   failure) -- mirrors image/png's existing OCR-unavailable fallback.
# Only meaningful when `optional_extra` is set: with no optional dependency,
# a missing REQUIRED library is always a hard failure regardless of this
# field's value.
Degradation = Literal["hard_fail", "placeholder"]


@dataclass(frozen=True)
class FileTypeSpec:
    """Everything one file type needs to fully describe itself (#117).

    Every field answers a question some now-deleted piece of scattered code
    used to answer independently:

    ============================ ===========================================
    Field                        Replaces
    ============================ ===========================================
    ``mime_types``                ``ALLOWED_MIME_TYPES`` membership check
    ``surfaces``                  ``SUPPORTED_TEXT_MIME_TYPES`` string guess
    ``extractor``                 the ``extract.py`` if/elif dispatch
    ``magic``                     nothing -- the verification hole #117 closes
    ``chunking_hint``              input to #129's format-aware chunker
    ``optional_extra``/``degradation``  the ad hoc OCR-availability try/except
    ``max_size_bytes``            per-format override of the global cap
    ============================ ===========================================
    """

    # Short, stable identifier. Used as the extraction dispatch key and in
    # error messages/logs -- an internal name, never shown as the "type" to
    # an end user (that's `mime_types[0]`).
    key: str

    # Canonical MIME type first, any accepted aliases after. All are valid
    # values for the upload's declared Content-Type.
    mime_types: tuple[str, ...]

    # Filename extensions (with leading dot), e.g. (".md", ".markdown").
    # Consulted as a fallback classifier for a generic/absent content-type
    # (e.g. "application/octet-stream" or no Content-Type at all) by
    # `get_spec_for_upload` (#122) -- completing the design this field was
    # originally reserved for (see the #117 PR). A SPECIFIC-but-unregistered
    # declared MIME type is deliberately NOT widened by this: the fallback
    # only fires when the content type itself carries no real information,
    # so an over-broad extension list cannot turn every unknown text file
    # into an accepted upload (see `get_spec_for_upload`'s docstring). Also
    # the substrate for a future extension-based consumer (e.g. #130's ZIP
    # member classification) to share instead of re-deriving its own list.
    extensions: tuple[str, ...]

    # Magic-byte signature checked at intake (see `sniff_content_type`).
    # None for every current text format -- there is no binary signature to
    # check for free-form text, so the sniff for those relies entirely on the
    # cross-check against OTHER specs' signatures (a text/plain upload whose
    # bytes match a known binary signature is still caught).
    magic: bytes | None

    # Which upload surface(s) accept this type.
    surfaces: frozenset[Surface]

    # Dispatch key into inh-ingestion-svc's EXTRACTORS map
    # (src/temporal/activities/extract.py). A string, not a callable/import,
    # so this package stays free of extraction-library dependencies.
    extractor: str

    # Strategy family #129's format-aware chunker will branch on.
    chunking_hint: ChunkingHint

    # pyproject optional-dependency group gating a heavy/optional extractor
    # library (existing pattern: "ocr" for pytesseract+pillow). None means
    # the extractor's dependencies are part of the service's core install.
    optional_extra: str | None = None

    # Behavior when `optional_extra` is required but missing. See the
    # `Degradation` docstring above.
    degradation: Degradation = "hard_fail"

    # Per-format upload size cap override, in bytes. None means "use the
    # service's global MAX_UPLOAD_SIZE_BYTES default".
    max_size_bytes: int | None = None

    # Overrides the default `_SNIFF_WINDOW` (1024 bytes) tolerance for THIS
    # spec's OWN `magic` signature only. None (the default) means "use the
    # standard tolerant window" -- correct for a real binary container
    # format like PDF, which legitimately has leading junk (BOM, blank
    # lines) before its signature in real-world files (see
    # `sniff_content_type`'s docstring). Set this to a SMALL value only for
    # a format whose real files NEVER have anything before the signature --
    # otherwise a substring match anywhere in a large window can
    # false-positive on ordinary PROSE that happens to mention the magic
    # bytes (#126 review item 5: RTF's `{\rtf` is exactly this -- unlike
    # `%PDF-`, it is plausible English prose, e.g. a sentence explaining
    # RTF's own file format, so a 1024-byte substring search rejects
    # legitimate text/markdown/html/eml uploads that merely discuss RTF).
    magic_anchor_window: int | None = None

    # Whether the extension-mismatch check (`check_extension_consistency`)
    # should be SKIPPED for this format, even though it has a `magic`
    # signature (used for sniffing above). Set for a format whose bytes are
    # genuinely ASCII/text -- RTF is a control-word TEXT format, not a
    # binary container, so it is plausibly declared under a generic text/*
    # Content-Type by a real client, exactly the same "text/plain is a
    # truthful, IANA-valid Content-Type for X" argument
    # `check_extension_consistency`'s own docstring already makes for
    # .txt/.md/.csv/.html (#126 review item 6). False (the default) for
    # every genuinely BINARY container format (docx/epub/odt/png/pdf/...),
    # where a mismatched extension IS still a real, actionable
    # contradiction that must stay caught.
    extension_check_exempt: bool = False

    # Per-extension override resolving to a SPECIFIC canonical MIME type,
    # needed only by a spec whose `mime_types` pools MULTIPLE distinct
    # sub-formats under one registry entry (#197; currently only "code" --
    # #122's 21 extensions sharing one extractor). `mime_types[0]` is a fine
    # stand-in for "the" MIME type when a spec describes exactly ONE format
    # (every other current entry) -- it is NOT a stand-in for a SPECIFIC
    # extension when `mime_types` is actually a pool of aliases spanning many
    # distinct languages: before this field existed, every code file
    # uploaded over MCP with `content_type` omitted resolved to
    # `mime_types[0]` == "text/x-python" regardless of whether it was a .go,
    # .java, or .sql file (verified in #197: app.js, lib.go, Main.java,
    # q.sql, s.sh, x.rs all mislabelled identically). None (the default,
    # every spec but "code") means "mime_types[0] IS the answer for every
    # extension this spec declares" -- see `mime_type_for_extension` below,
    # the one place this field is consulted.
    #
    # A tuple of (extension, mime) pairs -- NOT a `dict` (review follow-up:
    # a plain `dict` field breaks this `@dataclass(frozen=True)`'s own
    # contract two ways at once: `hash(spec)` / `set(FILE_TYPE_REGISTRY)`
    # raise `TypeError: unhashable type: 'dict'` the moment anything hashes
    # a spec, and `spec.mime_type_by_extension['.py'] = 'x'` mutates the
    # process-global registry despite `frozen=True`, since freezing a
    # dataclass only blocks reassigning its OWN attributes, not mutating a
    # mutable object one of them points to. `MappingProxyType` fixes the
    # second problem but not the first -- a proxy of a dict is itself
    # unhashable. A tuple of pairs is both immutable and hashable, matching
    # every other field on this class -- see `mime_type_for_extension`'s
    # linear scan, the only place this is read). Keys are lowercased
    # extensions WITH the leading dot (e.g. ".go"), matching `extensions`
    # above.
    mime_type_by_extension: tuple[tuple[str, str], ...] | None = None


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
# One entry per currently-supported format. The FIRST EIGHT entries match
# the pre-#117 constants.py ALLOWED_MIME_TYPES ordering exactly (plain text,
# Markdown, CSV, HTML, PDF, JSON, DOCX, PNG), so the 400 error text for those
# eight is byte-for-byte unchanged by this migration -- entries after PNG are
# formats #117 itself did not cover (#118+), appended in whatever order their
# own issues landed; nothing beyond "the first eight" was ever a promise.
# (docs/index.md's prose list happened to state JSON before PDF -- a
# pre-existing, harmless inconsistency between two human-readable listings
# that predates #117; this registry follows the CODE's order, the one that
# actually appears in a response body.)
#
# Adding a NEW format (the whole point of #117) is:
#   1. One FileTypeSpec entry below.
#   2. One function + EXTRACTORS[key] entry in inh-ingestion-svc's extract.py.
#   3. `uv run python scripts/generate_supported_formats.py` to refresh docs.
# REST validation, MCP exposure (if `surfaces` includes "mcp"), extraction
# dispatch, and the docs table all pick it up with no other edits.
FILE_TYPE_REGISTRY: tuple[FileTypeSpec, ...] = (
    FileTypeSpec(
        key="txt",
        mime_types=("text/plain",),
        extensions=(".txt",),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="text_passthrough",
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="markdown",
        mime_types=("text/markdown",),
        extensions=(".md", ".markdown"),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="text_passthrough",
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="csv",
        mime_types=("text/csv",),
        extensions=(".csv",),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="text_passthrough",
        chunking_hint="tabular",
    ),
    FileTypeSpec(
        key="html",
        mime_types=("text/html",),
        extensions=(".html", ".htm"),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="html",
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="pdf",
        mime_types=("application/pdf",),
        extensions=(".pdf",),
        magic=b"%PDF-",
        surfaces=frozenset({"rest"}),
        extractor="pdf",
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="json",
        mime_types=("application/json",),
        extensions=(".json",),
        magic=None,
        # REST-only: MCP upload_document is text/*-only by design (#87 Task
        # 3), and JSON is a distinct content family even though it's
        # transported as text.
        surfaces=frozenset({"rest"}),
        extractor="json_pretty",
        chunking_hint="structured",
    ),
    FileTypeSpec(
        key="docx",
        mime_types=("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
        extensions=(".docx",),
        # DOCX is a ZIP container (OOXML); PK\x03\x04 is the local-file-header
        # signature every ZIP starts with. NOTE: this signature is shared by
        # every OOXML sibling format (xlsx below, pptx below, and ZIP itself
        # #130) -- a 4-byte prefix cannot distinguish "this zip is a .docx"
        # from "this zip is a .xlsx" or "this zip is a .pptx". It DOES still
        # catch a non-zip file (text, PNG, ...) mislabeled as docx, which is
        # what #117 requires. Disambiguating between OOXML siblings needs
        # inspecting the zip's internal `[Content_Types].xml`, which this
        # lightweight sniff deliberately does not do -- see
        # `check_extension_consistency` (filename) and the extraction stage
        # (fails loudly on a structurally wrong OOXML part set) for the two
        # layers that catch a genuinely mislabeled upload instead.
        magic=b"PK\x03\x04",
        surfaces=frozenset({"rest"}),
        extractor="docx",
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="xlsx",
        mime_types=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
        extensions=(".xlsx",),
        # Same ZIP local-file-header signature as docx/pptx -- see the docx
        # entry's comment and `_magic_families_overlap` for why this is
        # correct (allow, not reject) rather than a bug. Legacy `.xls` is a
        # completely different binary format (OLE2/CFBF, magic
        # D0 CF 11 E0 A1 B1 1A E1) with NO registry entry -- it is not this
        # spec's `extensions`, so a declared `.xls` upload 400s via the
        # standard `UnknownContentTypeError` (never accept-then-garble). That
        # message names every supported type, including this xlsx entry, so
        # it IS actionable -- but it is the generic list, not the #118 issue
        # body's suggested bespoke "convert to .xlsx" wording; a friendlier,
        # legacy-format-specific message is filed as a follow-up (see
        # surface-friction issue linked from the #118/#119 PR).
        magic=b"PK\x03\x04",
        surfaces=frozenset({"rest"}),
        extractor="xlsx",
        chunking_hint="tabular",
    ),
    FileTypeSpec(
        key="pptx",
        mime_types=("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
        extensions=(".pptx",),
        # Same ZIP family as docx/xlsx -- see the docx entry's comment.
        # Legacy `.ppt` (OLE2/CFBF) has no registry entry, same reasoning as
        # xlsx/.xls above.
        magic=b"PK\x03\x04",
        surfaces=frozenset({"rest"}),
        extractor="pptx",
        # ChunkingHint has no "sections" member (the #119 issue's proposed
        # name for slide-boundary chunking) -- "structured" is the closest
        # existing value: like JSON, a PPTX is a sequence of discrete,
        # addressable units (slides) rather than continuous prose, which is
        # exactly what #129's format-aware chunker needs to know to chunk on
        # slide boundaries instead of sentence/paragraph breaks.
        chunking_hint="structured",
    ),
    FileTypeSpec(
        key="png",
        mime_types=("image/png",),
        extensions=(".png",),
        magic=b"\x89PNG\r\n\x1a\n",
        surfaces=frozenset({"rest"}),
        extractor="image_ocr",
        chunking_hint="media",
        optional_extra="ocr",
        degradation="placeholder",
    ),
    # -- Long-tail formats (#124/#125/#126) -- the low-dependency group: EML
    # needs only the stdlib `email` package, EPUB needs stdlib `zipfile` plus
    # the bs4 already a core dep, RTF needs one tiny pure-Python dep
    # (striprtf), and ODT is a zip of XML read through the same tag-strip
    # path as HTML/EPUB. All REST-only: EML transports raw, possibly
    # non-UTF-8-encoded bytes and the other three are binary containers, none
    # of which can cross the MCP upload_document tool's inline-UTF-8-text-only
    # boundary (#87 Task 3).
    FileTypeSpec(
        key="eml",
        mime_types=("message/rfc822",),
        extensions=(".eml",),
        # RFC 822 messages have no binary file signature -- like every other
        # text/* entry above, this relies entirely on sniff_content_type's
        # cross-check against OTHER specs' signatures (e.g. real PNG bytes
        # declared message/rfc822 are still caught).
        magic=None,
        surfaces=frozenset({"rest"}),
        extractor="eml",
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="epub",
        mime_types=("application/epub+zip",),
        extensions=(".epub",),
        # EPUB is a ZIP container (OCF) -- shares the identical PK\x03\x04
        # signature with docx/xlsx/pptx/odt/zip. See the `docx` entry's
        # comment and `_magic_families_overlap`: this is a "same family,
        # cannot disambiguate at this level" case, not a conflict -- both
        # docx and epub still validate correctly (pinned by
        # test_docx_still_validates_with_epub_and_odt_registered in
        # inh-contracts' test suite).
        magic=b"PK\x03\x04",
        surfaces=frozenset({"rest"}),
        extractor="epub",
        # The closed ChunkingHint vocabulary (#129) has no dedicated
        # "chapter-segmented" value yet; "prose" is the closest existing fit
        # for long-form chapter text. The extractor's own "## Chapter N"
        # markers (spine order) are what give a future format-aware chunker
        # the actual chapter boundaries to split on.
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="rtf",
        # application/rtf is the canonical/registered IANA type; text/rtf is
        # the alias many real-world clients (older Word exports, macOS
        # TextEdit) send instead -- both must be accepted (#126).
        mime_types=("application/rtf", "text/rtf"),
        extensions=(".rtf",),
        # Real RTF files begin with the literal control word "{\rtf1..." --
        # a distinct signature from every other family registered here (not
        # the shared ZIP PK\x03\x04 prefix), so this one gets a real,
        # disambiguating magic check rather than a same-family pass-through.
        magic=b"{\\rtf",
        # ANCHORED to the first 8 bytes (5-byte magic + 3 bytes of slack for
        # a stray UTF-8 BOM) rather than the default 1024-byte window: a
        # real RTF file's signature is always at byte 0, and unlike PDF's
        # `%PDF-`, the string "{\rtf" is plausible ordinary prose (e.g. a
        # sentence about RTF itself) that a full-window substring search
        # would wrongly reject when it appears anywhere in an unrelated
        # text/markdown/html/eml upload (#126 review item 5).
        magic_anchor_window=8,
        # RTF is a control-word TEXT format, not a binary container --
        # exempted from the binary-extension mismatch check the same way
        # .txt/.md/.csv/.html are, since text/plain is a truthful Content-
        # Type a real client may declare for it (#126 review item 6).
        extension_check_exempt=True,
        surfaces=frozenset({"rest"}),
        extractor="rtf",
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="odt",
        mime_types=("application/vnd.oasis.opendocument.text",),
        extensions=(".odt",),
        # ODT is also a ZIP container (ODF) -- same PK\x03\x04 family as
        # docx/epub above; see the epub entry's comment for why this is safe.
        magic=b"PK\x03\x04",
        surfaces=frozenset({"rest"}),
        extractor="odt",
        chunking_hint="prose",
    ),
    # -- #121: structured text (YAML, TOML, XML) -------------------------
    # Ingested as decoded text with NO parse step: malformed YAML is still
    # searchable rather than rejecting the upload over a syntax error a
    # human writer made (the same "never lossy, never a hard gate on
    # correctness" philosophy `_decode_text` already applies -- see
    # extract.py). RFC 9512 registers "application/yaml"; "text/yaml" is
    # the long-standing de facto alias most tooling still emits.
    FileTypeSpec(
        key="yaml",
        mime_types=("application/yaml", "text/yaml"),
        extensions=(".yaml", ".yml"),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="text_passthrough",
        chunking_hint="structured",
    ),
    FileTypeSpec(
        key="toml",
        mime_types=("application/toml",),
        extensions=(".toml",),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="text_passthrough",
        chunking_hint="structured",
    ),
    # XML tags are stripped via the same BeautifulSoup `html.parser` path as
    # HTML (extractor="xml" -> `_extract_html_text` in inh-ingestion-svc) --
    # deliberately the stdlib `html.parser` backend, not `lxml`'s XML mode:
    # `html.parser` never resolves DTDs or external entities at all, so it
    # has no XXE / billion-laughs entity-expansion surface on untrusted
    # input, unlike a naive `xml.etree.ElementTree.parse`. Trade-off: it is
    # not a strict/validating XML parser, so malformed XML degrades to
    # best-effort text extraction instead of raising -- consistent with
    # YAML/TOML's own "still searchable" policy above. Attribute VALUES are
    # dropped (only element text content survives `get_text()`), same as
    # HTML's existing attribute-stripping behavior.
    FileTypeSpec(
        key="xml",
        mime_types=("application/xml", "text/xml"),
        extensions=(".xml",),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="xml",
        chunking_hint="structured",
    ),
    # -- #122: source code -- explicit extension contract ----------------
    # The 21-extension allowlist IS the contract (`extensions` below is the
    # authoritative source of truth per the issue) -- `mime_types` is an
    # accepted-alias convenience layer on top of it, not the gate: a client
    # sending a generic/absent content type (see `get_spec_for_upload`)
    # falls back to this list. Extraction is a plain decode (chunking_hint
    # "code" -- see the ChunkingHint comment -- awaits #129's chunker;
    # "plain decode until then" per the issue).
    FileTypeSpec(
        key="code",
        mime_types=(
            "text/x-python",
            "application/javascript",
            "text/javascript",
            "application/typescript",
            "text/x-go",
            "text/x-java-source",
            "text/x-rustsrc",
            "text/x-csrc",
            "text/x-chdr",
            "text/x-c++src",
            "text/x-csharp",
            "text/x-ruby",
            "text/x-php",
            "text/x-swift",
            "text/x-kotlin",
            "text/x-scala",
            "application/x-sh",
            "text/x-sh",
            "application/sql",
            "text/x-sql",
            "text/x-r-source",
            "text/x-lua",
        ),
        extensions=(
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".go",
            ".java",
            ".rs",
            ".c",
            ".h",
            ".cpp",
            ".cs",
            ".rb",
            ".php",
            ".swift",
            ".kt",
            ".scala",
            ".sh",
            ".sql",
            ".r",
            ".lua",
        ),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="text_passthrough",
        chunking_hint="code",
        # #197 fix: `mime_types` above is a POOL of 22 accepted aliases for
        # 21 extensions -- not a positional, one-per-extension mapping (.js,
        # .sh, and .sql each legitimately have two accepted aliases; .ts/
        # .tsx/.jsx share no dedicated IANA-registered type at all). Every
        # value here is still a member of `mime_types` above (so declaring it
        # explicitly on either surface keeps working exactly as before) --
        # this map only decides which ONE of the accepted aliases is
        # "canonical" for a given extension when the caller doesn't declare
        # one, replacing the old `mime_types[0]` guess that answered
        # "text/x-python" for every single one of them.
        mime_type_by_extension=(
            (".py", "text/x-python"),
            (".js", "text/javascript"),
            (".ts", "application/typescript"),
            (".tsx", "application/typescript"),
            (".jsx", "text/javascript"),
            (".go", "text/x-go"),
            (".java", "text/x-java-source"),
            (".rs", "text/x-rustsrc"),
            (".c", "text/x-csrc"),
            (".h", "text/x-chdr"),
            (".cpp", "text/x-c++src"),
            (".cs", "text/x-csharp"),
            (".rb", "text/x-ruby"),
            (".php", "text/x-php"),
            (".swift", "text/x-swift"),
            (".kt", "text/x-kotlin"),
            (".scala", "text/x-scala"),
            (".sh", "application/x-sh"),
            (".sql", "application/sql"),
            (".r", "text/x-r-source"),
            (".lua", "text/x-lua"),
        ),
    ),
    # -- #127: transcripts (SRT, WebVTT) ----------------------------------
    # Cue numbers and raw per-cue timestamp lines are stripped; cue text is
    # joined into prose with a coarse `[t=MM:SS]` marker reinserted every
    # few cues so an agent can still cite roughly WHEN something was said
    # without the sentence-level timestamp noise polluting embeddings (see
    # `_extract_subtitle_text` in inh-ingestion-svc for the exact policy).
    FileTypeSpec(
        key="srt",
        mime_types=("application/x-subrip",),
        extensions=(".srt",),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="srt",
        chunking_hint="prose",
    ),
    FileTypeSpec(
        key="vtt",
        mime_types=("text/vtt",),
        extensions=(".vtt",),
        magic=None,
        surfaces=frozenset({"rest", "mcp"}),
        extractor="vtt",
        chunking_hint="prose",
    ),
)


# ---------------------------------------------------------------------------
# Explicitly unsupported formats -- deliberately NOT in FILE_TYPE_REGISTRY,
# but with a real supported replacement, so a caller gets a SPECIFIC,
# actionable rejection instead of the generic "Unsupported file type" allow-
# list dump (#124/#126). "Explicit 400, never accept-then-garble" per both
# issues -- the message names the replacement instead of leaving the caller
# to guess.
#
# This lives beside FILE_TYPE_REGISTRY (not duplicated per-service) after a
# #124/#126 review caught the single-service version of this table causing a
# cross-surface hole: REST's own local copy meant MCP's `upload_document`
# never learned about it, and MCP's content_type default (derived from the
# filename extension when the caller omits content_type) fell through to a
# generic MCP-eligible type for a filename like "report.doc" -- silently
# accepting and indexing the exact format both issues say must be rejected.
# Both `mime_types` AND `extensions` are needed here (unlike most of this
# module, which treats extension as a secondary signal): REST always has a
# declared Content-Type, but MCP's upload_document resolves its content_type
# FROM the filename extension when the caller omits it, so the extension
# itself must be a first-class rejection key, not just a fallback check.
@dataclass(frozen=True)
class ExplicitlyUnsupportedSpec:
    """One deliberately-rejected format: a short key, the MIME type(s) and
    extension(s) that identify it, and the actionable message every surface
    shows instead of the generic allow-list dump."""

    key: str
    mime_types: tuple[str, ...]
    extensions: tuple[str, ...]
    message: str


EXPLICITLY_UNSUPPORTED: tuple[ExplicitlyUnsupportedSpec, ...] = (
    ExplicitlyUnsupportedSpec(
        key="doc",
        mime_types=("application/msword",),
        extensions=(".doc",),
        message="Legacy .doc files are not supported. Convert the file to .docx and re-upload.",
    ),
    ExplicitlyUnsupportedSpec(
        key="msg",
        mime_types=("application/vnd.ms-outlook",),
        extensions=(".msg",),
        message="Outlook .msg files are not supported. Export the message to .eml and re-upload.",
    ),
)


def explicitly_unsupported_message_for_mime(content_type: str) -> str | None:
    """The actionable rejection message for `content_type`, or None if it
    isn't one of the specifically-called-out unsupported formats above.
    Normalized the same way `get_spec_for_mime` normalizes the registry
    (strip Content-Type parameters, lowercase, strip whitespace)."""
    normalized = content_type.split(";", 1)[0].strip().lower()
    for spec in EXPLICITLY_UNSUPPORTED:
        if normalized in spec.mime_types:
            return spec.message
    return None


def explicitly_unsupported_message_for_extension(filename: str) -> str | None:
    """The actionable rejection message for `filename`'s extension, or None.

    Needed on top of the MIME-based lookup above for any surface that can
    resolve a content type FROM the filename when none is declared (MCP's
    upload_document, see the module comment above) -- checking the MIME
    alone misses that path entirely, which is exactly how a `report.doc`
    upload with `content_type` omitted used to slip through as
    ``text/markdown`` (#124/#126 review blocker 3).
    """
    if "." not in filename:
        return None
    extension = "." + filename.rsplit(".", 1)[-1].strip().lower()
    for spec in EXPLICITLY_UNSUPPORTED:
        if extension in spec.extensions:
            return spec.message
    return None


# ---------------------------------------------------------------------------
# Lookups derived from the registry -- REST, MCP, and extraction all call
# these instead of maintaining their own copy.
# ---------------------------------------------------------------------------


def get_spec_for_mime(mime_type: str) -> FileTypeSpec | None:
    """Look up the registry entry whose ``mime_types`` contains `mime_type`.

    Case-insensitive/whitespace-tolerant, and strips any Content-Type
    PARAMETERS (e.g. ``text/plain; charset=utf-8`` -> ``text/plain``) --
    the most common real-world Content-Type variation, routinely emitted by
    browsers and HTTP client libraries, since Content-Type headers are
    entirely client-supplied and inconsistently formatted in the wild.
    """
    normalized = mime_type.split(";", 1)[0].strip().lower()
    for spec in FILE_TYPE_REGISTRY:
        if normalized in spec.mime_types:
            return spec
    return None


def get_spec_for_extension(extension: str) -> FileTypeSpec | None:
    """Look up the registry entry whose ``extensions`` contains `extension`.

    Accepts the extension with or without a leading dot (".md" or "md").
    """
    normalized = extension if extension.startswith(".") else f".{extension}"
    normalized = normalized.strip().lower()
    for spec in FILE_TYPE_REGISTRY:
        if normalized in spec.extensions:
            return spec
    return None


def mime_type_for_extension(spec: FileTypeSpec, extension: str) -> str:
    """The single best, SPECIFIC MIME type `spec` resolves `extension` to
    (#197).

    `mime_types[0]` is "the" canonical MIME type ONLY for a spec describing
    ONE format -- true of every current spec except "code", whose
    `mime_types` pools 22 aliases across 21 distinct languages sharing one
    extractor. Blindly taking index 0 there answered "text/x-python" for
    every code file regardless of its real extension (a Go, Java, or SQL
    file all mislabelled identically -- the exact bug this function exists
    to close). This consults `spec.mime_type_by_extension` (see that field's
    docstring) when present; a spec with no override (`None`) keeps the
    original `mime_types[0]` behaviour unchanged, and an extension present
    in `spec.extensions` but missing from the override map (should not
    happen -- see the "every code extension has an override" test) degrades
    to the same safe fallback rather than raising.

    Args:
        spec: an already-resolved spec, e.g. from `get_spec_for_extension`.
        extension: with or without the leading dot, case-insensitively --
            same tolerance as `get_spec_for_extension`.
    """
    normalized = extension if extension.startswith(".") else f".{extension}"
    normalized = normalized.strip().lower()
    if spec.mime_type_by_extension is not None:
        # A tuple of (extension, mime) pairs, not a dict (see the field's
        # docstring on FileTypeSpec for why) -- a linear scan over it is
        # negligible at this size (currently 21 entries, one spec) and keeps
        # the field itself hashable/immutable, preserving this dataclass's
        # `frozen=True` contract.
        for candidate_extension, mime in spec.mime_type_by_extension:
            if candidate_extension == normalized:
                return mime
    return spec.mime_types[0]


# Content-Type values that carry no real type information -- the "I don't
# know what this is" signal a client sends when it has nothing better, most
# commonly `file.content_type or "application/octet-stream"` fallbacks in
# inh-public-api-svc's own REST route. `get_spec_for_upload` only consults
# the extension for THESE values, never for a specific-but-unrecognized one
# (see that function's docstring for why this distinction is load-bearing).
GENERIC_CONTENT_TYPES = frozenset({"application/octet-stream", ""})


def get_spec_for_upload(content_type: str, filename: str) -> FileTypeSpec | None:
    """Resolve the spec for an upload, consulting the filename extension as
    a FALLBACK only when `content_type` is generic or absent (#122).

    This completes the design `FileTypeSpec.extensions` was reserved for at
    #117 ("a fallback classifier for a generic/absent content-type ... not
    yet consulted") rather than working around it: the extension was always
    intended to answer "what is this file" when the declared MIME type
    can't, and this is that consultation, finally wired into the upload
    path via `get_spec_for_upload`.

    Resolution order:
    1. `content_type` maps to a registered spec directly -> that spec wins,
       filename is never even inspected. The common case (a client sending
       an accurate, specific Content-Type) is unaffected by this function
       existing at all.
    2. `content_type` is one of `GENERIC_CONTENT_TYPES` (i.e. genuinely
       carries no information) AND `filename`'s extension is registered ->
       fall back to the extension's spec.
    3. Otherwise -> None, same as `get_spec_for_mime` returning None today.

    SECURITY (#122 review note): step 2 fires ONLY for a generic/absent
    content type, never for a SPECIFIC-but-unrecognized one (e.g.
    "application/x-something-made-up"). Falling back on any unrecognized
    MIME type would turn every unregistered format into an accepted upload
    the moment its filename happened to carry a known extension -- an
    over-broad allowlist widening, not the narrow "octet-stream + a listed
    extension is accepted" contract #122 actually asks for. A client that
    declares a real, WRONG, specific type is still flatly rejected; only a
    client that admits it doesn't know gets the extension consulted.

    Callers that also need to sniff the bytes should pass the returned spec
    into `sniff_content_type`'s `resolved_spec` parameter -- re-deriving a
    spec from `content_type` alone inside that function would fail for
    exactly the generic-content-type case this function exists to resolve.
    """
    spec = get_spec_for_mime(content_type)
    if spec is not None:
        return spec

    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized not in GENERIC_CONTENT_TYPES:
        return None

    if "." not in filename:
        return None
    extension = "." + filename.rsplit(".", 1)[-1]
    return get_spec_for_extension(extension)


def get_spec_by_key(key: str) -> FileTypeSpec | None:
    """Look up a registry entry by its short `key` (e.g. "pdf")."""
    for spec in FILE_TYPE_REGISTRY:
        if spec.key == key:
            return spec
    return None


def all_mime_types() -> list[str]:
    """Every accepted MIME type across all registered formats.

    Replaces the old hand-maintained ``ALLOWED_MIME_TYPES`` list: REST
    upload validation, the 400 error text, and generated docs all derive
    from this so they cannot disagree about what "supported" means.
    """
    return [mime for spec in FILE_TYPE_REGISTRY for mime in spec.mime_types]


def mcp_mime_types() -> tuple[str, ...]:
    """MIME types the MCP ``upload_document`` tool accepts.

    Replaces the old ``ALLOWED_MIME_TYPES`` ``.startswith("text/")`` guess in
    ``mcp_server/server.py``. That heuristic happened to be correct only
    because every current MCP-eligible type's MIME starts with "text/" --
    the moment a sibling issue adds a text/*-prefixed type that ISN'T
    MCP-safe, or a non-"text/"-prefixed type that IS (e.g. a future
    structured format transported as text), the heuristic silently
    misclassifies it. ``surfaces`` makes the decision an explicit, per-type
    fact in the registry instead of an inferred string property.
    """
    return tuple(
        sorted(
            mime
            for spec in FILE_TYPE_REGISTRY
            if "mcp" in spec.surfaces
            for mime in spec.mime_types
        )
    )


# ---------------------------------------------------------------------------
# Intake-time sniffing -- closes hole #1 from the module docstring
# ---------------------------------------------------------------------------


class UnknownContentTypeError(ValueError):
    """A declared/stored content type has no matching registry entry."""

    def __init__(self, declared_mime: str):
        self.declared_mime = declared_mime
        super().__init__(
            f"Unsupported content type '{declared_mime}'. "
            f"Supported types: {', '.join(all_mime_types())}"
        )


class ContentTypeMismatchError(ValueError):
    """A file's magic bytes contradict its declared content type."""

    def __init__(self, declared_mime: str, reason: str):
        self.declared_mime = declared_mime
        self.reason = reason
        super().__init__(
            f"File content does not match the declared content type "
            f"'{declared_mime}': {reason}."
        )


# Bounded prefix window magic bytes are searched within, instead of requiring
# an exact match at byte offset 0. PDF is the concrete reason: this repo's
# own pypdf parses PDFs that have a leading blank line, a UTF-8 BOM, or
# leading whitespace before "%PDF-" just fine (scanner/legacy-tool output
# routinely has this shape; the PDF spec itself tolerates junk before the
# header), so a strict startswith() rejected real, already-working uploads.
# 1024 bytes is the conventional tolerance (matches common `file`-style
# magic detectors) -- generous enough for any realistic preamble, small
# enough that an unrelated coincidental match deep in a large file is not a
# realistic concern for the formats registered today.
_SNIFF_WINDOW = 1024


def _contains_signature(content: bytes, magic: bytes, window: int = _SNIFF_WINDOW) -> bool:
    """Whether `magic` appears within the first `window` bytes of `content`
    -- see the module comment above for why this isn't a strict
    ``content.startswith(magic)``. `window` defaults to `_SNIFF_WINDOW` but
    is overridable per-spec via `FileTypeSpec.magic_anchor_window` (#126
    review item 5) for a format whose real files never have anything before
    the signature, where a full 1024-byte substring search risks matching
    ordinary prose instead of an actual file of that format."""
    return magic in content[:window]


def _magic_families_overlap(a: bytes, b: bytes) -> bool:
    """Whether two magic signatures belong to the same underlying container
    format (#117 structural fix -- see the `docx` registry entry's comment).

    OOXML formats (docx/xlsx/pptx) and plain ZIP all share the identical
    4-byte ZIP local-file-header signature ``PK\\x03\\x04`` -- a 4-byte
    prefix cannot distinguish "this zip is a .docx" from "this zip is a
    .xlsx" without inspecting the archive's internal ``[Content_Types].xml``,
    which this lightweight, dependency-free sniff deliberately does not do.
    One signature being a PREFIX of the other (equal, in the current
    registry, but checked both ways for any future format whose signature
    happens to extend another's) means "same family, cannot disambiguate at
    this level" -- and the correct response to "cannot disambiguate" is to
    ALLOW both, not reject both. Rejecting would mean the moment a second
    OOXML-family format (#118 XLSX) is registered, EVERY upload of the
    FIRST one (docx) starts failing too -- the exact structural bug a
    sibling-format issue must not be able to introduce.
    """
    return a.startswith(b) or b.startswith(a)


def sniff_content_type(
    content: bytes, declared_mime: str, *, resolved_spec: FileTypeSpec | None = None
) -> FileTypeSpec:
    """Validate that `content`'s magic bytes agree with `declared_mime` (#117).

    MIME type is entirely client-supplied and, before this function existed,
    was never checked against the actual bytes -- a mislabeled binary (e.g.
    PNG bytes declared as ``text/plain``) passed validation and was garbled
    downstream instead of rejected. Two checks, run in both directions so
    either a wrong "this IS a real binary format" claim or a wrong "this is
    just text" claim gets caught:

    1. `declared_mime` maps to a spec with a known signature -> `content`
       MUST contain it within the first `_SNIFF_WINDOW` bytes (catches
       "declared PDF, isn't actually PDF").
    2. `content`'s bytes match a DIFFERENT spec's signature than the
       declared one -> still a mismatch (catches "PNG bytes declared as
       text/plain", where text/plain has no signature of its own for
       check 1 to fail on) -- UNLESS the two signatures belong to the same
       container family (see `_magic_families_overlap`), in which case
       neither can be disambiguated at this level and both are allowed.

    Formats with no magic signature (every current text/* format) skip
    check 1 -- there is nothing to compare against -- but remain subject to
    check 2, so a binary file mislabeled as text is still caught.

    Returns the resolved ``FileTypeSpec`` for `declared_mime` on success.

    Args:
        resolved_spec: pass this when the caller already resolved a spec via
            `get_spec_for_upload` (#122) instead of a plain
            `get_spec_for_mime` lookup -- e.g. an ``application/octet-stream``
            upload that resolved through the filename-extension fallback.
            Re-deriving from `declared_mime` alone inside this function
            would fail for exactly that case (a generic content type has no
            direct registry entry by construction), so the already-resolved
            spec is threaded through rather than recomputed. When omitted,
            behavior is unchanged: `declared_mime` is looked up directly.

    Raises:
        UnknownContentTypeError: `declared_mime` has no registry entry (and
            no `resolved_spec` was supplied).
        ContentTypeMismatchError: the bytes contradict `declared_mime`.
    """
    spec = resolved_spec if resolved_spec is not None else get_spec_for_mime(declared_mime)
    if spec is None:
        raise UnknownContentTypeError(declared_mime)

    # The window a given spec's OWN signature is searched within is a
    # property of THAT spec (`magic_anchor_window`), not of the sniff call
    # site -- whether junk may legitimately precede the signature depends on
    # the format itself (PDF: yes: RTF: no, see the field's docstring).
    own_window = spec.magic_anchor_window if spec.magic_anchor_window is not None else _SNIFF_WINDOW
    if spec.magic is not None and not _contains_signature(content, spec.magic, own_window):
        raise ContentTypeMismatchError(
            declared_mime,
            f"expected the '{spec.key}' file signature but the bytes did not match it",
        )

    for other in FILE_TYPE_REGISTRY:
        if other.magic is None or other.key == spec.key:
            continue
        if spec.magic is not None and _magic_families_overlap(spec.magic, other.magic):
            continue
        other_window = (
            other.magic_anchor_window if other.magic_anchor_window is not None else _SNIFF_WINDOW
        )
        if _contains_signature(content, other.magic, other_window):
            raise ContentTypeMismatchError(
                declared_mime,
                f"the bytes match the '{other.key}' file signature instead",
            )

    return spec


class ExtensionMismatchError(ValueError):
    """A filename's extension belongs to a DIFFERENT registered type than
    the one resolved from the declared content type."""

    def __init__(self, filename: str, declared_key: str, extension_key: str):
        self.filename = filename
        self.declared_key = declared_key
        self.extension_key = extension_key
        super().__init__(
            f"Filename '{filename}' has an extension registered to type "
            f"'{extension_key}', which does not match the declared content "
            f"type (resolved to '{declared_key}')."
        )


def check_extension_consistency(filename: str, declared_spec: FileTypeSpec) -> None:
    """Cross-check a filename's extension against the DECLARED type (#117).

    Three independent signals describe an upload: the declared content type,
    the filename's extension, and the actual bytes. `sniff_content_type`
    checks bytes against the declared type; this checks the filename against
    the declared type -- but ONLY when the extension resolves to a BINARY
    format (``magic is not None``). A binary-format extension (``.pdf``,
    ``.docx``, ``.png``, ...) claiming a different declared type than that
    IS a real, actionable contradiction. A text-format extension does not
    get the same treatment, because MIME type for plain text is inherently
    ambiguous: ``text/plain`` is a truthful, IANA-valid Content-Type for a
    Markdown, CSV, or HTML file too (``text/markdown`` was only registered
    in 2016; plenty of HTTP clients and OS mime databases still default any
    text file to ``text/plain``), so ``README.md`` declared ``text/plain``,
    ``data.csv`` declared ``text/plain``, ``page.html`` declared
    ``text/plain``, and ``notes.txt`` declared ``text/markdown`` are all
    correctly-labeled uploads that worked before #117 and must keep working.
    Rejecting those combinations bought nothing (three of the four dispatch
    to the identical ``"text_passthrough"`` extractor regardless of which
    was declared) and broke working callers.

    Deliberately permissive when the extension is NOT one this registry
    recognizes at all (e.g. ``.log``, ``.rst`` -- formats the registry doesn't cover
    yet, or no extension at all, e.g. the REST route's ``"unnamed"``
    fallback): content type remains the authoritative signal, and an
    unrecognized extension is not evidence of anything.

    Raises:
        ExtensionMismatchError: the filename has a BINARY-format extension
            registered to a different type than `declared_spec`.
    """
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not extension:
        return

    extension_spec = get_spec_for_extension(extension)
    if (
        extension_spec is None
        or extension_spec.magic is None
        # RTF has a `magic` (needed for the binary-mislabel sniff above) but
        # is genuinely ASCII text, not a binary container -- exempted here
        # the same way .txt/.md/.csv/.html are, via `extension_check_exempt`
        # rather than `magic is None`, since it still needs real sniffing
        # (#126 review item 6).
        or extension_spec.extension_check_exempt
    ):
        return
    if extension_spec.key == declared_spec.key:
        return

    raise ExtensionMismatchError(filename, declared_spec.key, extension_spec.key)


# ---------------------------------------------------------------------------
# Docs generation -- closes the "docs can drift from code" defect (#117)
# ---------------------------------------------------------------------------


def render_markdown_table() -> str:
    """Render the supported-file-types table straight from the registry.

    ``docs/reference/file-types.md`` embeds this exact string between
    generated-content markers. ``scripts/generate_supported_formats.py``
    regenerates it; ``test_docs_sync.py`` (inh-public-api-svc) fails CI the
    moment the checked-in table and this function disagree -- the docs/code
    drift #117 exists to prevent, made unable to happen silently.
    """
    header = (
        "| Type | Extension(s) | MIME type(s) | Surfaces | Chunking hint | Extra required |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
    )
    rows = []
    for spec in FILE_TYPE_REGISTRY:
        exts = ", ".join(f"`{e}`" for e in spec.extensions)
        mimes = ", ".join(f"`{m}`" for m in spec.mime_types)
        # Stable "rest" / "rest + mcp" rendering regardless of frozenset order.
        surfaces = " + ".join(s for s in ("rest", "mcp") if s in spec.surfaces)
        extra = f"`{spec.optional_extra}`" if spec.optional_extra else "—"
        rows.append(
            f"| {spec.key} | {exts} | {mimes} | {surfaces} | {spec.chunking_hint} | {extra} |"
        )
    return header + "\n".join(rows) + "\n"
