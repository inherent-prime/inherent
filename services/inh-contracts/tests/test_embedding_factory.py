"""create_embedding_provider (#311 item 1) -- provider selection is an env-only change."""

from __future__ import annotations

import pytest

from inh_contracts.embedding import (
    DEFAULT_EMBEDDING_PROVIDER,
    OpenAICompatibleProvider,
    TEIProvider,
    create_embedding_provider,
)


def test_default_provider_is_tei() -> None:
    """TEI stays the default -- non-negotiable (#311 item 8)."""
    assert DEFAULT_EMBEDDING_PROVIDER == "tei"


@pytest.mark.parametrize("value", ["tei", "TEI", " tei ", ""])
def test_tei_aliases_select_tei_provider(value: str) -> None:
    provider = create_embedding_provider(
        provider=value, base_url="http://x", model_id="m", dimension=3
    )
    assert isinstance(provider, TEIProvider)


@pytest.mark.parametrize(
    "value", ["openai_compatible", "OPENAI_COMPATIBLE", "openai-compatible", "openai"]
)
def test_openai_aliases_select_openai_provider(value: str) -> None:
    provider = create_embedding_provider(
        provider=value, base_url="http://x", model_id="m", dimension=3
    )
    assert isinstance(provider, OpenAICompatibleProvider)


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="Unknown EMBEDDING_PROVIDER"):
        create_embedding_provider(
            provider="bogus-vendor", base_url="http://x", model_id="m", dimension=3
        )


def test_factory_forwards_config_to_provider() -> None:
    provider = create_embedding_provider(
        provider="tei",
        base_url="http://tei:80",
        model_id="BAAI/bge-small-en-v1.5",
        dimension=384,
        api_key="k",
    )
    assert provider.model_id == "BAAI/bge-small-en-v1.5"
    assert provider.dimension == 384
