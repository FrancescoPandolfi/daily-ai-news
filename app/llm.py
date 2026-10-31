from __future__ import annotations

import time
from functools import lru_cache
from typing import Literal

from openai import OpenAI

from . import metrics
from .config import get_settings

EMBED_BATCH_SIZE = 64


@lru_cache
def get_client() -> OpenAI:
    return OpenAI(api_key=get_settings().openai_api_key)


def embed(
    texts: list[str],
    operation: Literal["index_embed", "rag_embed"] = "index_embed",
) -> tuple[list[list[float]], float]:
    """Calcola gli embedding, a blocchi. Restituisce (vettori, costo stimato in USD)."""
    if not texts:
        return [], 0.0
    model = get_settings().embedding_model
    vectors: list[list[float]] = []
    cost = 0.0
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        started = time.perf_counter()
        try:
            response = get_client().embeddings.create(model=model, input=batch)
        except Exception:
            metrics.record_usage(
                operation, model=model, duration_ms=metrics.elapsed_ms(started), ok=False
            )
            raise
        tokens = response.usage.total_tokens if response.usage else 0
        cost += metrics.record_usage(
            operation,
            model=model,
            tokens_in=tokens,
            duration_ms=metrics.elapsed_ms(started),
        )
        vectors.extend(item.embedding for item in response.data)
    return vectors, cost
