"""Vertical pack contract: load and validate a pack's manifest + profiles.

A "vertical pack" is a small, separate repo that supplies domain config
(chunking rules, a tag schema, tagger examples, tool profiles) to the
generic core engine. Core has NO domain terms baked in -- everything
domain-specific lives in the pack, on disk (mounted under
``VERTICAL_PACKS_DIR``) or discovered via the ``inherent.verticals`` Python
entry-point group (see ``discover_packs``/``discover_entry_point_packs``).

This module is a straight port of the pack format defined and already
shipped by the first vertical pack (its reference
loader, inherent#390/#392) -- core must load EXACTLY
that file layout (``vertical.yaml`` -> chunking / tags / tagger_examples /
tools profile files). The only addition here is the OPTIONAL ``rules`` block
on an enum tag field (used by the rules-based tagger, inherent#390 item 4);
packs written before ``rules`` existed still validate unchanged.

Schema-level checks live on the models; cross-file checks (tool filters,
tagger examples and rules keys against the tag schema) live in
``load_vertical``.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class VerticalError(ValueError):
    """Pack is malformed; the message names the file and the problem."""


class _Strict(BaseModel):
    # Unknown keys are typos, not extensions.
    model_config = ConfigDict(extra="forbid", frozen=True)


class HeadingPattern(_Strict):
    """One heading-detection rule in a chunking profile.

    Patterns are tried top to bottom; the first match wins, so the pack
    orders its most specific patterns first. Group 1 of ``regex`` must
    capture the section number.
    """

    level: int = Field(ge=1, le=9)
    regex: str

    @field_validator("regex")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex {value!r}: {exc}") from exc
        if compiled.groups < 1:
            raise ValueError(f"regex {value!r} needs group 1 to capture the number")
        return value


class ChunkingProfile(_Strict):
    """Drives the ``numbered_sections`` chunking strategy for a pack."""

    strategy: Literal["numbered_sections"]
    split_at_level: int = Field(ge=1, le=9)
    keep_parent_heading: bool = True
    max_tokens: int = Field(gt=0)
    heading_patterns: list[HeadingPattern] = Field(min_length=1)


class TagField(_Strict):
    type: Literal["enum", "string", "date"]
    required: bool = False
    values: list[str] | None = None
    # Optional per-value keyword/regex rules for the rules tagger (inherent#390
    # item 4/1): enum value -> list of case-insensitive keywords/regexes
    # matched against a section's heading + text. Only meaningful for
    # `type: enum`; key membership (must be an allowed enum value) is
    # validated in `load_vertical` below, where the full field name is
    # available for the error message. A pack with no `rules` block (every
    # pack before this existed) validates exactly as before -- this field is
    # optional and defaults to empty.
    rules: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("rules")
    @classmethod
    def _patterns_compile(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for tag_value, patterns in value.items():
            if not patterns:
                raise ValueError(f"rules[{tag_value!r}] has no patterns")
            for pattern in patterns:
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error as exc:
                    raise ValueError(f"invalid rule regex {pattern!r}: {exc}") from exc
        return value

    def check(self, name: str, value: Any) -> None:
        """Raise VerticalError if `value` breaks this field's rules."""
        if self.type == "enum" and value not in (self.values or []):
            raise VerticalError(f"{name}={value!r} is not one of {self.values}")
        if self.type == "date" and not isinstance(value, date):
            raise VerticalError(f"{name}={value!r} is not a date")


class TagSchema(_Strict):
    fields: dict[str, TagField] = Field(min_length=1)


class TaggerExample(_Strict):
    text: str = Field(min_length=1)
    tags: dict[str, Any]


class TaggerExamples(_Strict):
    examples: list[TaggerExample] = Field(min_length=1)


class ToolProfile(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=20)
    filters: list[str] = []
    default_limit: int = Field(ge=1, le=50)
    max_limit: int = Field(ge=1, le=50)


class ProfilePaths(_Strict):
    chunking: str
    tags: str
    tagger_examples: str
    tools: list[str] = Field(min_length=1)


class Manifest(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    version: str
    description: str
    requires_core: str
    profiles: ProfilePaths


class Vertical(_Strict):
    manifest: Manifest
    chunking: ChunkingProfile
    tags: TagSchema
    tagger_examples: TaggerExamples
    tools: list[ToolProfile]


_M = TypeVar("_M", bound=BaseModel)


def _load(root: Path, relative: str, model: type[_M]) -> _M:
    """Parse one YAML file into `model`, wrapping every failure as VerticalError."""
    path = root / relative
    if not path.is_file():
        raise VerticalError(f"missing profile file: {relative}")
    try:
        return model.model_validate(yaml.safe_load(path.read_text()))
    except (yaml.YAMLError, ValidationError) as exc:
        raise VerticalError(f"{relative}: {exc}") from exc


def load_vertical(root: Path) -> Vertical:
    """Load the pack at `root` and verify its files agree with each other."""
    manifest = _load(root, "vertical.yaml", Manifest)
    paths = manifest.profiles
    tags = _load(root, paths.tags, TagSchema)
    tools = [_load(root, p, ToolProfile) for p in paths.tools]
    examples = _load(root, paths.tagger_examples, TaggerExamples)

    for tool in tools:
        if tool.default_limit > tool.max_limit:
            raise VerticalError(f"tool {tool.name}: default_limit exceeds max_limit")
        unknown = set(tool.filters) - set(tags.fields)
        if unknown:
            raise VerticalError(f"tool {tool.name}: filters not in tag schema: {sorted(unknown)}")

    for i, example in enumerate(examples.examples):
        for name, value in example.tags.items():
            field = tags.fields.get(name)
            if field is None:
                raise VerticalError(f"tagger example {i}: unknown tag {name!r}")
            try:
                field.check(name, value)
            except VerticalError as exc:
                raise VerticalError(f"tagger example {i}: {exc}") from exc

    # Rules cross-validation (inherent#390 item 1): every rule key must be an
    # allowed enum value for that field. Regex compilation is already checked
    # at the model level (TagField._patterns_compile); this just checks the
    # KEYS, which need the field's own `values` list to validate against.
    for field_name, field in tags.fields.items():
        if not field.rules:
            continue
        if field.type != "enum":
            raise VerticalError(f"tag {field_name!r}: rules are only valid on enum fields")
        allowed = set(field.values or [])
        unknown_keys = set(field.rules) - allowed
        if unknown_keys:
            raise VerticalError(
                f"tag {field_name!r}: rule keys not in its enum values: {sorted(unknown_keys)}"
            )

    return Vertical(
        manifest=manifest,
        chunking=_load(root, paths.chunking, ChunkingProfile),
        tags=tags,
        tagger_examples=examples,
        tools=tools,
    )
