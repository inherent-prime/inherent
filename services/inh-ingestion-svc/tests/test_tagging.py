"""RulesTagger + tag_chunks activity tests (inherent#390 item 4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from inh_contracts.vertical_pack import load_vertical

from src.temporal.activities.tagging import RulesTagger, _tags_as_weaviate_strings, tag_chunks
from src.temporal.models import TagChunksInput

FIXTURE_PACK = Path(__file__).resolve().parent / "fixtures" / "handbook_pack"
PACKS_DIR = str(FIXTURE_PACK.parent)


@pytest.fixture(scope="module")
def tags_schema():
    return load_vertical(FIXTURE_PACK).tags


def test_rules_tagger_picks_highest_scoring_enum_value(tags_schema):
    tagger = RulesTagger()
    tags = tagger.tag(
        heading="1.1 Standard plan",
        text="The monthly fee for the standard plan is $10 per seat.",
        schema=tags_schema,
    )
    assert tags["section_type"] == "pricing"


def test_rules_tagger_matches_case_insensitively(tags_schema):
    tagger = RulesTagger()
    tags = tagger.tag(heading="", text="ALL DATA MUST BE ENCRYPTED AT REST.", schema=tags_schema)
    assert tags["section_type"] == "security"


def test_rules_tagger_unmatched_falls_back_to_other_when_declared(tags_schema):
    tagger = RulesTagger()
    tags = tagger.tag(
        heading="Governance", text="This handbook is reviewed annually.", schema=tags_schema
    )
    assert tags["section_type"] == "other"


def test_rules_tagger_omits_field_when_no_rules_and_no_fallback():
    """A field with rules on SOME values but no "other" declared, and no
    match, is omitted rather than guessed."""
    from inh_contracts.vertical_pack import TagField, TagSchema

    schema = TagSchema(
        fields={
            "kind": TagField(
                type="enum", values=["pricing", "security"], rules={"pricing": ["fee"]}
            )
        }
    )
    tagger = RulesTagger()
    tags = tagger.tag(heading="", text="nothing relevant here", schema=schema)
    assert "kind" not in tags


def test_rules_tagger_fills_string_field_from_document_metadata(tags_schema):
    tagger = RulesTagger()
    tags = tagger.tag(
        heading="",
        text="some section text",
        schema=tags_schema,
        document_metadata={"product": "Core Platform"},
    )
    assert tags["product"] == "Core Platform"


def test_rules_tagger_leaves_string_field_empty_without_metadata(tags_schema):
    tagger = RulesTagger()
    tags = tagger.tag(heading="", text="some section text", schema=tags_schema)
    assert "product" not in tags


def test_tags_as_weaviate_strings_format():
    assert _tags_as_weaviate_strings({"section_type": "pricing", "product": "Core"}) == [
        "product=Core",
        "section_type=pricing",
    ]


@pytest.mark.asyncio
async def test_tag_chunks_noop_when_no_vertical_pack():
    out = await tag_chunks(TagChunksInput(workflow_run_id="wf", document_id="d"))
    assert out.tagged_count == 0


@pytest.mark.asyncio
async def test_tag_chunks_noop_when_pack_not_resolvable():
    settings = MagicMock()
    settings.vertical_packs_dir = None
    with patch("src.config.settings.get_settings", return_value=settings):
        out = await tag_chunks(
            TagChunksInput(workflow_run_id="wf", document_id="d", vertical_pack="handbook")
        )
    assert out.tagged_count == 0


@pytest.mark.asyncio
async def test_tag_chunks_tags_staged_chunks_and_writes_weaviate_projection():
    settings = MagicMock()
    settings.vertical_packs_dir = PACKS_DIR

    staging = MagicMock()
    staging.read_chunks.return_value = [
        {
            "document_id": "d",
            "content": "The monthly fee for the standard plan is $10 per seat.",
            "section_heading": "1.1 Standard plan",
            "chunk_index": 0,
        },
    ]
    written: dict = {}
    staging.write_chunks.side_effect = lambda _run_id, chunks: written.setdefault("chunks", chunks)

    with (
        patch("src.config.settings.get_settings", return_value=settings),
        patch("src.temporal.shared_services.get_staging_service", return_value=staging),
    ):
        out = await tag_chunks(
            TagChunksInput(workflow_run_id="wf", document_id="d", vertical_pack="handbook")
        )

    assert out.tagged_count == 1
    tagged = written["chunks"][0]
    assert tagged["tags"]["section_type"] == "pricing"
    assert tagged["tags_weaviate"] == ["section_type=pricing"]
