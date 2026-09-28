"""numbered_sections dispatch precedence in chunk_text (inherent#390 item 3).

Precedence (top to bottom): per-document override > numbered_sections
(new, pack-driven) > registry chunking_hint > global config. Existing
strategies/precedence must be byte-for-byte unchanged when no pack is
bound -- most of these tests assert exactly that "unchanged" half, plus the
new numbered_sections branch winning when it applies.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from inh_contracts.vertical_pack import load_vertical

from src.temporal.activities.chunk import _chunk_text_inner
from src.temporal.models import ChunkTextInput

FIXTURE_PACK = Path(__file__).resolve().parent / "fixtures" / "handbook_pack"
PACKS_DIR = str(FIXTURE_PACK.parent)


def _settings(**overrides):
    settings = MagicMock()
    settings.chunking_strategy = "tokens"
    settings.max_chunk_size = 1000
    settings.chunk_overlap = 50
    settings.embedding_max_tokens = 512
    settings.vertical_packs_dir = None
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


async def _run(text: str, chunk_input: ChunkTextInput, settings) -> list[dict]:
    staging = MagicMock()
    staging.read_text.return_value = text
    written: dict = {}

    def _capture(_run_id, chunks):
        written["chunks"] = chunks

    staging.write_chunks.side_effect = _capture

    with (
        patch("src.temporal.shared_services.get_staging_service", return_value=staging),
        patch("src.config.settings.get_settings", return_value=settings),
    ):
        await _chunk_text_inner(chunk_input)
    return written["chunks"]


@pytest.mark.asyncio
async def test_no_pack_bound_falls_through_unchanged():
    """vertical_pack unset (the default) -> pre-#390 dispatch, untouched."""
    chunks = await _run(
        "Sentence one. Sentence two.",
        ChunkTextInput(workflow_run_id="wf", document_id="d"),
        _settings(),
    )
    assert chunks[0]["chunking_strategy"] == "tokens"


@pytest.mark.asyncio
async def test_pack_bound_but_discovery_off_falls_through_unchanged():
    """vertical_pack set, but VERTICAL_PACKS_DIR unset -> feature off, unchanged."""
    chunks = await _run(
        "Sentence one. Sentence two.",
        ChunkTextInput(workflow_run_id="wf", document_id="d", vertical_pack="handbook"),
        _settings(vertical_packs_dir=None),
    )
    assert chunks[0]["chunking_strategy"] == "tokens"


@pytest.mark.asyncio
async def test_unknown_pack_name_falls_through_unchanged():
    chunks = await _run(
        "Sentence one. Sentence two.",
        ChunkTextInput(workflow_run_id="wf", document_id="d", vertical_pack="no-such-pack"),
        _settings(vertical_packs_dir=PACKS_DIR),
    )
    assert chunks[0]["chunking_strategy"] == "tokens"


@pytest.mark.asyncio
async def test_pack_bound_and_resolvable_uses_numbered_sections():
    text = "1. PRICING\n1.1 The monthly fee is $10 per seat.\n2. SECURITY\nData is encrypted.\n"
    chunks = await _run(
        text,
        ChunkTextInput(workflow_run_id="wf", document_id="d", vertical_pack="handbook"),
        _settings(vertical_packs_dir=PACKS_DIR),
    )
    assert all(c["chunking_strategy"] == "numbered_sections" for c in chunks)
    assert any("1. PRICING" in c["content"] for c in chunks)


@pytest.mark.asyncio
async def test_explicit_per_document_override_wins_over_bound_pack():
    """Per-document override is the TOP precedence level -- wins even with a
    resolvable pack bound (inherent#390's required precedence order)."""
    chunks = await _run(
        "Sentence one. Sentence two. Sentence three.",
        ChunkTextInput(
            workflow_run_id="wf",
            document_id="d",
            strategy="paragraphs",
            vertical_pack="handbook",
        ),
        _settings(vertical_packs_dir=PACKS_DIR),
    )
    assert chunks[0]["chunking_strategy"] == "paragraphs"


@pytest.mark.asyncio
async def test_existing_content_type_hint_dispatch_unaffected_by_pack_plumbing():
    """No vertical_pack given at all -> the #129 content_type/hint dispatch
    (an entirely separate precedence level) is completely untouched."""
    chunks = await _run(
        '{"a": 1}',
        ChunkTextInput(workflow_run_id="wf", document_id="d", content_type="application/json"),
        _settings(),
    )
    # structured hint with no "## " markers -> degrades to tokens, same as
    # before #390 ever existed.
    assert chunks[0]["chunking_strategy"] == "tokens"


def test_fixture_pack_actually_loads():
    """Sanity: the fixture pack this test module relies on is valid."""
    vertical = load_vertical(FIXTURE_PACK)
    assert vertical.chunking.strategy == "numbered_sections"
