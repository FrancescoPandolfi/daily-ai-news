from __future__ import annotations

from typing import Any

from . import storage
from .config import get_settings
from .llm import embed, get_client

SYSTEM_PROMPT = """Rispondi in italiano alla domanda usando SOLO gli estratti forniti.
Cita le fonti con il loro numero tra parentesi quadre, ad esempio [1] o [2][3].
Se gli estratti non bastano per rispondere, dillo chiaramente senza inventare."""


def answer(question: str, topic: str | None = None, days: int | None = None) -> dict[str, Any]:
    """Risponde alla domanda recuperando i blocchi più rilevanti dall'archivio."""
    settings = get_settings()
    query_vector = embed([question])[0]
    hits = storage.search_chunks(query_vector, settings.rag_top_k, topic=topic, days=days)
    if not hits:
        return {"answer": "Non ho trovato articoli rilevanti nell'archivio.", "sources": []}

    # un numero per articolo: più blocchi dello stesso articolo condividono la citazione
    numbers: dict[int, int] = {}
    sources: list[dict[str, Any]] = []
    blocks: list[str] = []
    for hit in hits:
        article_id = hit["article_id"]
        if article_id not in numbers:
            numbers[article_id] = len(numbers) + 1
            sources.append(
                {
                    "n": numbers[article_id],
                    "title": hit["title"],
                    "url": hit["url"],
                    "topic": hit["topic"],
                    "date": (hit["published_at"] or hit["created_at"])[:10],
                }
            )
        blocks.append(f"[{numbers[article_id]}] {hit['title']} ({hit['url']})\n{hit['text']}")

    response = get_client().chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Estratti:\n\n" + "\n\n---\n\n".join(blocks) + f"\n\nDomanda: {question}",
            },
        ],
    )
    return {"answer": response.choices[0].message.content or "", "sources": sources}
