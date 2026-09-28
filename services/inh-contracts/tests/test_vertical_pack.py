"""Vertical pack contract tests (inherent#390).

Uses a generic "handbook" fixture pack under tests/fixtures -- no domain
(legal/contract/etc.) terms, per the issue's "no domain terms in core code,
docs or test fixtures" requirement.
"""

from pathlib import Path

import pytest

from inh_contracts.vertical_pack import (
    HeadingPattern,
    TagField,
    VerticalError,
    load_vertical,
)

FIXTURES = Path(__file__).parent / "fixtures"
HANDBOOK = FIXTURES / "handbook_pack"
HANDBOOK_NO_RULES = FIXTURES / "handbook_pack_no_rules"


def test_loads_good_pack():
    vertical = load_vertical(HANDBOOK)
    assert vertical.manifest.name == "handbook"
    assert vertical.chunking.strategy == "numbered_sections"
    assert vertical.tags.fields["section_type"].type == "enum"
    assert vertical.tools[0].name == "search_sections"
    assert len(vertical.tagger_examples.examples) == 3


def test_rules_block_is_optional_backward_compat():
    """A pack with no `rules` on any tag field still loads (legacy packs)."""
    vertical = load_vertical(HANDBOOK_NO_RULES)
    assert vertical.tags.fields["section_type"].rules == {}


def test_rules_are_parsed_and_compiled():
    vertical = load_vertical(HANDBOOK)
    rules = vertical.tags.fields["section_type"].rules
    assert set(rules) == {"pricing", "security"}
    assert rules["pricing"] == ["price", "fee", r"\bcost\b"]


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(VerticalError, match="vertical.yaml"):
        load_vertical(tmp_path)


def test_missing_profile_file_raises(tmp_path):
    (tmp_path / "vertical.yaml").write_text(
        """
name: broken
version: "0.1.0"
description: missing profile files
requires_core: ">=0.8.0"
profiles:
  chunking: profiles/chunking/missing.yaml
  tags: profiles/tags/missing.yaml
  tagger_examples: profiles/tagger/missing.yaml
  tools: [profiles/tools/missing.yaml]
"""
    )
    with pytest.raises(VerticalError, match="missing profile file"):
        load_vertical(tmp_path)


def test_tool_filter_not_in_tag_schema_raises(tmp_path):
    _write_minimal_pack(tmp_path, tool_filters=["not_a_real_field"])
    with pytest.raises(VerticalError, match="filters not in tag schema"):
        load_vertical(tmp_path)


def test_tool_default_limit_over_max_raises(tmp_path):
    _write_minimal_pack(tmp_path, default_limit=20, max_limit=10)
    with pytest.raises(VerticalError, match="default_limit exceeds max_limit"):
        load_vertical(tmp_path)


def test_tagger_example_unknown_tag_raises(tmp_path):
    _write_minimal_pack(tmp_path, example_tags="{section_type: pricing, bogus: 1}")
    with pytest.raises(VerticalError, match="unknown tag"):
        load_vertical(tmp_path)


def test_tagger_example_bad_enum_value_raises(tmp_path):
    _write_minimal_pack(tmp_path, example_tags="{section_type: not_a_value}")
    with pytest.raises(VerticalError, match="is not one of"):
        load_vertical(tmp_path)


def test_rules_key_not_in_enum_values_raises(tmp_path):
    """Bad cross-ref: a `rules` key that isn't one of the field's own enum values."""
    _write_minimal_pack(tmp_path, rules_yaml="pricing: ['fee']\n      not_a_value: ['x']")
    with pytest.raises(VerticalError, match="rule keys not in its enum values"):
        load_vertical(tmp_path)


def test_rules_on_non_enum_field_raises(tmp_path):
    _write_minimal_pack(tmp_path, rules_on_string_field=True)
    with pytest.raises(VerticalError, match="rules are only valid on enum fields"):
        load_vertical(tmp_path)


def test_bad_regex_in_rules_raises():
    with pytest.raises(ValueError, match="invalid rule regex"):
        TagField(type="enum", values=["a"], rules={"a": ["("]})


def test_heading_pattern_needs_capture_group():
    with pytest.raises(ValueError, match="needs group 1"):
        HeadingPattern(level=1, regex=r"^\d+\s+")


def test_heading_pattern_bad_regex():
    with pytest.raises(ValueError, match="invalid regex"):
        HeadingPattern(level=1, regex="(unclosed")


def test_unknown_manifest_key_rejected(tmp_path):
    (tmp_path / "vertical.yaml").write_text(
        """
name: broken
version: "0.1.0"
description: has a typo'd extra key
requires_core: ">=0.8.0"
oops_extra_key: true
profiles:
  chunking: profiles/chunking/c.yaml
  tags: profiles/tags/t.yaml
  tagger_examples: profiles/tagger/e.yaml
  tools: [profiles/tools/tool.yaml]
"""
    )
    with pytest.raises(VerticalError):
        load_vertical(tmp_path)


def _write_minimal_pack(
    tmp_path: Path,
    tool_filters: list[str] | None = None,
    default_limit: int = 3,
    max_limit: int = 10,
    example_tags: str = "{section_type: pricing}",
    rules_yaml: str | None = None,
    rules_on_string_field: bool = False,
) -> None:
    """Write a minimal, otherwise-valid pack into `tmp_path`, with one knob
    tweaked per test so each bad-input test isolates a single failure mode."""
    (tmp_path / "profiles" / "chunking").mkdir(parents=True)
    (tmp_path / "profiles" / "tags").mkdir(parents=True)
    (tmp_path / "profiles" / "tagger").mkdir(parents=True)
    (tmp_path / "profiles" / "tools").mkdir(parents=True)

    (tmp_path / "vertical.yaml").write_text(
        """
name: minimal
version: "0.1.0"
description: minimal fixture pack
requires_core: ">=0.8.0"
profiles:
  chunking: profiles/chunking/c.yaml
  tags: profiles/tags/t.yaml
  tagger_examples: profiles/tagger/e.yaml
  tools: [profiles/tools/tool.yaml]
"""
    )
    (tmp_path / "profiles" / "chunking" / "c.yaml").write_text(
        """
strategy: numbered_sections
split_at_level: 2
keep_parent_heading: true
max_tokens: 100
heading_patterns:
  - level: 1
    regex: '^(\\d+)\\.\\s+\\S'
"""
    )
    rules_block = ""
    string_rules_block = ""
    if rules_yaml:
        rules_block = f"    rules:\n      {rules_yaml}\n"
    if rules_on_string_field:
        string_rules_block = "    rules:\n      x: ['y']\n"  # x isn't an enum value at all, but the point is `product` is type: string
    (tmp_path / "profiles" / "tags" / "t.yaml").write_text(
        f"""
fields:
  section_type:
    type: enum
    values: [pricing, security, other]
{rules_block}  product:
    type: string
{string_rules_block}
"""
    )
    (tmp_path / "profiles" / "tagger" / "e.yaml").write_text(
        f"""
examples:
  - text: "some example text"
    tags: {example_tags}
"""
    )
    filters = tool_filters if tool_filters is not None else ["section_type"]
    (tmp_path / "profiles" / "tools" / "tool.yaml").write_text(
        f"""
name: search_sections
description: "search the handbook's numbered sections for relevant text"
filters: {filters}
default_limit: {default_limit}
max_limit: {max_limit}
"""
    )
