"""Unit tests for `src.temporal.activities.docx_numbering` (#389).

Builds `numbering.xml`/style XML directly (python-docx exposes no public API
for either abstractNum level definitions or style-level numPr), then exercises
`NumberingScheme`/`resolve_paragraph_num_id_ilvl` in isolation from the
surrounding paragraph/table walk in `extract.py` -- see that module's
docstring for why this lives in its own module and its own tests.
"""

import docx
import pytest
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn

from src.temporal.activities.docx_numbering import (
    NumberingScheme,
    resolve_paragraph_num_id_ilvl,
)


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """Override the DB-backed root autouse fixture (see
    `test_extraction_by_type.py`'s identical override) -- this module needs
    neither PostgreSQL nor any other live service and must run unconditionally,
    including in local dev without Docker."""
    yield


def _set_val(parent, tag: str, value: str) -> None:
    """Append a `<w:{tag} w:val="{value}"/>` child to `parent` (an lxml
    element) -- the one-line OOXML "set an attribute-only child" shape every
    helper below needs, spelled out once."""
    child = parent.makeelement(qn(tag), {})
    child.set(qn("w:val"), value)
    parent.append(child)


def _add_abstract_num(
    numbering_element,
    abstract_id: str,
    levels: list[tuple[str, str, str]],
    legal_ilvls: set[int] | None = None,
):
    """Append one `<w:abstractNum>` with one `<w:lvl>` per
    `(num_fmt, lvl_text, start)` triple, at consecutive ilvl 0, 1, 2, ...
    `legal_ilvls` marks which of those levels also carry a bare
    `<w:isLgl/>` flag (OOXML's "render every placeholder in this level's
    pattern as decimal" marker)."""
    abstract_num = numbering_element.makeelement(qn("w:abstractNum"), {})
    abstract_num.set(qn("w:abstractNumId"), abstract_id)
    for ilvl, (num_fmt, lvl_text, start) in enumerate(levels):
        lvl = abstract_num.makeelement(qn("w:lvl"), {})
        lvl.set(qn("w:ilvl"), str(ilvl))
        _set_val(lvl, "w:start", start)
        _set_val(lvl, "w:numFmt", num_fmt)
        _set_val(lvl, "w:lvlText", lvl_text)
        if legal_ilvls and ilvl in legal_ilvls:
            lvl.append(lvl.makeelement(qn("w:isLgl"), {}))
        abstract_num.append(lvl)
    numbering_element.append(abstract_num)


def _add_num(numbering_element, num_id: str, abstract_id: str, start_overrides=None):
    """Append one `<w:num>` binding `num_id` to `abstract_id`, with optional
    `{ilvl: start}` `<w:lvlOverride>/<w:startOverride>` entries."""
    num = numbering_element.makeelement(qn("w:num"), {})
    num.set(qn("w:numId"), num_id)
    _set_val(num, "w:abstractNumId", abstract_id)
    for ilvl, start in (start_overrides or {}).items():
        override = num.makeelement(qn("w:lvlOverride"), {})
        override.set(qn("w:ilvl"), str(ilvl))
        _set_val(override, "w:startOverride", str(start))
        num.append(override)
    numbering_element.append(num)


def _set_direct_num_pr(paragraph, num_id: str, ilvl: int) -> None:
    """Give `paragraph` a direct `<w:pPr>/<w:numPr>` binding, the same shape
    Word writes for a manually-applied (not style-inherited) list item."""
    p_pr = paragraph._p.get_or_add_pPr()
    num_pr = p_pr.get_or_add_numPr()
    _set_val(num_pr, "w:ilvl", str(ilvl))
    _set_val(num_pr, "w:numId", str(num_id))


def _set_style_num_pr(style, num_id: str, ilvl: int) -> None:
    """Give a STYLE (not a paragraph) a direct numPr -- the "style-inherited
    numPr" case: a paragraph using this style gets this list binding without
    ever carrying its own numPr."""
    p_pr = style.element.get_or_add_pPr()
    num_pr = p_pr.get_or_add_numPr()
    _set_val(num_pr, "w:ilvl", str(ilvl))
    _set_val(num_pr, "w:numId", str(num_id))


def _new_document_with_numbering():
    """A fresh python-docx `Document`, returning it alongside its
    `numbering_part`'s root `<w:numbering>` element ready for
    `_add_abstract_num`/`_add_num` to populate."""
    document = docx.Document()
    numbering_element = document.part.numbering_part.element
    return document, numbering_element


class TestRenderCountFormats:
    """Each supported numFmt renders the running count correctly (#389:
    "Support numFmt decimal, lowerLetter, upperLetter, lowerRoman,
    upperRoman, bullet"). Exercised through `NumberingScheme.marker_for`
    (the only public entry point) rather than the private `_render_count`
    directly, since that IS the contract callers rely on."""

    def _single_level_scheme(self, num_fmt: str, lvl_text: str = "%1") -> NumberingScheme:
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(numbering_element, "900", [(num_fmt, lvl_text, "1")])
        _add_num(numbering_element, "900", "900")
        return NumberingScheme.from_document(document)

    def test_decimal(self):
        scheme = self._single_level_scheme("decimal")
        assert [scheme.marker_for("900", 0) for _ in range(3)] == ["1", "2", "3"]

    def test_lower_letter(self):
        scheme = self._single_level_scheme("lowerLetter")
        markers = [scheme.marker_for("900", 0) for _ in range(28)]
        assert markers[0] == "a"
        assert markers[25] == "z"
        assert markers[26] == "aa"  # bijective base-26, not plain base-26
        assert markers[27] == "ab"

    def test_upper_letter(self):
        scheme = self._single_level_scheme("upperLetter")
        assert scheme.marker_for("900", 0) == "A"

    def test_lower_roman(self):
        scheme = self._single_level_scheme("lowerRoman")
        markers = [scheme.marker_for("900", 0) for _ in range(4)]
        assert markers == ["i", "ii", "iii", "iv"]

    def test_upper_roman(self):
        scheme = self._single_level_scheme("upperRoman")
        assert scheme.marker_for("900", 0) == "I"

    def test_bullet_renders_as_dash_regardless_of_count(self):
        scheme = self._single_level_scheme("bullet", lvl_text="")
        assert scheme.marker_for("900", 0) == "-"
        assert scheme.marker_for("900", 0) == "-"  # stays "-" on repeat, no visible counting

    def test_unknown_num_fmt_falls_back_to_decimal(self):
        """A numFmt this module hasn't been taught (a real OOXML value like
        `ordinal`, or a typo in hand-built XML) degrades to plain decimal
        rather than raising (#389: "Unknown formats fall back to decimal")."""
        scheme = self._single_level_scheme("chineseCounting")
        assert scheme.marker_for("900", 0) == "1"


class TestMultiLevelCountersAndResets:
    """The counter state machine: per-level running counts, with deeper
    levels resetting when a shallower level advances (#389: "keep per-list
    counters (reset deeper levels when a shallower level increments)")."""

    def _outline_scheme(self) -> NumberingScheme:
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(
            numbering_element,
            "901",
            [
                ("decimal", "%1", "1"),
                ("decimal", "%1.%2", "1"),
                ("lowerLetter", "(%3)", "1"),
                ("lowerRoman", "(%4)", "1"),
            ],
        )
        _add_num(numbering_element, "901", "901")
        return NumberingScheme.from_document(document)

    def test_1_1_1_a_1_i_sequence_with_resets(self):
        """1 / 1.1 / (a) / (i) then back up to 2 -- exactly the #389 example
        sequence, including that returning to a shallower level resets
        every deeper counter instead of continuing from where it left off."""
        scheme = self._outline_scheme()
        assert scheme.marker_for("901", 0) == "1"
        assert scheme.marker_for("901", 1) == "1.1"
        assert scheme.marker_for("901", 2) == "(a)"
        assert scheme.marker_for("901", 3) == "(i)"
        # Back to ilvl 1 without visiting ilvl 0 again: 1.2, not 1.3 -- and
        # its own sub-levels (2, 3) must have been reset, not carried over.
        assert scheme.marker_for("901", 1) == "1.2"
        assert scheme.marker_for("901", 2) == "(a)"  # reset, not "(b)"
        # Back to ilvl 0: resets EVERY deeper level, including ilvl 1.
        assert scheme.marker_for("901", 0) == "2"
        assert scheme.marker_for("901", 1) == "2.1"

    def test_shallower_level_untouched_by_deeper_rendering(self):
        """Rendering a deep level must never itself advance a shallower
        level's counter -- only an explicit render at that shallower level
        does."""
        scheme = self._outline_scheme()
        scheme.marker_for("901", 0)  # "1"
        scheme.marker_for("901", 1)  # "1.1"
        scheme.marker_for("901", 1)  # "1.2" -- ilvl 0 must still read "1" next
        assert scheme.marker_for("901", 0) == "2"

    def test_num_ids_sharing_an_abstract_num_continue_one_shared_sequence(self):
        """Two different numId list instances against the SAME abstractNum
        are the SAME visible list in Word (it mints a fresh numId for an
        existing list after an intervening unnumbered paragraph, or on
        copy-paste) -- they must continue one shared count, not each start
        at 1 (review fix: counters are keyed by abstractNumId, not numId)."""
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(numbering_element, "902", [("decimal", "%1", "1")])
        _add_num(numbering_element, "910", "902")
        _add_num(numbering_element, "920", "902")
        scheme = NumberingScheme.from_document(document)

        assert scheme.marker_for("910", 0) == "1"
        assert scheme.marker_for("910", 0) == "2"
        # A different numId bound to the SAME abstractNum continues the
        # sequence -- 3, not a fresh 1.
        assert scheme.marker_for("920", 0) == "3"
        assert scheme.marker_for("920", 0) == "4"

    def test_num_ids_against_different_abstract_nums_stay_independent(self):
        """Per-list independence still holds when the numIds are bound to
        genuinely DIFFERENT abstractNums -- only a SHARED abstractNum
        continues the sequence."""
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(numbering_element, "906", [("decimal", "%1", "1")])
        _add_abstract_num(numbering_element, "907", [("decimal", "%1", "1")])
        _add_num(numbering_element, "960", "906")
        _add_num(numbering_element, "970", "907")
        scheme = NumberingScheme.from_document(document)

        assert scheme.marker_for("960", 0) == "1"
        assert scheme.marker_for("960", 0) == "2"
        # Bound to a different abstractNum entirely -- starts fresh at 1.
        assert scheme.marker_for("970", 0) == "1"

    def test_jumping_straight_to_a_deep_level_defaults_ancestors_to_their_start(self):
        """An ilvl-2 paragraph with no preceding ilvl-0/ilvl-1 paragraph is
        unusual but not invalid OOXML -- ancestor counters that were never
        visited must default to their own defined start, not KeyError or 0."""
        scheme = self._outline_scheme()
        assert scheme.marker_for("901", 2) == "(a)"


class TestStartOverride:
    """`<w:lvlOverride>/<w:startOverride>` -- one list INSTANCE restarting a
    level at a different number than its abstractNum's own `<w:start>`,
    without a whole separate abstractNum (#389: resolve start through
    numbering.xml's lvlOverride)."""

    def test_start_override_wins_over_abstract_num_start(self):
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(numbering_element, "903", [("decimal", "%1", "1")])
        _add_num(numbering_element, "930", "903", start_overrides={0: 5})
        scheme = NumberingScheme.from_document(document)

        assert scheme.marker_for("930", 0) == "5"
        assert scheme.marker_for("930", 0) == "6"

    def test_second_num_id_with_start_override_restarts_then_continues_shared_sequence(
        self,
    ):
        """A second numId sharing the first's abstractNum, carrying its own
        startOverride, restarts the shared sequence at that override value
        the FIRST time IT renders that level -- and every subsequent render
        (by either numId) continues on from there, not back to the
        pre-restart count (review fix)."""
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(numbering_element, "908", [("decimal", "%1", "1")])
        _add_num(numbering_element, "980", "908")
        _add_num(numbering_element, "990", "908", start_overrides={0: 1})
        scheme = NumberingScheme.from_document(document)

        assert scheme.marker_for("980", 0) == "1"
        assert scheme.marker_for("980", 0) == "2"
        # First render under "990": its startOverride restarts the shared
        # count at 1, not 3.
        assert scheme.marker_for("990", 0) == "1"
        # Second render under "990": the override was already consumed --
        # continues from the restarted value, not from the pre-restart "2".
        assert scheme.marker_for("990", 0) == "2"
        # Back to "980" (same abstractNum): still continues the one shared
        # sequence.
        assert scheme.marker_for("980", 0) == "3"


class TestIsLglDecimalOverride:
    """OOXML's `<w:isLgl/>` flag on a level: every `%N` placeholder in THAT
    level's own `lvlText` renders as plain decimal, even where the
    placeholder's own ancestor level is a different numFmt entirely (#389
    review fix)."""

    def test_islgl_renders_upper_roman_ancestor_as_decimal(self):
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(
            numbering_element,
            "909",
            [
                ("upperRoman", "%1", "1"),
                ("decimal", "%1.%2", "1"),
            ],
            legal_ilvls={1},
        )
        _add_num(numbering_element, "909", "909")
        scheme = NumberingScheme.from_document(document)

        # Level 0 (no isLgl) still renders its own ancestor format normally.
        assert scheme.marker_for("909", 0) == "I"
        # Level 1 has isLgl: %1 (the upperRoman ancestor) renders as "1", not
        # "I" -- every placeholder in this level's pattern is decimal.
        assert scheme.marker_for("909", 1) == "1.1"

    def test_islgl_does_not_affect_a_sibling_level_without_the_flag(self):
        """isLgl is per-level, not global -- a level withOUT the flag must
        keep rendering its ancestors in their own native numFmt even though
        a DEEPER level in the same list has isLgl set."""
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(
            numbering_element,
            "911",
            [
                ("upperRoman", "%1", "1"),
                ("lowerLetter", "%1.%2", "1"),
                ("decimal", "%1.%2.%3", "1"),
            ],
            legal_ilvls={2},
        )
        _add_num(numbering_element, "911", "911")
        scheme = NumberingScheme.from_document(document)

        assert scheme.marker_for("911", 0) == "I"
        # Level 1 has no isLgl -- ancestor (level 0, upperRoman) still "I".
        assert scheme.marker_for("911", 1) == "I.a"
        # Level 2 has isLgl -- both ancestors render as decimal.
        assert scheme.marker_for("911", 2) == "1.1.1"


class TestUnresolvableNumbering:
    """Every "this isn't a real/known list" shape degrades to `None`
    rather than raising (see the module docstring's "degrade, don't
    raise" contract)."""

    def test_unbound_num_id_on_default_template_returns_none(self):
        """python-docx's default template ships numId 1 already bound (a
        default bullet list) -- a numId this document never actually
        used (no `<w:num>` binding at all) must still resolve to None."""
        document = docx.Document()
        scheme = NumberingScheme.from_document(document)
        assert scheme.marker_for("99999", 0) is None

    def test_unbound_num_id_returns_none(self):
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(numbering_element, "904", [("decimal", "%1", "1")])
        # Deliberately no `_add_num` binding for numId "999".
        scheme = NumberingScheme.from_document(document)
        assert scheme.marker_for("999", 0) is None

    def test_ilvl_beyond_defined_levels_returns_none(self):
        document, numbering_element = _new_document_with_numbering()
        _add_abstract_num(numbering_element, "905", [("decimal", "%1", "1")])  # only ilvl 0
        _add_num(numbering_element, "950", "905")
        scheme = NumberingScheme.from_document(document)
        assert scheme.marker_for("950", 5) is None


class TestResolveParagraphNumIdIlvl:
    """`resolve_paragraph_num_id_ilvl`: direct numPr wins; otherwise climb
    the paragraph's style `basedOn` chain (#389: "style-inherited numPr")."""

    def test_no_numbering_at_all_returns_none(self):
        document = docx.Document()
        paragraph = document.add_paragraph("plain text")
        assert resolve_paragraph_num_id_ilvl(paragraph) is None

    def test_direct_num_pr_on_paragraph(self):
        document = docx.Document()
        paragraph = document.add_paragraph("item one")
        _set_direct_num_pr(paragraph, num_id="12", ilvl=1)
        assert resolve_paragraph_num_id_ilvl(paragraph) == ("12", 1)

    def test_style_inherited_num_pr(self):
        """A paragraph using a style that itself carries numPr, with no
        numPr of its own -- the template-driven "every paragraph in this
        style is a list item" case."""
        document = docx.Document()
        list_style = document.styles.add_style("NumberedItem", WD_STYLE_TYPE.PARAGRAPH)
        _set_style_num_pr(list_style, num_id="34", ilvl=0)

        paragraph = document.add_paragraph("inherited item", style="NumberedItem")
        assert resolve_paragraph_num_id_ilvl(paragraph) == ("34", 0)

    def test_direct_num_pr_overrides_style_num_pr(self):
        document = docx.Document()
        list_style = document.styles.add_style("NumberedItem2", WD_STYLE_TYPE.PARAGRAPH)
        _set_style_num_pr(list_style, num_id="34", ilvl=0)

        paragraph = document.add_paragraph("override", style="NumberedItem2")
        _set_direct_num_pr(paragraph, num_id="99", ilvl=2)
        assert resolve_paragraph_num_id_ilvl(paragraph) == ("99", 2)

    def test_climbs_based_on_chain_to_find_num_pr(self):
        """A style with no numPr of its own, based on a style that does,
        must still resolve -- one level of `basedOn` indirection."""
        document = docx.Document()
        base_style = document.styles.add_style("ListBase", WD_STYLE_TYPE.PARAGRAPH)
        _set_style_num_pr(base_style, num_id="55", ilvl=0)
        child_style = document.styles.add_style("ListChild", WD_STYLE_TYPE.PARAGRAPH)
        child_style.base_style = base_style

        paragraph = document.add_paragraph("child item", style="ListChild")
        assert resolve_paragraph_num_id_ilvl(paragraph) == ("55", 0)
