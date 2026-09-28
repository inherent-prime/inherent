"""Resolves DOCX automatic (list) numbering into rendered marker text (#389).

Word never stores the visible "1.1", "(a)", "-" text a numbered/bulleted
paragraph shows on screen -- it stores a *reference* (``w:numId``/``w:ilvl``
on the paragraph, or inherited from the paragraph's style) into a numbering
*definition* (``word/numbering.xml``: an ``<w:abstractNum>`` per list
template, with one ``<w:lvl>`` per outline level giving its number format
and pattern, plus a ``<w:num>`` that binds a concrete list instance -- a
``numId`` -- to one ``abstractNum``). Word's renderer walks the document top
to bottom, keeping a running counter per level per list instance, and
renders each paragraph's marker from those counters at display time. Nothing
in ``python-docx``'s public API reproduces that renderer -- ``paragraph.text``
never includes the marker -- so plain-text extraction (``_extract_docx_text``
in ``extract.py``) silently dropped every "1.1", "(a)", "-" that made a
numbered document's structure legible, exactly the #389 issue.

This module is that missing renderer, kept separate from ``extract.py`` (and
its own test module) because it is a self-contained, order-dependent state
machine -- easy to get subtly wrong (off-by-one starts, stale deeper-level
counters) and worth testing in isolation from the surrounding
paragraph/table walk.

Two moving parts:

- ``NumberingScheme`` -- parses ``word/numbering.xml`` ONCE per document into
  an ``abstractNumId -> {ilvl -> _LevelDef}`` map plus the ``numId ->
  abstractNumId`` bindings (including any per-instance ``<w:lvlOverride>``
  start override), then holds this document's PER-ABSTRACT-NUM-ID,
  PER-level running counters as paragraphs are walked in order. Counters are
  keyed by ``abstractNumId``, not ``numId``: several ``numId``s commonly
  reference the SAME ``abstractNum`` (Word mints a fresh ``numId`` for the
  same list after an intervening unnumbered paragraph, or on copy-paste) and
  Word continues that one shared sequence across all of them -- keying by
  ``numId`` instead would restart the count at each new ``numId`` and
  renumber a single continuous list as if it were several. Docx numbering
  counters are process-order state, not a pure function of one paragraph --
  callers must call ``marker_for`` at most once per paragraph, in document
  order, exactly as the paragraphs will be walked for extraction.
- ``resolve_paragraph_num_id_ilvl`` -- resolves the ``(numId, ilvl)`` a given
  paragraph actually renders under: its OWN ``<w:pPr>/<w:numPr>`` if present,
  else the first one found climbing its style's ``basedOn`` chain (Word
  applies a list style to every paragraph using it without repeating numPr
  on each one -- the common case in template-based documents).

Both degrade to "no numbering" (``None``) rather than raising: a document
with no numbering.xml part, a numId with no matching ``<w:num>``/
``<w:abstractNum>``, or a level index the abstractNum doesn't define are all
things a hand-edited or tool-generated OOXML file can legitimately have, and
a missing marker is a much better failure mode than failing the whole
document's extraction over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from docx.oxml.ns import qn

if TYPE_CHECKING:
    from docx.oxml.text.parfmt import CT_PPr
    from docx.text.paragraph import Paragraph

# Roman numeral digit table for _to_roman, largest value first -- the
# standard subtractive-notation encoding (bounded to 1..3999: Word's own
# lvlRoman format has no defined behaviour beyond that range either, and no
# realistic outline nests 4000 levels deep).
_ROMAN_DIGITS: tuple[tuple[int, str], ...] = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


def _to_roman(n: int) -> str:
    """Render `n` (1..3999) as an uppercase Roman numeral."""
    if n <= 0:
        return str(n)  # out of range for Roman numerals -- fall back plainly
    remaining = n
    parts: list[str] = []
    for value, symbol in _ROMAN_DIGITS:
        count, remaining = divmod(remaining, value)
        parts.append(symbol * count)
    return "".join(parts)


def _to_letters(n: int) -> str:
    """Render `n` (1-based) as a bijective base-26 letter sequence:
    1='a', 2='b', ..., 26='z', 27='aa', 28='ab', ... -- matching Word's
    lowerLetter/upperLetter numbering exactly (NOT plain base-26, which has
    no representation for a run of trailing "a"s -- e.g. plain base-26 can't
    tell 'a' apart from 'aa' at position 27 the way this bijective form
    does)."""
    if n <= 0:
        return str(n)
    letters: list[str] = []
    remaining = n
    while remaining > 0:
        remaining, rem = divmod(remaining - 1, 26)
        letters.append(chr(ord("a") + rem))
    return "".join(reversed(letters))


def _render_count(count: int, num_fmt: str) -> str:
    """Render one level's running `count` per its `num_fmt`.

    Supports the formats #389 asks for; any other value (a real OOXML format
    this hasn't been taught, e.g. ``ordinal``/``chineseCounting``, or a typo
    in a hand-built fixture) falls back to plain decimal rather than raising
    -- an unfamiliar format degrading to "1, 2, 3" is a far better outcome
    than crashing extraction over a list template quirk.
    """
    if num_fmt == "decimal":
        return str(count)
    if num_fmt == "lowerLetter":
        return _to_letters(count)
    if num_fmt == "upperLetter":
        return _to_letters(count).upper()
    if num_fmt == "lowerRoman":
        return _to_roman(count).lower()
    if num_fmt == "upperRoman":
        return _to_roman(count).upper()
    return str(count)  # unknown numFmt -- decimal fallback, never a crash


@dataclass(frozen=True)
class _LevelDef:
    """One ``<w:lvl>``'s definition inside an ``<w:abstractNum>``."""

    num_fmt: str
    lvl_text: str
    start: int
    # OOXML's `<w:isLgl/>` flag: when set on a level, every `%N` placeholder
    # in ITS OWN `lvlText` renders as plain decimal, regardless of the
    # format `%N`'s own ancestor level actually defines (e.g. an ancestor
    # level formatted `upperRoman` still renders "1", not "I", inside a
    # level flagged `isLgl` -- a common outline convention for keeping
    # every segment of a multi-level number in one consistent digit style).
    decimal_override: bool = False


def _find_val(element: object, tag: str) -> str | None:
    """``element.find(qn(tag)).get(qn("w:val"))``, or None if `element` is
    None or the child is missing -- the same "absent is fine" shape every
    OOXML optional child has."""
    if element is None:
        return None
    child = element.find(qn(tag))  # type: ignore[attr-defined]
    if child is None:
        return None
    value: str | None = child.get(qn("w:val"))  # type: ignore[attr-defined]
    return value


def _find_onoff(element: object, tag: str) -> bool:
    """OOXML `ST_OnOff` convention for `element`'s `<w:{tag}>` child: absent
    entirely -> False; present with no `w:val` -> True (bare presence means
    "on"); present with a `w:val` -> True unless that value is one of the
    standard "off" spellings (`"0"`/`"false"`/`"off"`)."""
    if element is None:
        return False
    child = element.find(qn(tag))  # type: ignore[attr-defined]
    if child is None:
        return False
    val = child.get(qn("w:val"))  # type: ignore[attr-defined]
    if val is None:
        return True
    return val.lower() not in ("0", "false", "off")


class NumberingScheme:
    """Parsed ``word/numbering.xml`` plus this document's live per-numId,
    per-level counters. One instance covers exactly one document's document-
    order walk -- see the module docstring's "process-order state" note.
    """

    def __init__(self) -> None:
        # abstractNumId -> {ilvl -> _LevelDef}
        self._abstract_levels: dict[str, dict[int, _LevelDef]] = {}
        # numId -> abstractNumId
        self._num_to_abstract: dict[str, str] = {}
        # (numId, ilvl) -> overridden start value (<w:lvlOverride>/<w:startOverride>)
        self._start_overrides: dict[tuple[str, int], int] = {}
        # abstractNumId -> {ilvl -> running count}. Keyed by abstractNumId,
        # NOT numId -- see the module docstring's "several numIds, one
        # shared abstractNum" note. Mutated by `marker_for` only.
        self._counters: dict[str, dict[int, int]] = {}
        # (numId, ilvl) pairs whose startOverride has already been applied
        # once -- a startOverride only restarts the shared sequence the
        # FIRST time that specific numId renders that level; every
        # subsequent render (by this numId or any other sharing the same
        # abstractNum) just continues it.
        self._consumed_start_overrides: set[tuple[str, int]] = set()

    @classmethod
    def from_document(cls, document: object) -> NumberingScheme:
        """Build a scheme from `document`'s numbering part, or an empty
        (always-``None``) scheme if it has none -- a document with no lists
        at all often has no numbering.xml relationship, and that must be a
        normal, silent case, not an error (see the module docstring)."""
        scheme = cls()
        numbering_part = getattr(document.part, "numbering_part", None)  # type: ignore[attr-defined]
        if numbering_part is None:
            return scheme
        root = numbering_part.element
        scheme._parse_abstract_nums(root)
        scheme._parse_num_bindings(root)
        return scheme

    def _parse_abstract_nums(self, root: object) -> None:
        for abstract_num in root.findall(qn("w:abstractNum")):  # type: ignore[attr-defined]
            abstract_id = abstract_num.get(qn("w:abstractNumId"))
            if abstract_id is None:
                continue
            levels: dict[int, _LevelDef] = {}
            for lvl in abstract_num.findall(qn("w:lvl")):
                ilvl_attr = lvl.get(qn("w:ilvl"))
                if ilvl_attr is None:
                    continue
                num_fmt = _find_val(lvl, "w:numFmt") or "decimal"
                lvl_text = _find_val(lvl, "w:lvlText") or ""
                start_raw = _find_val(lvl, "w:start")
                start = int(start_raw) if start_raw is not None else 1
                decimal_override = _find_onoff(lvl, "w:isLgl")
                levels[int(ilvl_attr)] = _LevelDef(
                    num_fmt=num_fmt,
                    lvl_text=lvl_text,
                    start=start,
                    decimal_override=decimal_override,
                )
            self._abstract_levels[abstract_id] = levels

    def _parse_num_bindings(self, root: object) -> None:
        for num in root.findall(qn("w:num")):  # type: ignore[attr-defined]
            num_id = num.get(qn("w:numId"))
            if num_id is None:
                continue
            abstract_id = _find_val(num, "w:abstractNumId")
            if abstract_id is not None:
                self._num_to_abstract[num_id] = abstract_id
            # <w:lvlOverride w:ilvl="N"><w:startOverride w:val="S"/></w:lvlOverride>
            # -- a single list instance restarting (or not) a level without a
            # whole new abstractNum. Only startOverride is supported (#389
            # scope); a lvlOverride with its own full <w:lvl> replacement is
            # rare enough (and unrelated to the numbering *format*, only to
            # per-instance start) to leave for a follow-up if ever needed.
            for override in num.findall(qn("w:lvlOverride")):
                ilvl_attr = override.get(qn("w:ilvl"))
                if ilvl_attr is None:
                    continue
                start_raw = _find_val(override, "w:startOverride")
                if start_raw is not None:
                    self._start_overrides[(num_id, int(ilvl_attr))] = int(start_raw)

    def _level_def(self, num_id: str, ilvl: int) -> _LevelDef | None:
        abstract_id = self._num_to_abstract.get(num_id)
        if abstract_id is None:
            return None
        return self._abstract_levels.get(abstract_id, {}).get(ilvl)

    def marker_for(self, num_id: str, ilvl: int) -> str | None:
        """Advance the shared counters for one paragraph rendered under
        `num_id` at `ilvl`, and return its rendered marker text (e.g.
        ``"1.1"``, ``"(a)"``, ``"-"``), or ``None`` if `num_id`/`ilvl`
        doesn't resolve to a known list level at all.

        Call EXACTLY ONCE per numbered paragraph, in the document's own
        top-to-bottom order -- this mutates the running counters shared by
        every ``numId`` bound to the same ``abstractNum`` (see the class/
        module docstrings).

        Counter rules (Word's own outline-numbering behaviour):
        - The level actually being rendered always advances by 1 from its
          previous value FOR THIS ABSTRACTNUM (shared across every numId
          bound to it), or starts fresh the first time this (abstractNum,
          ilvl) pair is seen -- see the next rule for exactly where "fresh"
          comes from.
        - A ``numId`` carrying a ``startOverride`` for this level RESTARTS
          the shared counter at that override value, but only the FIRST
          time THIS numId renders THIS level -- every later render (by this
          numId or any other one sharing the abstractNum) just continues
          the sequence from there. With no override at all, "fresh" means
          the abstractNum's own defined `start`.
        - Every counter for a DEEPER level (ilvl' > ilvl) is dropped so that
          level restarts at its own `start` the next time it appears --
          moving up a level always resets everything below it, e.g. 1.1,
          1.2, 2 (not 2.3) when the second top-level item follows sub-items.
        - Counters for SHALLOWER levels (ilvl' < ilvl) are left completely
          untouched -- a sub-level rendering doesn't advance its parent.
        """
        level = self._level_def(num_id, ilvl)
        if level is None:
            return None
        abstract_id = self._num_to_abstract[num_id]
        counters = self._counters.setdefault(abstract_id, {})

        override_key = (num_id, ilvl)
        has_unconsumed_override = (
            override_key in self._start_overrides
            and override_key not in self._consumed_start_overrides
        )
        if has_unconsumed_override:
            counters[ilvl] = self._start_overrides[override_key]
            self._consumed_start_overrides.add(override_key)
        else:
            counters[ilvl] = counters.get(ilvl, level.start - 1) + 1

        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]

        if level.num_fmt == "bullet":
            # Bullets have no meaningful running count (#389 requirement:
            # "bullet (render as -)") -- the counter bump/reset above still
            # ran, for consistency, but the rendered marker ignores it.
            return "-"

        text = level.lvl_text
        for i in range(ilvl + 1):
            ancestor = level if i == ilvl else self._level_def(num_id, i)
            if ancestor is None:
                continue
            # An ancestor level's counter may not exist yet if this
            # paragraph jumps straight to a deep level without ever visiting
            # a shallower one first (unusual, but not invalid OOXML) --
            # default to that level's own start rather than 0/KeyError.
            count = counters.get(i, ancestor.start)
            # `isLgl` on the level actually being rendered forces every
            # substitution in ITS lvlText to decimal, including ancestor
            # positions that are themselves a different numFmt (e.g.
            # upperRoman "I" rendered as "1") -- see `_LevelDef.decimal_override`.
            fmt = "decimal" if level.decimal_override else ancestor.num_fmt
            text = text.replace(f"%{i + 1}", _render_count(count, fmt))
        return text


def _direct_num_pr(p_pr: CT_PPr | None) -> tuple[str, int] | None:
    """This `<w:pPr>`'s OWN `<w:numPr>` binding, or None if it has none at
    all -- never climbs styles (that's `resolve_paragraph_num_id_ilvl`'s
    job, one level up, since a style's `<w:pPr>` is checked with this same
    helper)."""
    if p_pr is None:
        return None
    num_pr = p_pr.numPr
    if num_pr is None:
        return None
    num_id = _find_val(num_pr, "w:numId")
    if num_id is None:
        return None
    ilvl_raw = _find_val(num_pr, "w:ilvl")
    return (num_id, int(ilvl_raw) if ilvl_raw is not None else 0)


def resolve_paragraph_num_id_ilvl(paragraph: Paragraph) -> tuple[str, int] | None:
    """The ``(numId, ilvl)`` `paragraph` actually renders under, or ``None``
    if it isn't part of any numbered/bulleted list.

    Checks the paragraph's own direct ``numPr`` first; if absent, climbs its
    style's ``basedOn`` chain (``Style.base_style``) looking for the first
    style that itself carries a ``numPr`` -- this is how a template's "List
    Paragraph"-style style applies numbering to every paragraph using it
    without each one repeating a direct ``numPr`` (#389 requirement:
    "style-inherited numPr").

    The chain is walked with a ``seen`` guard against a cyclic ``basedOn``
    graph -- corrupt/hand-edited OOXML can express one; python-docx's
    ``base_style`` resolution and `paragraph.style` fault does not itself
    guard against this, so an infinite loop here becomes this module's, not
    a caller's, problem to leave out.
    """
    p_pr: CT_PPr | None = paragraph._p.pPr  # noqa: SLF001 -- see module docstring
    direct = _direct_num_pr(p_pr)
    if direct is not None:
        return direct

    seen: set[int] = set()
    style = paragraph.style
    while style is not None and id(style) not in seen:
        seen.add(id(style))
        style_element = getattr(style, "element", None)
        style_p_pr = getattr(style_element, "pPr", None) if style_element is not None else None
        found = _direct_num_pr(style_p_pr)
        if found is not None:
            return found
        style = style.base_style
    return None
