from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import metrics
from .config import get_settings
from .llm import get_client
from .tools import WebTools

logger = logging.getLogger(__name__)

MAX_STEPS = 8

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Cerca notizie recenti sul web. Restituisce titolo, URL e un estratto.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Query di ricerca"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Legge il testo di un articolo a partire dall'URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]

SYSTEM_PROMPT = """Sei un redattore che prepara un briefing quotidiano di notizie sull'intelligenza artificiale.
Per l'argomento indicato:
1. Cerca notizie recenti con search_web (puoi fare più ricerche con query diverse). Nelle query
   includi sempre termini legati all'AI / all'argomento (es. AI, LLM, agenti, AI Act), così
   i risultati restano in tema.
2. Scegli SOLO articoli il cui tema centrale è l'argomento indicato. Scarta cronaca generica,
   politica, sport, economia o società che nominano l'AI solo di passaggio (o nel footer del sito).
   Preferisci fonti primarie; evita duplicati della stessa notizia e contenuti promozionali.
3. Leggi ogni articolo scelto con read_url prima di riassumerlo e verifica che parli davvero
   dell'argomento: se dopo la lettura non è pertinente, escludilo.
4. Scrivi per ciascuno un riassunto fedele di 3-4 frasi, in italiano, senza inventare nulla.

Non includere articoli il cui URL è nell'elenco "già inviati".
Meglio zero articoli che articoli fuori tema.
Quando hai finito, rispondi SOLO con un oggetto JSON in questo formato:
{"articles": [{"title": "...", "url": "...", "summary": "...", "published_date": "YYYY-MM-DD o null"}]}
Se non trovi nulla di nuovo o di pertinente, rispondi {"articles": []}."""


def run_topic_agent(topic: str, tools: WebTools, known_urls: list[str]) -> list[dict[str, Any]]:
    """Ciclo agente per un argomento: pensa, usa i tool, osserva, poi produce il JSON finale."""
    settings = get_settings()
    client = get_client()
    known = "\n".join(known_urls) or "(nessuno)"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Argomento: {topic}\n"
                f"Seleziona al massimo {settings.max_articles_per_topic} articoli.\n\n"
                f"Già inviati (da escludere):\n{known}"
            ),
        },
    ]

    for step in range(MAX_STEPS):
        is_last_step = step == MAX_STEPS - 1
        started = time.perf_counter()
        try:
            response = client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                tools=TOOLS,
                # all'ultimo giro si impedisce di chiamare altri tool, per forzare la risposta finale
                tool_choice="none" if is_last_step else "auto",
                response_format={"type": "json_object"},
            )
        except Exception:
            metrics.record_usage(
                "agent_step",
                model=settings.llm_model,
                duration_ms=metrics.elapsed_ms(started),
                ok=False,
            )
            raise
        usage = response.usage
        metrics.record_usage(
            "agent_step",
            model=settings.llm_model,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            duration_ms=metrics.elapsed_ms(started),
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            return _parse_articles(message.content or "")

        for call in message.tool_calls:
            arguments = json.loads(call.function.arguments or "{}")
            logger.info("[%s] tool %s %s", topic, call.function.name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": tools.call(call.function.name, arguments),
                }
            )

    return []


def _parse_articles(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Risposta dell'agente non è JSON valido: %.200s", raw)
        return []
    articles = []
    for item in data.get("articles", []):
        if item.get("url") and item.get("title") and item.get("summary"):
            articles.append(
                {
                    "title": str(item["title"]).strip(),
                    "url": str(item["url"]).strip(),
                    "summary": str(item["summary"]).strip(),
                    "published_at": item.get("published_date") or None,
                }
            )
    return articles
