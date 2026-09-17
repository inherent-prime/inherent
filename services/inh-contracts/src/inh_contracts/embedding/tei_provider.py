"""TEI (text-embeddings-inference) wire adapter -- the default provider (#311).

``POST /embed`` with ``{"inputs": [...], "truncate": true}`` -> a bare JSON
list of vectors, one per input, in request order.
"""

from __future__ import annotations

from inh_contracts.embedding.provider import HTTPEmbeddingProvider


class TEIProvider(HTTPEmbeddingProvider):
    """Default provider: HuggingFace text-embeddings-inference sidecar."""

    @property
    def name(self) -> str:
        return "tei"

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # truncate=true tells TEI to silently truncate inputs longer than the
        # model's max_input_length instead of returning 413. Without this, any
        # chunk over the model's token budget crashes the ENTIRE batch with
        # "Input validation error: inputs must have less than N tokens" --
        # dropping every other text in the batch along with it. Keep this
        # even though the caller (batching.py) also caps text count per
        # request; per-text length is a separate axis it doesn't control.
        resp = self._get_client().post("/embed", json={"inputs": texts, "truncate": True})
        resp.raise_for_status()
        data = resp.json()
        # TEI returns a bare list of vectors, already in request order
        # (already normalized for cosine-similarity models).
        return [[float(x) for x in vec] for vec in data]
