"""OpenAI-compatible wire adapter (#311).

``POST /v1/embeddings`` with ``{"model": ..., "input": [...]}`` ->
``{"data": [{"index": ..., "embedding": [...]}, ...]}``. The spec does not
promise ``data`` comes back in request order -- each entry carries its own
``index`` -- so this adapter sorts by ``index`` before returning, rather than
assuming order is preserved.
"""

from __future__ import annotations

from inh_contracts.embedding.provider import HTTPEmbeddingProvider


class OpenAICompatibleProvider(HTTPEmbeddingProvider):
    """Any embeddings endpoint implementing the OpenAI ``/v1/embeddings`` shape."""

    @property
    def name(self) -> str:
        return "openai_compatible"

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = self._get_client().post(
            "/v1/embeddings", json={"model": self._model_id, "input": texts}
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # The response `data` array carries an `index` field per the OpenAI
        # spec -- order by it explicitly rather than assuming the API
        # preserves request order (#311 item 3).
        ordered = sorted(data, key=lambda item: item["index"])
        return [[float(x) for x in item["embedding"]] for item in ordered]
