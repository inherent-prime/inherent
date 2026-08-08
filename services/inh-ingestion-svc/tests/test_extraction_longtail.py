"""Tests for the long-tail extractors added by #124 (EML), #125 (EPUB), and
#126 (RTF/ODT).

Written before the implementation (TESTS FIRST per CLAUDE.md): running this
file against pre-change ``extract.py`` fails with ImportError, because none
of ``_extract_eml_text`` / ``_extract_epub_text`` / ``_extract_rtf_text`` /
``_extract_odt_text`` exist yet.

Fixtures are built in-memory (stdlib ``email``/``zipfile``) rather than
checked-in binary files, so every edge case (corrupt zip, missing spine,
DRM marker, mismatched container) is a few explicit lines instead of an
opaque binary blob nobody can review in a diff.
"""

from __future__ import annotations

import io
import zipfile
from email.message import EmailMessage

import pytest
from temporalio.exceptions import ApplicationError

from src.temporal.activities.extract import (
    _extract_eml_text,
    _extract_epub_text,
    _extract_odt_text,
    _extract_rtf_text,
)


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """Override the DB-backed root autouse fixture so these stay offline
    (mirrors test_extraction_by_type.py -- no PostgreSQL needed here)."""
    yield


# ---------------------------------------------------------------------------
# #124 -- EML (RFC 822)
# ---------------------------------------------------------------------------


def _build_plain_eml(body: str = "Hello from the plain-text body.") -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Cc"] = "carol@example.com"
    msg["Date"] = "Mon, 03 Aug 2026 10:00:00 +0000"
    msg["Subject"] = "Plain text test"
    msg.set_content(body)
    return bytes(msg)


class TestExtractEmlText:
    def test_headers_extracted(self):
        """Headers (From/To/Cc/Date/Subject) are what make an email
        citable/searchable -- they must never be dropped (#124)."""
        text = _extract_eml_text(_build_plain_eml(), "mail.eml")
        assert "From: alice@example.com" in text
        assert "To: bob@example.com" in text
        assert "Cc: carol@example.com" in text
        assert "Subject: Plain text test" in text
        assert "Date:" in text

    def test_plain_body_extracted(self):
        text = _extract_eml_text(_build_plain_eml("The quick brown fox."), "mail.eml")
        assert "The quick brown fox." in text

    def test_html_only_mail_extracted_via_html_path(self):
        """No text/plain alternative -- falls back to text/html run through
        the existing HTML extractor, so markup is stripped, not dumped raw."""
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "HTML only"
        msg.set_content("<html><body><p>Rendered paragraph</p></body></html>", subtype="html")

        text = _extract_eml_text(bytes(msg), "mail.eml")
        assert "Rendered paragraph" in text
        assert "<p>" not in text
        assert "<html" not in text.lower()

    def test_multipart_alternative_prefers_plain(self):
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "Alternative parts"
        msg.set_content("Plain version wins")
        msg.add_alternative("<p>HTML version loses</p>", subtype="html")

        text = _extract_eml_text(bytes(msg), "mail.eml")
        assert "Plain version wins" in text

    def test_quoted_printable_and_attachment_decoded(self):
        """Multipart mail with an attachment (#124 acceptance fixture):
        quoted-printable body decodes correctly, headers are present, and
        the attachment is NOT silently dropped -- its filename is recorded
        in a clearly labeled section, never presented as if it were body
        content."""
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "Invoice attached"
        # Non-ASCII forces quoted-printable transfer encoding.
        msg.set_content("Café résumé — invoice attached below.")
        msg.add_attachment(
            b"%PDF-1.4 fake pdf bytes",
            maintype="application",
            subtype="pdf",
            filename="invoice.pdf",
        )

        text = _extract_eml_text(bytes(msg), "mail.eml")
        assert "Café résumé" in text  # quoted-printable decoded correctly
        assert "invoice.pdf" in text  # filename recorded
        assert "not extracted" in text  # explicitly labeled as elided, not content
        # The raw PDF bytes must never leak into the text as if they were content.
        assert "%PDF" not in text

    def test_base64_body_decoded(self):
        """Base64 transfer-encoded body must decode correctly, not appear
        as base64 gibberish in the extracted text."""
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "Base64 body"
        # cte="base64" forces base64 Content-Transfer-Encoding for the body,
        # exercising the actual decode path (not just 7bit passthrough).
        msg.set_content("Base64-encoded plain body.", cte="base64")

        text = _extract_eml_text(bytes(msg), "mail.eml")
        assert "Base64-encoded plain body." in text

    def test_no_body_still_extracts_headers(self):
        """Failure-path fixture: an .eml with headers but no body part must
        not crash -- it still yields the header block rather than raising."""
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "No body at all"
        # Deliberately do not call set_content(): no body part exists.
        raw = bytes(msg)

        text = _extract_eml_text(raw, "mail.eml")
        assert "Subject: No body at all" in text

    def test_truly_empty_message_produces_no_usable_text(self):
        """A message with no headers and no body extracts to whitespace --
        confirms the extractor doesn't fabricate content, letting the
        activity's generic empty-text guard (in _extract_text_inner) be the
        one to fail the document."""
        text = _extract_eml_text(b"", "empty.eml")
        assert text.strip() == ""

    def test_nested_message_rfc822_not_recursed_into(self):
        """#124: nested message/rfc822 parts are inspected one level only --
        a forwarded email attached as message/rfc822 is listed as an
        attachment, but its OWN nested attachments/body are not walked."""
        inner = EmailMessage()
        inner["From"] = "dave@example.com"
        inner["Subject"] = "Forwarded original"
        inner.set_content("Inner body that should not appear in the outer extraction.")
        inner.add_attachment(
            b"innerbytes",
            maintype="application",
            subtype="octet-stream",
            filename="inner-attachment.bin",
        )

        outer = EmailMessage()
        outer["From"] = "alice@example.com"
        outer["To"] = "bob@example.com"
        outer["Subject"] = "Fwd: Forwarded original"
        outer.set_content("See forwarded message below.")
        outer.add_attachment(inner.as_bytes(), maintype="message", subtype="rfc822")

        text = _extract_eml_text(bytes(outer), "mail.eml")
        assert "See forwarded message below." in text
        # The inner message's own body must not leak into the outer text.
        assert "Inner body that should not appear" not in text
        assert "inner-attachment.bin" not in text


# ---------------------------------------------------------------------------
# #125 -- EPUB
# ---------------------------------------------------------------------------


_CONTAINER_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def _opf(spine_itemrefs: str, manifest_items: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Sample Book</dc:title>
  </metadata>
  <manifest>
    {manifest_items}
  </manifest>
  <spine>
    {spine_itemrefs}
  </spine>
</package>""".encode()


def _build_epub(*, chapters: list[str], include_nav: bool = True, spine: bool = True) -> bytes:
    """Build a minimal but structurally real multi-chapter EPUB in memory."""
    manifest_items = []
    spine_refs = []
    for i, chapter_html in enumerate(chapters, start=1):
        item_id = f"chap{i}"
        manifest_items.append(
            f'<item id="{item_id}" href="chap{i}.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine_refs.append(f'<itemref idref="{item_id}"/>')

    if include_nav:
        manifest_items.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )

    opf = _opf(
        "\n".join(spine_refs) if spine else "",
        "\n".join(manifest_items),
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        for i, chapter_html in enumerate(chapters, start=1):
            zf.writestr(f"OEBPS/chap{i}.xhtml", chapter_html)
        if include_nav:
            zf.writestr(
                "OEBPS/nav.xhtml",
                "<html><body><nav>THIS SHOULD BE SKIPPED</nav></body></html>",
            )
    return buf.getvalue()


class TestExtractEpubText:
    def test_multi_chapter_spine_order_preserved(self):
        """#125 acceptance criterion: chapters extracted in SPINE order
        (not zip member order), markup stripped."""
        chapters = [
            "<html><body><h1>Chapter One</h1><p>First chapter text.</p></body></html>",
            "<html><body><h1>Chapter Two</h1><p>Second chapter text.</p></body></html>",
            "<html><body><h1>Chapter Three</h1><p>Third chapter text.</p></body></html>",
        ]
        epub_bytes = _build_epub(chapters=chapters)

        text = _extract_epub_text(epub_bytes, "book.epub")

        assert "First chapter text." in text
        assert "Second chapter text." in text
        assert "Third chapter text." in text
        # Reading order: chapter 1 must appear before 2, which must appear before 3.
        assert text.index("First chapter text.") < text.index("Second chapter text.")
        assert text.index("Second chapter text.") < text.index("Third chapter text.")
        # Markup stripped.
        assert "<h1>" not in text
        assert "<p>" not in text

    def test_nav_item_skipped(self):
        """The nav document (properties="nav") must never appear in output,
        even though it's a real zip member with real XHTML content."""
        chapters = ["<html><body><p>Real chapter content.</p></body></html>"]
        epub_bytes = _build_epub(chapters=chapters, include_nav=True)

        text = _extract_epub_text(epub_bytes, "book.epub")

        assert "Real chapter content." in text
        assert "THIS SHOULD BE SKIPPED" not in text

    def test_chapters_are_markdown_heading_delimited(self):
        """Serialization contract: '## ' per spine item, so a future
        format-aware chunker (#129) can split on chapter boundaries."""
        chapters = ["<p>Only chapter.</p>"]
        text = _extract_epub_text(_build_epub(chapters=chapters), "book.epub")
        assert "## " in text

    def test_no_spine_fails_clearly(self):
        """Failure-path fixture: an EPUB with an empty spine must fail with
        an actionable, non-retryable error (#206: deterministic given fixed
        content bytes -- Temporal must not retry it), not crash or silently
        produce empty text."""
        epub_bytes = _build_epub(chapters=[], spine=False)
        with pytest.raises(ApplicationError, match="spine") as exc_info:
            _extract_epub_text(epub_bytes, "no-spine.epub")
        assert exc_info.value.non_retryable

    # -- #125 review blocker 1: manifest hrefs are URL references. EPUB 3
    # requires spaces/non-ASCII to be percent-encoded, so a real EPUB's
    # manifest can declare href="chapter%201.xhtml" for a zip member
    # literally named "chapter 1.xhtml". Without urllib.parse.unquote,
    # zf.read() misses the real member, the chapter is silently dropped,
    # AND every later chapter renumbers down to fill the gap -- content loss
    # reported as success, with wrong citations for the rest of the book.

    def test_percent_encoded_href_resolves_and_loses_no_chapter(self):
        buf = io.BytesIO()
        manifest_items = (
            '<item id="chap1" href="chapter%201.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="chap2" href="chap2.xhtml" media-type="application/xhtml+xml"/>'
        )
        opf = _opf('<itemref idref="chap1"/><itemref idref="chap2"/>', manifest_items)
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("META-INF/container.xml", _CONTAINER_XML)
            zf.writestr("OEBPS/content.opf", opf)
            # The ACTUAL zip member has a literal space; the manifest's href
            # is percent-encoded per the EPUB OPF spec -- exactly the shape
            # that silently dropped chapter 1 pre-fix.
            zf.writestr(
                "OEBPS/chapter 1.xhtml",
                "<html><body><p>First chapter text.</p></body></html>",
            )
            zf.writestr(
                "OEBPS/chap2.xhtml",
                "<html><body><p>Second chapter text.</p></body></html>",
            )

        text = _extract_epub_text(buf.getvalue(), "book.epub")

        # Chapter 1's text must not be silently lost...
        assert "First chapter text." in text
        assert "Second chapter text." in text
        assert text.index("First chapter text.") < text.index("Second chapter text.")
        # ...and chapter 2 must not be relabeled "Chapter 1" as a result.
        assert "## Chapter 1" in text
        assert "## Chapter 2" in text

    def test_missing_manifest_item_does_not_renumber_later_chapters(self):
        """Chapters are numbered by SPINE POSITION, not by count of
        successfully-extracted chapters -- a genuinely skipped chapter
        (spine references a manifest id that does not exist) must not shift
        every later chapter's number down."""
        buf = io.BytesIO()
        # Spine has 3 itemrefs; the manifest only has entries for 1 and 3 --
        # "chap2" is a real gap (typo'd/missing manifest entry), not a
        # percent-encoding issue.
        manifest_items = (
            '<item id="chap1" href="chap1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="chap3" href="chap3.xhtml" media-type="application/xhtml+xml"/>'
        )
        opf = _opf(
            '<itemref idref="chap1"/><itemref idref="chap2"/><itemref idref="chap3"/>',
            manifest_items,
        )
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("META-INF/container.xml", _CONTAINER_XML)
            zf.writestr("OEBPS/content.opf", opf)
            zf.writestr("OEBPS/chap1.xhtml", "<html><body><p>Chapter one text.</p></body></html>")
            zf.writestr("OEBPS/chap3.xhtml", "<html><body><p>Chapter three text.</p></body></html>")

        text = _extract_epub_text(buf.getvalue(), "book.epub")

        assert "Chapter one text." in text
        assert "Chapter three text." in text
        # The surviving second chapter is genuinely spine position 3 -- it
        # must be labelled "Chapter 3", never renumbered down to "Chapter 2".
        assert "## Chapter 1" in text
        assert "## Chapter 3" in text
        assert "## Chapter 2" not in text

    def test_prefers_chapter_title_tag_over_generic_heading(self):
        chapters = [
            "<html><head><title>The Great Escape</title></head>"
            "<body><p>Escape chapter text.</p></body></html>"
        ]
        text = _extract_epub_text(_build_epub(chapters=chapters), "book.epub")
        assert "## The Great Escape" in text
        assert "Escape chapter text." in text

    def test_prefers_h1_when_no_title_tag(self):
        chapters = ["<html><body><h1>Opening Chapter</h1><p>Some text.</p></body></html>"]
        text = _extract_epub_text(_build_epub(chapters=chapters), "book.epub")
        assert "## Opening Chapter" in text

    def test_encrypted_epub_fails_clearly_not_crash(self):
        """Failure-path fixture: DRM/encrypted EPUB (signalled by the
        standard META-INF/encryption.xml manifest) must fail with a clear,
        actionable, NON-RETRYABLE error_message -- never a raw parser crash
        (#125), and never retried 3x for a deterministic failure (#206)."""
        chapters = ["<p>Encrypted chapter -- should never be reached.</p>"]
        base = _build_epub(chapters=chapters)

        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(base)) as src, zipfile.ZipFile(buf, "w") as dst:
            for item in src.infolist():
                dst.writestr(item, src.read(item.filename))
            dst.writestr(
                "META-INF/encryption.xml",
                b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"/>',
            )

        with pytest.raises(ApplicationError, match="[Ee]ncrypt|DRM") as exc_info:
            _extract_epub_text(buf.getvalue(), "protected.epub")
        assert exc_info.value.non_retryable

    def test_corrupt_zip_fails_clearly_not_crash(self):
        """Failure-path fixture: a corrupt zip must raise a clear,
        non-retryable ApplicationError (#206), never an uncaught
        zipfile.BadZipFile crash."""
        with pytest.raises(ApplicationError, match="[Cc]orrupt|zip") as exc_info:
            _extract_epub_text(b"not a zip file at all", "broken.epub")
        assert exc_info.value.non_retryable


# ---------------------------------------------------------------------------
# #126 -- RTF
# ---------------------------------------------------------------------------


class TestExtractRtfText:
    def test_headings_and_lists_extracted_no_control_word_residue(self):
        """#126 acceptance criterion: non-empty extraction with no control
        words or markup residue for a doc with headings and lists."""
        rtf = (
            r"{\rtf1\ansi\deff0"
            r"{\fonttbl{\f0 Times New Roman;}}"
            r"{\b\fs32 Document Heading\par}"
            r"{\pard\fi-360\li720 \bullet\tab First item\par}"
            r"{\pard\fi-360\li720 \bullet\tab Second item\par}"
            r"{\pard Plain paragraph text.\par}"
            r"}"
        ).encode("ascii")

        text = _extract_rtf_text(rtf, "sample.rtf")

        assert "Document Heading" in text
        assert "First item" in text
        assert "Second item" in text
        assert "Plain paragraph text." in text
        assert "\\rtf1" not in text
        assert "\\par" not in text
        assert "\\pard" not in text

    def test_nested_control_words_do_not_leak_into_output(self):
        """Failure-path fixture: nested control-word groups (bold nested
        inside italic nested inside a font-size change) must not leave any
        control-word residue in the extracted text."""
        rtf = (
            r"{\rtf1\ansi"
            r"{\fs28{\i italic wrapper {\b bold-and-italic inner} back to italic}}"
            r" trailing plain text"
            r"\par}"
        ).encode("ascii")

        text = _extract_rtf_text(rtf, "nested.rtf")

        assert "bold-and-italic inner" in text
        assert "trailing plain text" in text
        assert "\\b" not in text
        assert "\\i " not in text
        assert "\\fs28" not in text

    def test_empty_rtf_fails_clearly(self):
        """#206: deterministic given fixed content bytes -- must be a
        non-retryable ApplicationError, not a bare RuntimeError Temporal
        retries 3x for no reason."""
        with pytest.raises(ApplicationError, match="no extractable text|empty") as exc_info:
            _extract_rtf_text(r"{\rtf1\ansi\par}".encode("ascii"), "empty.rtf")
        assert exc_info.value.non_retryable


# ---------------------------------------------------------------------------
# #126 -- ODT
# ---------------------------------------------------------------------------


def _build_odt(content_xml_body: str) -> bytes:
    content_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    xmlns:dc="http://purl.org/dc/elements/1.1/">
  <office:body>
    <office:text>
      {content_xml_body}
    </office:text>
  </office:body>
</office:document-content>""".encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", content_xml)
    return buf.getvalue()


class TestExtractOdtText:
    def test_headings_and_lists_extracted_no_markup_residue(self):
        """#126 acceptance criterion: non-empty extraction with no markup
        residue for a doc with headings and lists."""
        body = (
            "<text:h>Document Heading</text:h>"
            "<text:list><text:list-item><text:p>First item</text:p></text:list-item>"
            "<text:list-item><text:p>Second item</text:p></text:list-item></text:list>"
            "<text:p>Plain paragraph text.</text:p>"
        )
        text = _extract_odt_text(_build_odt(body), "sample.odt")

        assert "Document Heading" in text
        assert "First item" in text
        assert "Second item" in text
        assert "Plain paragraph text." in text
        assert "<text:" not in text
        assert "</text:" not in text

    # -- #126 review blocker 2: content.xml is NOT "just another XML dialect
    # with human text inside element bodies" -- tracked-changes retain
    # DELETED text for revision history (not current content), and
    # office:annotation is a PRIVATE reviewer comment. Both are real ODF
    # constructs a generic tag-strip indexes indistinguishably from real
    # content, letting an agent cite retracted text as fact or leak a
    # private review comment to anyone with read access.

    def test_tracked_changes_deletion_excluded_from_output(self):
        body = (
            "<text:tracked-changes>"
            "<text:changed-region text:id='ct1'>"
            "<text:deletion>"
            "<text:p>THIS TEXT WAS DELETED and should arguably not be indexed</text:p>"
            "</text:deletion>"
            "</text:changed-region>"
            "</text:tracked-changes>"
            "<text:p>Real current paragraph text.</text:p>"
        )
        text = _extract_odt_text(_build_odt(body), "sample.odt")

        assert "Real current paragraph text." in text
        assert "THIS TEXT WAS DELETED" not in text

    def test_annotation_and_author_name_excluded_from_output(self):
        body = (
            "<text:p>Visible sentence "
            "<office:annotation>"
            "<dc:creator>Reviewer</dc:creator>"
            "<text:p>PRIVATE COMMENT</text:p>"
            "</office:annotation>"
            "continues after the note.</text:p>"
        )
        text = _extract_odt_text(_build_odt(body), "sample.odt")

        assert "Visible sentence" in text
        assert "continues after the note." in text
        assert "PRIVATE COMMENT" not in text
        assert "Reviewer" not in text

    def test_combined_tracked_changes_and_annotation_excluded(self):
        """The exact scenario from the review report: deleted text, an
        annotation body, AND the reviewer's name must all be absent, while
        real body text survives."""
        body = (
            "<text:tracked-changes>"
            "<text:changed-region text:id='ct1'>"
            "<text:deletion><text:p>Mallory deleted this</text:p></text:deletion>"
            "</text:changed-region>"
            "</text:tracked-changes>"
            "<text:p>Approved current text."
            "<office:annotation><dc:creator>Reviewer</dc:creator>"
            "<text:p>PRIVATE COMMENT</text:p></office:annotation>"
            "</text:p>"
        )
        text = _extract_odt_text(_build_odt(body), "sample.odt")

        assert "Approved current text." in text
        assert "Mallory" not in text
        assert "PRIVATE COMMENT" not in text
        assert "Reviewer" not in text

    def test_text_s_and_tab_become_whitespace_not_newlines(self):
        """Secondary defect from the same review: <text:s/> and <text:tab/>
        must become literal whitespace, not be dropped or turned into
        newlines that split one paragraph's words across several lines."""
        body = '<text:p>Word<text:s text:c="3"/>with<text:tab/>spacing</text:p>'
        text = _extract_odt_text(_build_odt(body), "sample.odt")

        # The whole phrase must survive as ONE paragraph, not be split by
        # spurious newlines -- word boundaries preserved via real whitespace.
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) == 1
        assert "Word" in lines[0]
        assert "with" in lines[0]
        assert "spacing" in lines[0]

    def test_corrupt_zip_fails_clearly_not_crash(self):
        """Failure-path fixture: a corrupt zip must raise a clear,
        non-retryable ApplicationError (#206), never an uncaught
        zipfile.BadZipFile crash."""
        with pytest.raises(ApplicationError, match="[Cc]orrupt|zip") as exc_info:
            _extract_odt_text(b"not a zip file at all", "broken.odt")
        assert exc_info.value.non_retryable

    def test_docx_payload_under_odt_extension_fails_clearly(self):
        """Failure-path fixture named explicitly in the task: a file whose
        extension says .odt but whose zip contains a DOCX payload
        (word/document.xml, no content.xml) must fail with an actionable,
        non-retryable error (#206), never silently extract garbage or
        crash."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "[Content_Types].xml",
                b'<?xml version="1.0"?><Types xmlns="fake"/>',
            )
            zf.writestr(
                "word/document.xml",
                b"<w:document><w:body><w:p>This is really a DOCX.</w:p></w:body></w:document>",
            )
        docx_shaped_bytes = buf.getvalue()

        with pytest.raises(ApplicationError, match="content.xml|not a valid ODT") as exc_info:
            _extract_odt_text(docx_shaped_bytes, "mislabeled.odt")
        assert exc_info.value.non_retryable
