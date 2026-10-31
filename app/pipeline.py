from __future__ import annotations

import logging
import threading
from typing import Any

from . import storage
from .agent import run_topic_agent
from .config import get_settings
from .indexing import chunk_text
from .llm import embed
from .mailer import send_digest
from .tools import WebTools, normalize_url

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def is_running() -> bool:
    return _lock.locked()


def run_pipeline(send_email: bool = True) -> int:
    """Ricerca, salva e indicizza le notizie nuove, poi invia il digest.

    Restituisce il numero di articoli nuovi. Se la pipeline è già in esecuzione
    non fa nulla (evita doppi lanci da scheduler e pulsante manuale).
    """
    if not _lock.acquire(blocking=False):
        logger.info("Pipeline già in esecuzione, salto")
        return 0
    try:
        return _run(send_email)
    finally:
        _lock.release()


def _run(send_email: bool) -> int:
    settings = get_settings()
    known_urls = storage.recent_urls()
    new_articles: list[dict[str, Any]] = []

    tools = WebTools()
    try:
        for topic in settings.topics:
            try:
                found = run_topic_agent(topic, tools, known_urls)
            except Exception:
                logger.exception("Agente fallito per l'argomento %r", topic)
                continue
            for item in found:
                saved = _save(item, topic, tools)
                if saved:
                    new_articles.append(saved)
                    known_urls.append(saved["url"])
    finally:
        tools.close()

    logger.info("Pipeline completata: %d articoli nuovi", len(new_articles))
    if send_email and new_articles:
        send_digest(new_articles)
    return len(new_articles)


def _save(item: dict[str, Any], topic: str, tools: WebTools) -> dict[str, Any] | None:
    url = normalize_url(item["url"])
    if storage.url_exists(url):
        return None

    # il testo completo si salva insieme al riassunto: il riassunto perde dettagli ricercabili
    content = tools.read_url(item["url"])
    article = {**item, "url": url, "topic": topic, "content": content}

    chunks = chunk_text(article["title"], f"{article['summary']}\n\n{content}")
    try:
        vectors = embed(chunks)
    except Exception:
        logger.exception("Embedding fallito per %s", url)
        return None

    if storage.insert_article(article, chunks, vectors) is None:
        return None
    return article


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    storage.init_db()
    run_pipeline(send_email=False)
