from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

from . import metrics, storage
from .config import get_settings
from .llm import embed, get_client

_CITATION_GROUP = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")  # [1] oppure [1, 2]
_CITATION_SEPARATOR = re.compile(r"\s*[,;]\s*")

NO_HITS_ANSWER = "Non ho trovato articoli rilevanti nell'archivio."
EMPTY_ANSWER = "Non sono riuscito a formulare una risposta."

SYSTEM_PROMPT = """Rispondi in italiano alla domanda usando SOLO gli estratti numerati forniti sotto.
Questi estratti sono l'unico materiale consentito: niente conoscenze generali, niente notizie
ricordate dal training, niente fatti o link che non compaiano esplicitamente negli estratti.
Cita le fonti con il loro numero tra parentesi quadre, ad esempio [1] o [2][3]: una citazione per
ogni numero, e cita solo gli estratti che hai davvero usato. Non inventare numeri di citazione.
Se gli estratti non bastano per rispondere, dillo chiaramente (es. «Nell'archivio non ho trovato
abbastanza informazioni su…») senza completare con altro.
Scrivi in paragrafi brevi e in testo semplice: niente titoli markdown, puoi usare **grassetto**
per i punti chiave e "- " per gli elenchi."""

# Eventi di answer_stream:
#   {"type": "delta", "text": "..."}                              un pezzo di testo appena generato
#   {"type": "final", "answer": ..., "sources": [...], "sources_cited": bool}   risposta definitiva
Event = dict[str, Any]


def answer(question: str, topic: str | None = None, days: int | None = None) -> dict[str, Any]:
    """Risposta completa in un colpo solo (usata dal form senza JavaScript)."""
    final: Event | None = None
    for event in answer_stream(question, topic, days):
        if event["type"] == "final":
            final = event
    if final is None:
        raise RuntimeError("Il flusso della risposta è finito senza un risultato")
    return {key: final[key] for key in ("answer", "sources", "sources_cited")}


def answer_stream(
    question: str, topic: str | None = None, days: int | None = None
) -> Iterator[Event]:
    """Risponde alla domanda recuperando i blocchi più rilevanti dall'archivio.

    Genera gli eventi `delta` mentre il modello scrive e un solo `final` alla fine, con il testo
    definitivo (citazioni rinumerate) e le sole fonti citate. Il testo definitivo può differire da
    quello dei `delta` proprio per la rinumerazione: chi mostra il flusso deve sostituirlo.
    Ogni domanda, riuscita, fallita o interrotta, viene registrata con tempo, costo ed esito.
    """
    started = time.perf_counter()
    final: Event | None = None
    cost = 0.0
    error: str | None = None
    try:
        vectors, cost = embed([question], operation="rag_embed")
        hits = storage.search_chunks(vectors[0], get_settings().rag_top_k, topic=topic, days=days)
        if not hits:
            final = {"type": "final", "answer": NO_HITS_ANSWER, "sources": [], "sources_cited": True}
            yield final
            return

        sources, blocks = _numbered_sources(hits)
        parts: list[str] = []
        generation = _generate(question, blocks)
        try:
            while True:
                try:
                    piece = next(generation)
                except StopIteration as done:
                    cost += done.value
                    break
                parts.append(piece)
                yield {"type": "delta", "text": piece}
        finally:
            generation.close()  # se il client se ne va, chiude subito lo stream verso OpenAI

        text, shown, cited = keep_cited_sources("".join(parts), sources)
        final = {
            "type": "final",
            "answer": text if text.strip() else EMPTY_ANSWER,
            "sources": shown,
            "sources_cited": cited,
        }
        yield final
    except GeneratorExit:
        error = "Interrotta: connessione chiusa prima della fine"
        raise
    except Exception as exc:
        error = metrics.describe_error(exc)
        raise
    finally:
        metrics.record_query(
            question=question,
            topic=topic,
            days=days,
            latency_ms=metrics.elapsed_ms(started),
            error=error,
            sources=len(final["sources"]) if final else 0,
            cost_usd=cost,
        )


def _numbered_sources(hits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Un numero per articolo: più blocchi dello stesso articolo condividono la citazione."""
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
                    "image": hit["image_url"] or None,
                }
            )
        blocks.append(f"[{numbers[article_id]}] {hit['title']} ({hit['url']})\n{hit['text']}")
    return sources, blocks


def _generate(question: str, blocks: list[str]) -> Iterator[str]:
    """Chiede la risposta al modello in streaming. Restituisce (via StopIteration) il costo in USD.

    L'uso viene registrato anche se il flusso si interrompe: in quel caso senza token, segnato come fallito.
    """
    settings = get_settings()
    started = time.perf_counter()
    tokens_in = tokens_out = 0
    completed = False
    try:
        with get_client().chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Estratti dall'archivio (usa solo questi):\n\n"
                    + "\n\n---\n\n".join(blocks)
                    + f"\n\nDomanda: {question}",
                },
            ],
            stream=True,
            stream_options={"include_usage": True},
        ) as stream:
            for chunk in stream:
                if chunk.usage:  # l'ultimo blocco porta l'uso e non ha testo
                    tokens_in = chunk.usage.prompt_tokens
                    tokens_out = chunk.usage.completion_tokens
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        completed = True
    finally:
        cost = metrics.record_usage(
            "rag_answer",
            model=settings.llm_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=metrics.elapsed_ms(started),
            ok=completed,
        )
    return cost


def keep_cited_sources(
    text: str, sources: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]], bool]:
    """Tiene solo le fonti citate nel testo e le rinumera nell'ordine in cui compaiono.

    Il modello riceve gli estratti di tutti gli articoli trovati ma di solito ne usa solo alcuni:
    mostrarli tutti come «Fonti» farebbe credere che la risposta si basi su tutti.
    Le citazioni con un numero inesistente vengono tolte. Se il testo non cita nulla non si può
    sapere cosa è stato usato: restituisce tutte le fonti e `False`, così la pagina le presenta
    come «consultate» e non come citate.
    """
    by_number = {source["n"]: source for source in sources}
    new_numbers: dict[int, int] = {}

    def renumber(match: re.Match[str]) -> str:
        marks: list[str] = []
        for raw in _CITATION_SEPARATOR.split(match.group(1)):
            old = int(raw)
            if old not in by_number:
                continue
            marks.append(f"[{new_numbers.setdefault(old, len(new_numbers) + 1)}]")
        return "".join(dict.fromkeys(marks))

    new_text = _CITATION_GROUP.sub(renumber, text)
    if not new_numbers:
        return text, sources, False
    cited = [{**by_number[old], "n": new} for old, new in sorted(new_numbers.items(), key=lambda item: item[1])]
    return new_text, cited, True
