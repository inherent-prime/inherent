"""Pluggable chunk tagging (inherent#390 item 4).

A `Tagger` assigns a pack's tag-schema values to one chunk. `RulesTagger` is
the only implementation shipped here: it assigns enum fields from the
schema's optional `rules` (keyword/regex -> value) and fills string/date
fields only when the value is trivially available (document-level metadata
passed at upload, e.g. a title) -- it never guesses.

An LLM tagger is explicitly OUT of scope for this issue (the ingestion
engine has no chat LLM client today) -- see `Tagger` below for the
extension point a follow-up would implement against, and the module-level
TODO.

TODO(follow-up issue): add an `LlmTagger(Tagger)` once the engine has a chat
LLM client, using `vertical.tagger_examples` as few-shot examples. It should
implement the exact same `Tagger` interface so `tag_chunks` below needs no
change to switch a pack-enabled workspace over to it.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

import structlog
from inh_contracts.vertical_pack import TagSchema, Vertical
from temporalio import activity

from src.temporal.models import TagChunksInput, TagChunksOutput

logger = structlog.get_logger(__name__)

# The conventional "nothing else matched" enum value (inherent#390 item 4):
# when a pack's schema declares an enum value literally named "other", an
# enum field with zero rule matches is tagged "other" instead of omitted.
_FALLBACK_VALUE = "other"


class Tagger(ABC):
    """Assigns a pack's tag-schema values to one chunk of text.

    Implementations must never raise on ordinary "nothing matched" input --
    returning fewer tags than the schema declares is normal (untagged
    fields are simply omitted, per the tag schema's own `required` flag
    being advisory here, not enforced at tag time).
    """

    @abstractmethod
    def tag(
        self,
        *,
        heading: str,
        text: str,
        schema: TagSchema,
        document_metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return {field_name: value} for whichever fields this chunk gets."""
        raise NotImplementedError


def _score_enum_field(haystack: str, rules: dict[str, list[str]]) -> str | None:
    """Return the enum value whose rule patterns match `haystack` the most,
    case-insensitively. Ties keep the FIRST value in the schema's own
    declaration order (dict insertion order), a stable, deterministic
    tie-break rather than an arbitrary one. Returns None when every value
    scores zero matches (nothing in `rules` fired at all)."""
    best_value: str | None = None
    best_score = 0
    for value, patterns in rules.items():
        score = sum(len(re.findall(pattern, haystack, flags=re.IGNORECASE)) for pattern in patterns)
        if score > best_score:
            best_score = score
            best_value = value
    return best_value if best_score > 0 else None


class RulesTagger(Tagger):
    """Keyword/regex rules tagger (inherent#390 item 4).

    Enum fields: the pack schema's optional `rules` (value -> patterns) are
    matched against `heading + "\\n" + text`; the highest-scoring value wins
    (see `_score_enum_field`). A field with NO rules at all is never tagged
    by this class (there is nothing to score against) -- that is a schema
    authoring choice the pack makes, not an error.

    Unmatched enum field (rules exist, none scored): omitted, UNLESS the
    field's own `values` list contains a value literally named "other", in
    which case that becomes the fallback (explicit, tested behaviour -- see
    tests/test_tagging.py).

    String/date fields: filled ONLY when `document_metadata` has a key
    matching the field's name exactly (e.g. a document title passed at
    upload) -- never derived from the chunk text itself, since that would be
    guessing rather than "trivially derivable".
    """

    def tag(
        self,
        *,
        heading: str,
        text: str,
        schema: TagSchema,
        document_metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        haystack = f"{heading}\n{text}"
        document_metadata = document_metadata or {}
        tags: dict[str, Any] = {}

        for name, field in schema.fields.items():
            if field.type == "enum":
                if not field.rules:
                    continue  # nothing to score against; leave untagged
                value = _score_enum_field(haystack, field.rules)
                if value is None and _FALLBACK_VALUE in (field.values or []):
                    value = _FALLBACK_VALUE
                if value is not None:
                    tags[name] = value
            elif field.type in ("string", "date"):
                value = document_metadata.get(name)
                if value:
                    tags[name] = value
            # Unknown field.type can't happen -- TagField.type is a closed
            # Literal at the schema level (inh_contracts.vertical_pack).

        return tags


def _tags_as_weaviate_strings(tags: dict[str, Any]) -> list[str]:
    """ "field=value" strings for the Weaviate `tags` TEXT_ARRAY property
    (inherent#390 item 4's recommended representation) -- arbitrary pack
    schema fields become filterable without a per-pack Weaviate schema
    change."""
    return [f"{name}={value}" for name, value in sorted(tags.items())]


@activity.defn
async def tag_chunks(input: TagChunksInput) -> TagChunksOutput:
    """Tag every staged chunk of a document for a pack-enabled workspace.

    No-op (returns immediately, chunks left untouched) when `input.
    vertical_pack` is unset -- legacy/no-pack workspaces are byte-for-byte
    unaffected by this activity's existence.
    """
    if not input.vertical_pack:
        return TagChunksOutput(tagged_count=0)

    from src.config.settings import get_settings
    from src.temporal.activities.numbered_sections import resolve_pack
    from src.temporal.shared_services import get_staging_service

    settings = get_settings()
    vertical: Vertical | None = resolve_pack(settings.vertical_packs_dir, input.vertical_pack)
    if vertical is None:
        # Pack discovery off, or the named pack isn't found/failed to load --
        # degrade to "no tagging" rather than fail the whole document.
        logger.warning(
            "tag_chunks: vertical pack not resolvable, skipping tagging",
            vertical_pack=input.vertical_pack,
        )
        return TagChunksOutput(tagged_count=0)

    staging = get_staging_service()
    chunk_dicts = staging.read_chunks(input.workflow_run_id)

    tagger: Tagger = RulesTagger()
    tagged_count = 0
    for chunk_dict in chunk_dicts:
        heading = chunk_dict.get("section_heading") or ""
        text = chunk_dict.get("content") or ""
        tags = tagger.tag(
            heading=heading,
            text=text,
            schema=vertical.tags,
            document_metadata=input.document_metadata,
        )
        if tags:
            chunk_dict["tags"] = tags
            # Precomputed Weaviate projection travels alongside the chunk
            # dict through staging -- store.py's Weaviate write promotes it
            # directly rather than recomputing the "field=value" strings.
            chunk_dict["tags_weaviate"] = _tags_as_weaviate_strings(tags)
            tagged_count += 1

    staging.write_chunks(input.workflow_run_id, chunk_dicts)
    return TagChunksOutput(tagged_count=tagged_count)
