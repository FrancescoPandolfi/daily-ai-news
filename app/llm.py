from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from .config import get_settings

EMBED_BATCH_SIZE = 64


@lru_cache
def get_client() -> OpenAI:
    return OpenAI(api_key=get_settings().openai_api_key)


def embed(texts: list[str]) -> list[list[float]]:
    """Calcola gli embedding, a blocchi, per una lista di testi."""
    if not texts:
        return []
    model = get_settings().embedding_model
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        response = get_client().embeddings.create(model=model, input=batch)
        vectors.extend(item.embedding for item in response.data)
    return vectors
