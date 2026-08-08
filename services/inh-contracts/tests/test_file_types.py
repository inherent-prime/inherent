"""Tests for the file-type support registry (#117).

Written before the implementation (TESTS FIRST per CLAUDE.md): running this
file against the pre-#117 codebase fails with ImportError because
``inh_contracts.file_types`` did not exist -- there was no single place any
of these facts were even queryable, only five independent, hand-maintained
copies (see the module docstring in ``inh_contracts/file_types.py``).
"""

from __future__ import annotations

import pytest

from inh_contracts.file_types import (
    EXPLICITLY_UNSUPPORTED,
    FILE_TYPE_REGISTRY,
    ContentTypeMismatchError,
    ExtensionMismatchError,
    FileTypeSpec,
    UnknownContentTypeError,
    all_mime_types,
    check_extension_consistency,
    explicitly_unsupported_message_for_extension,
    explicitly_unsupported_message_for_mime,
    get_spec_by_key,
    get_spec_for_extension,
    get_spec_for_mime,
    get_spec_for_upload,
    mcp_mime_types,
    mime_type_for_extension,
    render_markdown_table,
    sniff_content_type,
)

# ---------------------------------------------------------------------------
# Registry shape / internal consistency
# ---------------------------------------------------------------------------


class TestRegistryShape:
    """The registry itself must be internally consistent -- these are the
    invariants every sibling format issue (#118-#130) will be trusted to
    keep holding when it adds an entry."""

    def test_keys_are_unique(self):
        keys = [spec.key for spec in FILE_TYPE_REGISTRY]
        assert len(keys) == len(set(keys)), "duplicate FileTypeSpec.key"

    def test_mime_types_are_globally_unique(self):
        """No two specs may claim the same MIME type -- lookup must be
        unambiguous."""
        seen: set[str] = set()
        for spec in FILE_TYPE_REGISTRY:
            for mime in spec.mime_types:
                assert mime not in seen, f"MIME '{mime}' claimed by multiple specs"
                seen.add(mime)

    def test_every_spec_has_at_least_one_mime_and_extension(self):
        for spec in FILE_TYPE_REGISTRY:
            assert spec.mime_types, f"{spec.key} has no mime_types"
            assert spec.extensions, f"{spec.key} has no extensions"

    def test_degradation_is_meaningless_without_optional_extra(self):
        """A spec with no optional_extra has nothing optional to degrade --
        the field should stay at its "hard_fail" default so it isn't
        mistakenly read as promising graceful degradation that doesn't
        exist."""
        for spec in FILE_TYPE_REGISTRY:
            if spec.optional_extra is None:
                assert spec.degradation == "hard_fail", (
                    f"{spec.key} has no optional_extra but degradation=" f"{spec.degradation!r}"
                )

    def test_current_registered_formats_are_exactly_these(self):
        """Pins the full registered format set (20 as of #121/#122/#127).

        The eight pre-#117 formats migrated with no
        loss (acceptance criterion: 'All 8 current formats migrate to
        registry entries with behavior unchanged')."""
        keys = {spec.key for spec in FILE_TYPE_REGISTRY}
        assert keys == {
            "txt",
            "markdown",
            "csv",
            "html",
            "pdf",
            "json",
            "docx",
            "xlsx",
            "pptx",
            "png",
            "eml",
            "epub",
            "rtf",
            "odt",
            "yaml",
            "toml",
            "xml",
            "code",
            "srt",
            "vtt",
        }

    def test_longtail_formats_present(self):
        """#124/#125/#126: eml, epub, rtf, odt are registered."""
        keys = {spec.key for spec in FILE_TYPE_REGISTRY}
        assert {"eml", "epub", "rtf", "odt"} <= keys

    def test_text_family_formats_present(self):
        """#121 (YAML/TOML/XML), #122 (source code), #127 (SRT/WebVTT) --
        the three text-family sibling issues that land together in one
        workstream (see the FILE_TYPE_REGISTRY module comment)."""
        keys = {spec.key for spec in FILE_TYPE_REGISTRY}
        assert {"yaml", "toml", "xml", "code", "srt", "vtt"} <= keys

    def test_extensions_are_globally_unique(self):
        """Mirrors `test_mime_types_are_globally_unique` -- two specs
        claiming the same extension would make `get_spec_for_extension`
        (and therefore the octet-stream extension-fallback #122 relies on)
        silently pick whichever spec happens to be registered first."""
        seen: set[str] = set()
        for spec in FILE_TYPE_REGISTRY:
            for ext in spec.extensions:
                assert ext not in seen, f"extension '{ext}' claimed by multiple specs"
                seen.add(ext)

    def test_registry_is_hashable(self):
        """#197 review follow-up: `mime_type_by_extension` (a NEW field, #197)
        must not break this `@dataclass(frozen=True)`'s own contract. Every
        other field is a tuple/frozenset/str/bytes/bool/int specifically so a
        `FileTypeSpec` stays hashable -- a plain `dict` field would silently
        make `hash(spec)` and `set(FILE_TYPE_REGISTRY)` raise `TypeError:
        unhashable type: 'dict'` the moment anything (a future caller,
        de-duplication, a cache key) hashes a spec. Nothing in this module
        hashes a spec today, which is exactly why this needs its own pin
        rather than relying on an existing call site to catch a regression."""
        for spec in FILE_TYPE_REGISTRY:
            hash(spec)  # must not raise
        assert len(set(FILE_TYPE_REGISTRY)) == len(FILE_TYPE_REGISTRY)

    def test_mime_type_by_extension_keys_are_a_subset_of_the_specs_own_extensions(self):
        """#197 review follow-up, generalized across the whole registry (not
        just "code"): every extension named in a spec's OWN
        `mime_type_by_extension` override must be one of that SAME spec's
        `extensions` -- an override naming an extension the spec doesn't
        even declare is dead, unreachable configuration (`get_spec_for_extension`
        would never resolve THIS spec for that extension in the first
        place)."""
        for spec in FILE_TYPE_REGISTRY:
            if spec.mime_type_by_extension is None:
                continue
            for extension, _mime in spec.mime_type_by_extension:
                assert extension in spec.extensions, (
                    f"{spec.key}'s mime_type_by_extension names '{extension}', "
                    f"which is not in its own extensions {spec.extensions}"
                )

    def test_mime_type_by_extension_values_are_members_of_the_specs_own_mime_types(self):
        """#197 review follow-up: every MIME an override resolves to must be
        a member of that SAME spec's `mime_types` -- otherwise the default
        `_default_upload_content_type` picks for an omitted `content_type`
        (`mcp_server/server.py`) could resolve to a value that then fails
        `content_type not in SUPPORTED_TEXT_MIME_TYPES`
        (`mcp_server/server.py`), hard-rejecting an upload of a format this
        registry is supposed to accept -- silently, since nothing else in
        this suite cross-checks the override map against `mime_types`."""
        for spec in FILE_TYPE_REGISTRY:
            if spec.mime_type_by_extension is None:
                continue
            for extension, mime in spec.mime_type_by_extension:
                assert mime in spec.mime_types, (
                    f"{spec.key}'s mime_type_by_extension['{extension}'] = '{mime}', "
                    f"which is not among its own mime_types {spec.mime_types}"
                )

    def test_every_spec_extension_covered_by_mime_type_by_extension_when_present(self):
        """#197 review follow-up (moved from TestMimeTypeForExtension, made
        registry-wide): whenever a spec DOES define an override map, it must
        cover EVERY extension the spec declares -- an extension missing from
        the map silently falls back to `mime_types[0]` (`mime_type_for_extension`'s
        documented, deliberate degradation for a spec that has no override
        at all), quietly reintroducing #197 for just that one uncovered
        extension on a spec that otherwise opted into per-extension
        resolution."""
        for spec in FILE_TYPE_REGISTRY:
            if spec.mime_type_by_extension is None:
                continue
            covered = {extension for extension, _mime in spec.mime_type_by_extension}
            for extension in spec.extensions:
                assert extension in covered, (
                    f"{spec.key}'s mime_type_by_extension has no entry for "
                    f"'{extension}', one of its own declared extensions"
                )


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class TestLookups:
    def test_get_spec_for_mime_known(self):
        spec = get_spec_for_mime("application/pdf")
        assert spec is not None
        assert spec.key == "pdf"

    def test_get_spec_for_mime_unknown_returns_none(self):
        assert get_spec_for_mime("application/x-nonexistent") is None

    def test_get_spec_for_mime_is_case_and_whitespace_tolerant(self):
        spec = get_spec_for_mime("  APPLICATION/PDF  ")
        assert spec is not None
        assert spec.key == "pdf"

    def test_get_spec_for_extension_with_and_without_dot(self):
        assert get_spec_for_extension(".md").key == "markdown"
        assert get_spec_for_extension("md").key == "markdown"

    def test_get_spec_for_extension_unknown_returns_none(self):
        # NB: this deliberately uses an extension no issue will ever register.
        # It previously used ".xlsx", which stopped being unknown the moment
        # #118 landed -- caught by the restored exact pins at batch-3 merge.
        assert get_spec_for_extension(".xyz") is None
        assert get_spec_for_extension(".doc") is None

    def test_get_spec_by_key(self):
        assert get_spec_by_key("png").mime_types == ("image/png",)
        assert get_spec_by_key("does-not-exist") is None

    def test_all_mime_types_exact_set_and_order(self):
        """Pins the FULL registered MIME list, set AND order.

        The first eight entries preserve byte-for-byte parity with the
        pre-#117 hand-maintained ALLOWED_MIME_TYPES in constants.py, so the
        400 error text's wording is unchanged by that migration; the rest are
        the formats their own issues appended (#118, #119, #121, #122,
        #124-#127).

        This was temporarily relaxed to a prefix check while those format
        branches were in flight concurrently, purely to avoid every branch
        conflicting with every other on this one assertion. The exact pin is
        restored here now that they have all landed -- #117 added it because
        a registry comment claimed ordering parity it did not have, and a
        prefix check cannot catch that in the tail.
        """
        mimes = all_mime_types()
        assert mimes == [
            "text/plain",
            "text/markdown",
            "text/csv",
            "text/html",
            "application/pdf",
            "application/json",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "image/png",
            # #124/#125/#126: long-tail formats
            "message/rfc822",
            "application/epub+zip",
            "application/rtf",
            "text/rtf",
            "application/vnd.oasis.opendocument.text",
            # #121: structured text
            "application/yaml",
            "text/yaml",
            "application/toml",
            "application/xml",
            "text/xml",
            # #122: source code (extension allowlist is the source of truth;
            # these are the accepted MIME aliases on top of it)
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
            # #127: subtitle transcripts
            "application/x-subrip",
            "text/vtt",
        ]

    def test_longtail_mime_types_present(self):
        """#124/#125/#126: the four new long-tail MIME types are registered
        (order-independent -- see the prefix-only rationale above)."""
        assert set(all_mime_types()) >= {
            "message/rfc822",
            "application/epub+zip",
            "application/rtf",
            "text/rtf",
            "application/vnd.oasis.opendocument.text",
        }

    def test_get_spec_for_mime_strips_content_type_parameters(self):
        """The most common real-world Content-Type variation -- a browser or
        HTTP client appending '; charset=...' -- must not 400 (#117 review)."""
        spec = get_spec_for_mime("text/plain; charset=utf-8")
        assert spec is not None
        assert spec.key == "txt"

    def test_mcp_mime_types_matches_historical_text_subset(self):
        """Pins byte-for-byte parity with the pre-#117
        SUPPORTED_TEXT_MIME_TYPES (the text/* subset of ALLOWED_MIME_TYPES),
        plus #121/#122/#127 -- all three text-family additions declare
        surfaces={"rest", "mcp"} (see each FileTypeSpec's comment), so their
        MIME types now join the MCP upload_document allow-list too."""
        assert mcp_mime_types() == (
            "application/javascript",
            "application/sql",
            "application/toml",
            "application/typescript",
            "application/x-sh",
            "application/x-subrip",
            "application/xml",
            "application/yaml",
            "text/csv",
            "text/html",
            "text/javascript",
            "text/markdown",
            "text/plain",
            "text/vtt",
            "text/x-c++src",
            "text/x-chdr",
            "text/x-csharp",
            "text/x-csrc",
            "text/x-go",
            "text/x-java-source",
            "text/x-kotlin",
            "text/x-lua",
            "text/x-php",
            "text/x-python",
            "text/x-r-source",
            "text/x-ruby",
            "text/x-rustsrc",
            "text/x-scala",
            "text/x-sh",
            "text/x-sql",
            "text/x-swift",
            "text/xml",
            "text/yaml",
        )

    def test_json_is_rest_only(self):
        """JSON is textual but was never MCP-exposed pre-#117 -- the
        registry's explicit `surfaces` field (not a 'text/' prefix guess)
        must preserve that."""
        spec = get_spec_for_mime("application/json")
        assert spec.surfaces == frozenset({"rest"})

    # -- #124/#125/#126: long-tail formats. All REST-only (binary or,
    # for EML, raw-bytes-with-non-UTF-8-transfer-encodings) -- none of
    # these were ever MCP-eligible (mcp upload_document is inline UTF-8 text
    # only, #87 Task 3).

    def test_eml_registered_rest_only(self):
        spec = get_spec_for_mime("message/rfc822")
        assert spec is not None
        assert spec.key == "eml"
        assert spec.surfaces == frozenset({"rest"})
        assert spec.magic is None  # RFC 822 has no binary signature

    def test_epub_registered_with_zip_magic(self):
        spec = get_spec_for_mime("application/epub+zip")
        assert spec is not None
        assert spec.key == "epub"
        assert spec.surfaces == frozenset({"rest"})
        assert spec.magic == b"PK\x03\x04"

    def test_rtf_registered_with_both_mime_aliases(self):
        """application/rtf is canonical; text/rtf is the common alias -- both
        must resolve to the same spec (#126)."""
        canonical = get_spec_for_mime("application/rtf")
        alias = get_spec_for_mime("text/rtf")
        assert canonical is not None
        assert canonical.key == "rtf"
        assert alias is canonical

    def test_odt_registered_with_zip_magic(self):
        spec = get_spec_for_mime("application/vnd.oasis.opendocument.text")
        assert spec is not None
        assert spec.key == "odt"
        assert spec.surfaces == frozenset({"rest"})
        assert spec.magic == b"PK\x03\x04"


# ---------------------------------------------------------------------------
# #121: structured text (YAML, TOML, XML) -- decoded as plain text, no parse
# step, so malformed input is still searchable rather than rejected. Both
# are `rest + mcp`, `chunking_hint="structured"` per the issue.
# ---------------------------------------------------------------------------


class TestStructuredTextSpecs:
    @pytest.mark.parametrize(
        "mime,key",
        [
            ("application/yaml", "yaml"),
            ("text/yaml", "yaml"),
            ("application/toml", "toml"),
            ("application/xml", "xml"),
            ("text/xml", "xml"),
        ],
    )
    def test_declared_mime_resolves(self, mime, key):
        spec = get_spec_for_mime(mime)
        assert spec is not None
        assert spec.key == key

    @pytest.mark.parametrize(
        "ext,key",
        [(".yaml", "yaml"), (".yml", "yaml"), (".toml", "toml"), (".xml", "xml")],
    )
    def test_extension_resolves(self, ext, key):
        spec = get_spec_for_extension(ext)
        assert spec is not None
        assert spec.key == key

    @pytest.mark.parametrize("key", ["yaml", "toml", "xml"])
    def test_chunking_hint_is_structured(self, key):
        """Configs/specs/API payloads are structured data, not prose --
        #129's future format-aware chunker branches on this."""
        assert get_spec_by_key(key).chunking_hint == "structured"

    @pytest.mark.parametrize("key", ["yaml", "toml", "xml"])
    def test_mcp_eligible(self, key):
        """rest+mcp per #121: agents relabeling configs as text/plain to get
        them past upload should no longer need to."""
        assert "mcp" in get_spec_by_key(key).surfaces

    @pytest.mark.parametrize("key", ["yaml", "toml", "xml"])
    def test_no_magic_signature(self, key):
        """Free-form text formats have nothing to sniff for -- same as every
        other text/* entry (see FileTypeSpec.magic's docstring)."""
        assert get_spec_by_key(key).magic is None


# ---------------------------------------------------------------------------
# #122: source code -- extension allowlist is the source of truth; MIME
# aliases are an accepted convenience layer on top of it, and the client's
# declared value is preserved verbatim in stored content_type (asserted in
# inh-public-api-svc's test_upload_document.py, which controls storage).
# ---------------------------------------------------------------------------


class TestSourceCodeSpec:
    # The exact 21-extension allowlist named in #122's proposed contract.
    EXTENSIONS = (
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
    )

    def test_every_listed_extension_is_registered(self):
        """Backs the README's 'code files are ingested as text' claim (#122)
        with an actual, enumerated test instead of an accidental
        text/plain-sniffing side effect."""
        for ext in self.EXTENSIONS:
            spec = get_spec_for_extension(ext)
            assert spec is not None, f"{ext} not registered"
            assert spec.key == "code"

    def test_extension_tuple_matches_the_proposed_contract_exactly(self):
        spec = get_spec_by_key("code")
        assert spec.extensions == self.EXTENSIONS

    @pytest.mark.parametrize(
        "mime",
        ["text/x-python", "application/javascript", "application/x-sh", "application/sql"],
    )
    def test_explicit_mime_aliases_from_the_issue_resolve(self, mime):
        spec = get_spec_for_mime(mime)
        assert spec is not None
        assert spec.key == "code"

    def test_mcp_eligible(self):
        assert "mcp" in get_spec_by_key("code").surfaces

    def test_no_magic_signature(self):
        assert get_spec_by_key("code").magic is None


# ---------------------------------------------------------------------------
# #197: `mime_types[0]` is a fine stand-in for "the" MIME type of a spec that
# describes exactly ONE format (every spec except "code") -- it is NOT a
# stand-in for a SPECIFIC extension when a spec's `mime_types` is actually a
# POOL of aliases spanning many distinct languages. Before this fix, every
# code file uploaded over MCP with `content_type` omitted resolved to
# `mime_types[0]` == "text/x-python" regardless of its real language --
# verified in the issue: app.js, lib.go, Main.java, q.sql, s.sh, x.rs all
# labelled "text/x-python". `mime_type_for_extension` is the fix: it
# consults `FileTypeSpec.mime_type_by_extension` (a per-extension override,
# populated only for the "code" spec) instead of blindly taking index 0.
# ---------------------------------------------------------------------------


class TestMimeTypeForExtension:
    # The issue's own repro list, verbatim (#197 "Verified: app.js, lib.go,
    # Main.java, q.sql, s.sh, x.rs all resolve to text/x-python"), plus one
    # representative entry for every other registered extension so a future
    # addition to the code spec can't silently reintroduce the bug for just
    # the NEW extension while this test keeps passing for the old ones.
    EXTENSION_TO_MIME = {
        ".py": "text/x-python",
        ".js": "text/javascript",
        ".ts": "application/typescript",
        ".tsx": "application/typescript",
        ".jsx": "text/javascript",
        ".go": "text/x-go",
        ".java": "text/x-java-source",
        ".rs": "text/x-rustsrc",
        ".c": "text/x-csrc",
        ".h": "text/x-chdr",
        ".cpp": "text/x-c++src",
        ".cs": "text/x-csharp",
        ".rb": "text/x-ruby",
        ".php": "text/x-php",
        ".swift": "text/x-swift",
        ".kt": "text/x-kotlin",
        ".scala": "text/x-scala",
        ".sh": "application/x-sh",
        ".sql": "application/sql",
        ".r": "text/x-r-source",
        ".lua": "text/x-lua",
    }

    @pytest.mark.parametrize("extension,expected_mime", list(EXTENSION_TO_MIME.items()))
    def test_code_extension_resolves_to_its_own_specific_mime(self, extension, expected_mime):
        spec = get_spec_by_key("code")
        assert mime_type_for_extension(spec, extension) == expected_mime

    def test_go_file_no_longer_mislabelled_as_python(self):
        """The exact regression named in the issue title."""
        spec = get_spec_by_key("code")
        assert mime_type_for_extension(spec, ".go") == "text/x-go"
        assert mime_type_for_extension(spec, ".go") != "text/x-python"

    def test_extension_without_leading_dot_is_tolerated(self):
        """Matches `get_spec_for_extension`'s own with-or-without-dot
        tolerance."""
        spec = get_spec_by_key("code")
        assert mime_type_for_extension(spec, "go") == mime_type_for_extension(spec, ".go")

    def test_extension_is_case_insensitive(self):
        spec = get_spec_by_key("code")
        assert mime_type_for_extension(spec, ".GO") == "text/x-go"

    def test_single_format_spec_falls_back_to_mime_types_zero(self):
        """A spec with no override (every format except "code") keeps its
        pre-#197 behaviour exactly: `mime_types[0]` unconditionally."""
        pdf_spec = get_spec_by_key("pdf")
        assert pdf_spec.mime_type_by_extension is None
        assert mime_type_for_extension(pdf_spec, ".pdf") == pdf_spec.mime_types[0]

    def test_unmapped_extension_on_a_multi_mime_spec_falls_back_to_index_zero(self):
        """Defensive: an extension the spec DOES declare but whose override
        map (hypothetically) omits it must not raise -- it degrades to the
        pre-#197 behaviour for that one extension rather than crashing the
        upload path."""
        spec = get_spec_by_key("code")
        assert spec.mime_type_by_extension is not None
        # Drop the ".py" pair -- mime_type_by_extension is a tuple of pairs
        # (see its docstring for why: hashable/immutable, matching every
        # other field on this frozen dataclass), so "trimming" it means
        # filtering the tuple, not popping a dict key.
        trimmed = tuple(pair for pair in spec.mime_type_by_extension if pair[0] != ".py")
        stub = FileTypeSpec(
            key="code-stub",
            mime_types=spec.mime_types,
            extensions=spec.extensions,
            magic=None,
            surfaces=spec.surfaces,
            extractor=spec.extractor,
            chunking_hint=spec.chunking_hint,
            mime_type_by_extension=trimmed,
        )
        assert mime_type_for_extension(stub, ".py") == stub.mime_types[0]


# ---------------------------------------------------------------------------
# #122: the octet-stream extension-fallback contract this issue asks for --
# completing the design `extensions` was RESERVED for (see the field's
# docstring in file_types.py before this change): "reserved as a fallback
# classifier for a generic/absent content-type ... not yet consulted".
# `get_spec_for_upload` is that consultation, finally wired in.
# ---------------------------------------------------------------------------


class TestGetSpecForUpload:
    def test_explicit_registered_mime_resolves_without_touching_the_extension(self):
        """The common case: a real, registered MIME type is authoritative on
        its own -- the filename is irrelevant."""
        spec = get_spec_for_upload("text/x-python", "anything.xyz")
        assert spec is not None
        assert spec.key == "code"

    def test_octet_stream_plus_known_extension_falls_back(self):
        """The #122 acceptance criterion verbatim: 'upload as
        application/octet-stream with .py extension succeeds via fallback'."""
        spec = get_spec_for_upload("application/octet-stream", "main.py")
        assert spec is not None
        assert spec.key == "code"

    def test_empty_content_type_plus_known_extension_falls_back(self):
        """An absent Content-Type is as generic as octet-stream -- the other
        half of 'generic/absent' named in the `extensions` field docstring."""
        spec = get_spec_for_upload("", "main.py")
        assert spec is not None
        assert spec.key == "code"

    def test_octet_stream_with_unrecognized_extension_stays_none(self):
        """The fallback does not swallow content_type validation: an
        extension this registry does not know about is not evidence of
        anything, so it must still be rejected upstream."""
        assert get_spec_for_upload("application/octet-stream", "notes.xyz") is None

    def test_octet_stream_with_no_extension_stays_none(self):
        assert get_spec_for_upload("application/octet-stream", "README") is None

    def test_octet_stream_with_extension_of_an_mcp_ineligible_type_still_falls_back(self):
        """The fallback is generic across the registry, not source-code-
        specific -- any registered extension qualifies, e.g. PDF's (a
        BINARY format, included here to show the fallback is not itself the
        gate that decides binary-vs-text; sniffing/size/etc downstream still
        apply exactly as they do for an explicitly-declared 'application/pdf')."""
        spec = get_spec_for_upload("application/octet-stream", "report.pdf")
        assert spec is not None
        assert spec.key == "pdf"

    def test_unrecognized_specific_mime_is_not_widened_by_extension(self):
        """SECURITY: the fallback applies ONLY to a generic/absent
        content-type, never to a SPECIFIC-but-unregistered one. Otherwise
        every unknown text file could sneak past validation just by having a
        registered-looking extension -- the exact over-broad-allowlist risk
        this contract must not introduce. A client that declares a real,
        wrong, specific MIME type is still flatly rejected."""
        assert get_spec_for_upload("application/x-something-made-up", "main.py") is None

    def test_no_extension_and_generic_content_type_stays_none(self):
        assert get_spec_for_upload("application/octet-stream", "unnamed") is None


# ---------------------------------------------------------------------------
# #127: SRT / WebVTT subtitle transcripts.
# ---------------------------------------------------------------------------


class TestSubtitleSpecs:
    def test_srt_resolves(self):
        spec = get_spec_for_mime("application/x-subrip")
        assert spec is not None
        assert spec.key == "srt"
        assert spec.extensions == (".srt",)

    def test_vtt_resolves(self):
        spec = get_spec_for_mime("text/vtt")
        assert spec is not None
        assert spec.key == "vtt"
        assert spec.extensions == (".vtt",)

    @pytest.mark.parametrize("key", ["srt", "vtt"])
    def test_chunking_hint_is_prose(self, key):
        """Per #127: cue text becomes prose after extraction, so it takes
        the default chunking treatment like txt/markdown/html."""
        assert get_spec_by_key(key).chunking_hint == "prose"

    @pytest.mark.parametrize("key", ["srt", "vtt"])
    def test_mcp_eligible(self, key):
        assert "mcp" in get_spec_by_key(key).surfaces


# ---------------------------------------------------------------------------
# sniff_content_type -- the hole #117 closes
# ---------------------------------------------------------------------------


class TestSniffContentType:
    def test_unregistered_declared_type_raises_unknown(self):
        with pytest.raises(UnknownContentTypeError):
            sniff_content_type(b"whatever", "application/x-msdownload")

    def test_correctly_labeled_text_passes(self):
        spec = sniff_content_type(b"hello world", "text/plain")
        assert spec.key == "txt"

    def test_correctly_labeled_pdf_passes(self):
        spec = sniff_content_type(b"%PDF-1.4\n...", "application/pdf")
        assert spec.key == "pdf"

    def test_pdf_labeled_bytes_that_are_not_pdf_are_rejected(self):
        """Declared PDF, but the bytes don't start with the PDF signature."""
        with pytest.raises(ContentTypeMismatchError):
            sniff_content_type(b"not actually a pdf", "application/pdf")

    def test_png_bytes_declared_as_text_plain_are_rejected(self):
        """The exact scenario named in the #117 acceptance criteria: a
        mislabeled binary (PNG bytes as text/plain) must be rejected."""
        png_magic = b"\x89PNG\r\n\x1a\n" + b"rest of a fake png"
        with pytest.raises(ContentTypeMismatchError) as exc_info:
            sniff_content_type(png_magic, "text/plain")
        assert "png" in str(exc_info.value)

    def test_png_bytes_declared_as_pdf_are_rejected(self):
        png_magic = b"\x89PNG\r\n\x1a\n" + b"rest of a fake png"
        with pytest.raises(ContentTypeMismatchError):
            sniff_content_type(png_magic, "application/pdf")

    @pytest.mark.parametrize("mime", ["application/yaml", "application/toml", "application/xml"])
    def test_png_bytes_declared_as_structured_text_are_rejected(self, mime):
        """#121 failure path: PNG bytes declared as YAML/TOML/XML must be
        rejected exactly like the pre-existing text/plain case above --
        these formats have no magic signature of their own (free-form text),
        but still lose the cross-spec check against a REAL binary
        signature."""
        png_magic = b"\x89PNG\r\n\x1a\n" + b"rest of a fake png"
        with pytest.raises(ContentTypeMismatchError) as exc_info:
            sniff_content_type(png_magic, mime)
        assert "png" in str(exc_info.value)

    def test_resolved_spec_param_skips_the_internal_mime_lookup(self):
        """#122: the octet-stream extension-fallback path already resolved a
        spec via `get_spec_for_upload` (declared_mime alone would not
        resolve -- it's generic). Passing that spec in via `resolved_spec`
        must be honored instead of re-deriving (and failing to find) one
        from `declared_mime`."""
        spec = get_spec_for_upload("application/octet-stream", "main.py")
        assert spec is not None
        result = sniff_content_type(b"print('hi')", "application/octet-stream", resolved_spec=spec)
        assert result.key == "code"

    def test_resolved_spec_param_still_catches_a_real_binary_mismatch(self):
        """The fallback path must not become a validation bypass: content
        whose bytes match a DIFFERENT registered binary signature is still
        rejected even when `resolved_spec` was supplied via the extension
        fallback."""
        spec = get_spec_for_upload("application/octet-stream", "main.py")
        png_magic = b"\x89PNG\r\n\x1a\n" + b"rest of a fake png"
        with pytest.raises(ContentTypeMismatchError):
            sniff_content_type(png_magic, "application/octet-stream", resolved_spec=spec)

    def test_short_content_shorter_than_magic_is_rejected_not_crashed(self):
        """A 2-byte upload declared as PDF must not raise IndexError/etc --
        `bytes.startswith` handles short buffers safely, this pins that the
        wrapper doesn't break that."""
        with pytest.raises(ContentTypeMismatchError):
            sniff_content_type(b"%P", "application/pdf")

    def test_correctly_labeled_docx_passes(self):
        spec = sniff_content_type(
            b"PK\x03\x04rest of a real docx zip",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        assert spec.key == "docx"

    # -- BLOCKER 3: bounded-prefix signature scan, not strict byte-0 match --
    # This repo's own pypdf parses each of these leading-junk PDFs to a real
    # page (verified directly against pypdf.PdfReader, not asserted blind);
    # a strict startswith() rejected uploads that worked fine end-to-end.

    def test_pdf_with_leading_blank_line_accepted(self):
        content = b"\n\n%PDF-1.4\n%useful pdf content follows"
        spec = sniff_content_type(content, "application/pdf")
        assert spec.key == "pdf"

    def test_pdf_with_leading_utf8_bom_accepted(self):
        content = b"\xef\xbb\xbf%PDF-1.4\n%useful pdf content follows"
        spec = sniff_content_type(content, "application/pdf")
        assert spec.key == "pdf"

    def test_pdf_with_leading_whitespace_accepted(self):
        content = b"   %PDF-1.7\n%useful pdf content follows"
        spec = sniff_content_type(content, "application/pdf")
        assert spec.key == "pdf"

    def test_pdf_signature_must_still_appear_somewhere_in_the_window(self):
        """The bounded-prefix tolerance is not "accept anything declared
        PDF" -- content with NO PDF signature anywhere in the sniff window
        is still rejected."""
        with pytest.raises(ContentTypeMismatchError):
            sniff_content_type(b"this text never contains the pdf marker at all", "application/pdf")

    # -- BLOCKER 4: shared-magic-prefix formats (the OOXML/ZIP family) must
    # not mutually reject each other the moment a sibling registers. This is
    # the load-bearing test #118 (XLSX)/#119 (PPTX)/#130 (ZIP) all rely on:
    # registering a new spec with the SAME magic as an existing one must
    # leave the EXISTING one (docx) still valid, not newly broken.

    def test_shared_magic_family_does_not_mutually_reject(self, monkeypatch):
        """Simulates #118 landing: register a synthetic 'xlsx' spec with the
        identical ZIP signature docx already uses, then confirm BOTH a real
        DOCX-declared-as-DOCX and a real XLSX-declared-as-XLSX still sniff
        clean -- neither one takes the other down."""
        import inh_contracts.file_types as ft

        docx_spec = get_spec_by_key("docx")
        xlsx_spec = FileTypeSpec(
            key="xlsx",
            mime_types=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
            extensions=(".xlsx",),
            magic=b"PK\x03\x04",  # identical signature -- the whole point
            surfaces=frozenset({"rest"}),
            extractor="xlsx",
            chunking_hint="tabular",
        )
        monkeypatch.setattr(ft, "FILE_TYPE_REGISTRY", (*FILE_TYPE_REGISTRY, xlsx_spec))

        docx_result = ft.sniff_content_type(b"PK\x03\x04 real docx bytes", docx_spec.mime_types[0])
        assert docx_result.key == "docx"

        xlsx_result = ft.sniff_content_type(b"PK\x03\x04 real xlsx bytes", xlsx_spec.mime_types[0])
        assert xlsx_result.key == "xlsx"

    # -- #124/#125/#126 SPECIFIC ASK: verify by hand that registering the
    # real EPUB and ODT specs (both PK\x03\x04, same family as docx) does
    # NOT break DOCX validation -- the exact regression #117's shared-magic
    # fix (test_shared_magic_family_does_not_mutually_reject above) exists
    # to prevent, exercised here against the REAL registry (not a synthetic
    # monkeypatched sibling) now that epub/odt actually landed.

    def test_docx_still_validates_with_epub_and_odt_registered(self):
        """DOCX, EPUB, and ODT all share the PK\\x03\\x04 ZIP signature.
        Registering epub/odt must not make docx-declared uploads start
        failing -- each of the three still sniffs clean as itself."""
        docx = sniff_content_type(
            b"PK\x03\x04 real docx bytes",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        assert docx.key == "docx"

        epub = sniff_content_type(b"PK\x03\x04 real epub bytes", "application/epub+zip")
        assert epub.key == "epub"

        odt = sniff_content_type(
            b"PK\x03\x04 real odt bytes", "application/vnd.oasis.opendocument.text"
        )
        assert odt.key == "odt"

    def test_shared_magic_family_still_rejects_a_genuinely_different_binary(self, monkeypatch):
        """The overlap tolerance is family-scoped, not "PDF/PNG can now claim
        to be a docx" -- a real cross-family mismatch is still caught even
        with the synthetic xlsx sibling present."""
        import inh_contracts.file_types as ft

        docx_spec = get_spec_by_key("docx")
        xlsx_spec = FileTypeSpec(
            key="xlsx",
            mime_types=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
            extensions=(".xlsx",),
            magic=b"PK\x03\x04",
            surfaces=frozenset({"rest"}),
            extractor="xlsx",
            chunking_hint="tabular",
        )
        monkeypatch.setattr(ft, "FILE_TYPE_REGISTRY", (*FILE_TYPE_REGISTRY, xlsx_spec))

        with pytest.raises(ContentTypeMismatchError):
            ft.sniff_content_type(b"\x89PNG\r\n\x1a\n fake png bytes", docx_spec.mime_types[0])

    # -- #126 review item 5: RTF's magic ("{\rtf") is plausible ENGLISH
    # PROSE, unlike PDF's "%PDF-" -- a full 1024-byte substring search
    # false-positives on ordinary text that merely discusses RTF. RTF's
    # `magic_anchor_window` must keep real RTF files working while no longer
    # rejecting prose that mentions "{\rtf" outside the first few bytes.

    def test_real_rtf_file_still_sniffs_clean(self):
        spec = sniff_content_type(b"{\\rtf1\\ansi\\deff0 hello world}", "application/rtf")
        assert spec.key == "rtf"

    def test_prose_mentioning_rtf_signature_is_not_mislabeled_as_rtf(self):
        """The exact scenario the review caught: a markdown/text file
        EXPLAINING the RTF format, declared as its real type, must not be
        rejected just because the string '{\\rtf1' appears somewhere past
        the anchored window."""
        content = (
            b"RTF files begin with the control word {\\rtf1\\ansi -- "
            b"here is why that matters for parsers."
        )
        spec = sniff_content_type(content, "text/plain")
        assert spec.key == "txt"

    def test_prose_mentioning_rtf_signature_declared_as_markdown_is_not_mislabeled(self):
        content = b"# About RTF\n\nRTF files begin with {\\rtf1\\ansi in the header."
        spec = sniff_content_type(content, "text/markdown")
        assert spec.key == "markdown"

    def test_rtf_declared_but_bytes_are_not_rtf_still_rejected(self):
        """The anchor narrows the window, it doesn't remove the check --
        content genuinely not RTF, declared as RTF, is still caught."""
        with pytest.raises(ContentTypeMismatchError):
            sniff_content_type(b"this is definitely not an rtf file at all", "application/rtf")

    def test_rtf_with_leading_bom_within_anchor_window_still_accepted(self):
        content = b"\xef\xbb\xbf{\\rtf1\\ansi hello"
        spec = sniff_content_type(content, "application/rtf")
        assert spec.key == "rtf"


# ---------------------------------------------------------------------------
# check_extension_consistency -- the third leg of the sniffing story.
# sniff_content_type compares BYTES against the DECLARED type; this compares
# the FILENAME against the DECLARED type. Between the two, any disagreement
# among {declared type, filename, actual bytes} is caught by at least one
# check (see document_intake.py's docstring for the full argument).
# ---------------------------------------------------------------------------


class TestCheckExtensionConsistency:
    def test_matching_extension_passes(self):
        spec = get_spec_for_mime("application/pdf")
        check_extension_consistency("report.pdf", spec)  # must not raise

    def test_mismatched_known_extension_rejected(self):
        """The '.pdf' extension is a BINARY (magic-bearing) format, so
        declaring it 'text/plain' is a real, actionable disagreement."""
        spec = get_spec_for_mime("text/plain")
        with pytest.raises(ExtensionMismatchError) as exc_info:
            check_extension_consistency("report.pdf", spec)
        assert "pdf" in str(exc_info.value)
        assert "txt" in str(exc_info.value)

    # -- BLOCKER 1: TEXT-format extensions (magic is None) must NEVER reject,
    # regardless of which text/* type is declared. text/plain is a truthful,
    # IANA-valid Content-Type for Markdown/CSV/HTML uploads (text/markdown
    # was only registered in 2016; plenty of clients and OS mime databases
    # still emit text/plain for any text file) -- these are exactly the
    # correctly-labeled uploads the #117 review caught being rejected.

    @pytest.mark.parametrize(
        "filename,declared_mime",
        [
            ("README.md", "text/plain"),
            ("data.csv", "text/plain"),
            ("page.html", "text/plain"),
            ("cfg.json", "text/plain"),
            ("notes.txt", "text/markdown"),
            ("notes.txt", "text/csv"),
        ],
    )
    def test_sibling_text_extension_is_accepted(self, filename, declared_mime):
        """Every text-format extension (magic is None) declared as any
        other text-format type must be accepted -- the exact scenarios named
        in the #117 review's BLOCKER 1."""
        spec = get_spec_for_mime(declared_mime)
        check_extension_consistency(filename, spec)  # must not raise

    def test_binary_extension_declared_as_text_is_still_rejected(self):
        """The check still catches a REAL contradiction: a '.pdf' (binary,
        magic-bearing) file declared as any text/* type."""
        for declared_mime in ("text/plain", "text/markdown", "text/csv"):
            spec = get_spec_for_mime(declared_mime)
            with pytest.raises(ExtensionMismatchError):
                check_extension_consistency("report.pdf", spec)

    def test_text_extension_declared_as_binary_is_not_caught_here(self):
        """The mirror case (declared PDF, filename says .txt) is NOT caught
        by this function -- .txt has no magic, so it never triggers the
        check. It IS still caught overall, by `sniff_content_type`'s byte
        sniff (real text bytes won't match the PDF signature) -- this
        function is one of two checks, not the only one."""
        spec = get_spec_for_mime("application/pdf")
        check_extension_consistency("notes.txt", spec)  # must not raise

    def test_unknown_extension_is_not_an_error(self):
        """An extension the registry doesn't recognize (e.g. a format #117
        doesn't cover yet) must NOT be rejected on that basis alone --
        content_type is the authoritative signal; an unrecognized extension
        is simply not evidence of anything."""
        spec = get_spec_for_mime("text/plain")
        check_extension_consistency("notes.xyz", spec)  # must not raise

    def test_no_extension_is_not_an_error(self):
        """A filename with no extension at all (e.g. the REST route's
        'unnamed' fallback) must not be rejected -- there's nothing to
        compare."""
        spec = get_spec_for_mime("text/plain")
        check_extension_consistency("unnamed", spec)  # must not raise

    def test_case_insensitive_extension_match(self):
        spec = get_spec_for_mime("application/pdf")
        check_extension_consistency("REPORT.PDF", spec)  # must not raise

    # -- #126 review item 6: RTF has a `magic` (needed for sniffing) but is
    # genuinely ASCII text, not a binary container -- it belongs in the same
    # "never rejected here" bucket as .txt/.md/.csv/.html, via
    # `extension_check_exempt`, even though `magic is not None` for it.

    def test_rtf_extension_declared_as_text_plain_is_accepted(self):
        spec = get_spec_for_mime("text/plain")
        check_extension_consistency("notes.rtf", spec)  # must not raise

    def test_rtf_extension_declared_as_markdown_is_accepted(self):
        spec = get_spec_for_mime("text/markdown")
        check_extension_consistency("notes.rtf", spec)  # must not raise

    def test_rtf_extension_declared_as_its_own_type_still_passes(self):
        spec = get_spec_for_mime("application/rtf")
        check_extension_consistency("report.rtf", spec)  # must not raise

    def test_genuinely_binary_extension_still_rejected_alongside_exempt_rtf(self):
        """The RTF exemption is scoped to RTF -- a real binary extension
        (.pdf) declared as a mismatched type is still caught."""
        spec = get_spec_for_mime("text/plain")
        with pytest.raises(ExtensionMismatchError):
            check_extension_consistency("report.pdf", spec)


# ---------------------------------------------------------------------------
# Docs generation
# ---------------------------------------------------------------------------


class TestRenderMarkdownTable:
    def test_renders_a_row_per_registry_entry(self):
        table = render_markdown_table()
        for spec in FILE_TYPE_REGISTRY:
            assert spec.key in table

    def test_renders_valid_markdown_table_header(self):
        table = render_markdown_table()
        lines = table.strip().splitlines()
        assert lines[0].startswith("|")
        assert set(lines[1].replace("|", "").strip()) <= {"-", " "}

    def test_deterministic_across_calls(self):
        """Docs generation must be reproducible -- a diff-only script run
        must never produce spurious churn from nondeterministic ordering
        (e.g. iterating a set)."""
        assert render_markdown_table() == render_markdown_table()


# ---------------------------------------------------------------------------
# EXPLICITLY_UNSUPPORTED -- deliberately-rejected formats with a real
# replacement (#124/#126 review blocker 3). A single shared table so REST
# and MCP cannot disagree about which formats get this treatment, unlike
# the pre-fix state where each surface (or just REST) held its own copy.
# ---------------------------------------------------------------------------


class TestExplicitlyUnsupported:
    def test_doc_and_msg_are_registered(self):
        keys = {spec.key for spec in EXPLICITLY_UNSUPPORTED}
        assert {"doc", "msg"} <= keys

    def test_explicitly_unsupported_types_are_not_in_the_real_registry(self):
        """A format cannot be both accepted and explicitly rejected -- the
        two tables must never overlap on MIME type or extension."""
        registered_mimes = set(all_mime_types())
        registered_extensions = {ext for spec in FILE_TYPE_REGISTRY for ext in spec.extensions}
        for spec in EXPLICITLY_UNSUPPORTED:
            assert not (set(spec.mime_types) & registered_mimes)
            assert not (set(spec.extensions) & registered_extensions)

    def test_message_for_mime_names_the_replacement(self):
        doc_message = explicitly_unsupported_message_for_mime("application/msword")
        assert doc_message is not None
        assert ".docx" in doc_message

        msg_message = explicitly_unsupported_message_for_mime("application/vnd.ms-outlook")
        assert msg_message is not None
        assert ".eml" in msg_message

    def test_message_for_mime_is_none_for_a_registered_or_unknown_type(self):
        assert explicitly_unsupported_message_for_mime("application/pdf") is None
        assert explicitly_unsupported_message_for_mime("application/x-made-up") is None

    def test_message_for_mime_strips_content_type_parameters(self):
        message = explicitly_unsupported_message_for_mime("application/msword; charset=utf-8")
        assert message is not None
        assert ".docx" in message

    def test_message_for_extension_covers_the_content_type_omitted_case(self):
        """The exact gap #124/#126 review blocker 3 found: a surface that
        resolves content type FROM the filename (MCP upload_document with
        content_type omitted) needs the extension itself as a rejection
        key, not just the MIME type."""
        doc_message = explicitly_unsupported_message_for_extension("report.doc")
        assert doc_message is not None
        assert ".docx" in doc_message

        msg_message = explicitly_unsupported_message_for_extension("message.MSG")
        assert msg_message is not None
        assert ".eml" in msg_message

    def test_message_for_extension_is_none_for_registered_or_unknown_extension(self):
        assert explicitly_unsupported_message_for_extension("report.docx") is None
        assert explicitly_unsupported_message_for_extension("notes.xyz") is None
        assert explicitly_unsupported_message_for_extension("no-extension-at-all") is None


class TestOOXMLSiblingsFromBatch3:
    """XLSX/PPTX registry and shared-magic tests carried over from the
    #118/#119 branch during the batch-3 merge (#118, #119)."""

    def test_get_spec_for_extension_pptx(self):
        assert get_spec_for_extension(".pptx").key == "pptx"

    def test_get_spec_for_extension_xlsx(self):
        assert get_spec_for_extension(".xlsx").key == "xlsx"

    def test_get_spec_for_mime_pptx(self):
        """#119: PPTX is registered, REST-only (binary). chunking_hint is
        "structured" -- ChunkingHint has no "sections" member (the issue's
        proposed name); "structured" is the closest existing value for a
        format made of discrete addressable units (slides) rather than
        continuous prose, same rationale as json's "structured" hint."""
        spec = get_spec_for_mime(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )
        assert spec is not None
        assert spec.key == "pptx"
        assert spec.surfaces == frozenset({"rest"})
        assert spec.chunking_hint == "structured"
        assert spec.extractor == "pptx"

    def test_get_spec_for_mime_xlsx(self):
        """#118: XLSX is registered, REST-only (binary), tabular chunking."""
        spec = get_spec_for_mime(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert spec is not None
        assert spec.key == "xlsx"
        assert spec.surfaces == frozenset({"rest"})
        assert spec.chunking_hint == "tabular"
        assert spec.extractor == "xlsx"

    def test_xlsx_bytes_declared_as_docx_pass_the_byte_sniff(self):
        """The reachable case this guarantee implies: a byte sniff CANNOT
        distinguish the OOXML siblings from each other (a 4-byte ZIP header
        is all any of them has to check). Genuine XLSX bytes, declared as
        DOCX, pass `sniff_content_type` -- it resolves to the DECLARED spec
        (docx), not the true one. This is NOT a hole: `sniff_content_type`
        only proves "these bytes are plausibly a ZIP-family OOXML document",
        never "these bytes are SPECIFICALLY a .docx". Disambiguation for a
        filename-less/extension-mismatched upload is deferred to the
        extraction stage, which fails loudly instead of mis-parsing (see
        inh-ingestion-svc's test_extraction_by_type.py::
        test_genuine_xlsx_fed_to_docx_extractor_fails_loudly_not_silently)."""
        docx_spec = get_spec_by_key("docx")
        assert docx_spec is not None

        # Genuine XLSX bytes (same ZIP signature), declared as the DOCX mime.
        result = sniff_content_type(
            b"PK\x03\x04 an actual xlsx workbook's bytes", docx_spec.mime_types[0]
        )
        # Resolves to the DECLARED type -- sniff_content_type's contract is
        # "do the bytes CONTRADICT the declared type", not "identify the
        # true type". They don't contradict (same family), so it resolves
        # docx, silently wrong about the TRUE format.
        assert result.key == "docx"

    def test_xlsx_bytes_renamed_to_match_the_declared_lie_passes_both_checks(self):
        """Review follow-up: the extension check above ONLY fires when the
        filename carries a RECOGNIZED, DIFFERING extension. It does NOT fire
        when the filename is renamed to MATCH the (false) declared type --
        genuine XLSX bytes, uploaded as "report.docx" and declared as DOCX,
        pass BOTH `sniff_content_type` (same ZIP family, no contradiction)
        AND `check_extension_consistency` (the extension IS ".docx", which
        DOES match the declared docx spec -- there is nothing for this check
        to object to). This is the complete, accurate statement of the
        shared-magic guarantee's limit: renaming to match the lie reaches
        extraction exactly like the extensionless case
        test_xlsx_bytes_declared_as_docx_pass_the_byte_sniff already covers --
        it is not a narrower or rarer case, it is the SAME reachable case
        under a different, equally realistic filename. All six renamed pairs
        among {docx, xlsx, pptx} behave identically (only docx<->xlsx is
        spelled out here; the other four follow the same two-check argument
        with no format-specific difference in either function)."""
        docx_spec = get_spec_by_key("docx")
        assert docx_spec is not None

        # Genuine XLSX bytes, uploaded as "report.docx", declared as docx.
        sniff_result = sniff_content_type(
            b"PK\x03\x04 an actual xlsx workbook's bytes", docx_spec.mime_types[0]
        )
        assert sniff_result.key == "docx"  # passes -- resolves to the DECLARED type

        check_extension_consistency(
            "report.docx", docx_spec
        )  # must not raise -- ".docx" IS docx's own extension

        # The mirror case: genuine DOCX bytes, uploaded as "sheet.xlsx",
        # declared as xlsx -- same two-check pass, same underlying gap.
        xlsx_spec = get_spec_by_key("xlsx")
        assert xlsx_spec is not None
        sniff_result_2 = sniff_content_type(
            b"PK\x03\x04 an actual docx document's bytes", xlsx_spec.mime_types[0]
        )
        assert sniff_result_2.key == "xlsx"
        check_extension_consistency("sheet.xlsx", xlsx_spec)  # must not raise

    def test_xlsx_named_file_declared_as_docx_is_caught_by_extension_check(self):
        """The byte sniff alone cannot catch a mislabeled OOXML sibling, but
        `check_extension_consistency` (the THIRD signal: filename) can, and
        does, whenever the upload has a recognized, differing extension --
        the common real-world case (an uploader's filename normally matches
        its true type even when Content-Type is wrong)."""
        docx_spec = get_spec_by_key("docx")
        assert docx_spec is not None
        with pytest.raises(ExtensionMismatchError) as exc_info:
            check_extension_consistency("workbook.xlsx", docx_spec)
        assert "xlsx" in str(exc_info.value)
        assert "docx" in str(exc_info.value)
