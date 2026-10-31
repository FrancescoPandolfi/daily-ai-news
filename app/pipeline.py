from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Literal, assert_never

from . import metrics, storage
from .agent import run_topic_agent
from .config import get_settings
from .images import find_cover_image
from .indexing import chunk_text
from .llm import embed
from .mailer import DigestResult, send_digest
from .tools import WebTools, normalize_url

logger = logging.getLogger(__name__)

_lock = threading.Lock()

TriggerKind = Literal["scheduler", "manual", "cli"]
SaveStatus = Literal["new", "duplicate", "embedding_failed"]


@dataclass(frozen=True)
class SaveOutcome:
    status: SaveStatus
    article: dict[str, Any] | None = None
    unreadable: bool = False  # salvato, ma solo col riassunto: la pagina non era leggibile


@dataclass
class RunStats:
    found: int = 0
    new_count: int = 0
    duplicates: int = 0
    unreadable: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def is_running() -> bool:
    return _lock.locked()


def run_pipeline(send_email: bool = True, trigger: TriggerKind = "scheduler") -> int:
    """Ricerca, salva e indicizza le notizie nuove, poi invia il digest.

    Restituisce il numero di articoli nuovi. Se la pipeline è già in esecuzione
    non fa nulla (evita doppi lanci da scheduler e pulsante manuale).
    """
    if not _lock.acquire(blocking=False):
        logger.info("Pipeline già in esecuzione, salto")
        return 0
    try:
        return _run(send_email, trigger)
    finally:
        _lock.release()


def _run(send_email: bool, trigger: TriggerKind) -> int:
    settings = get_settings()
    run_id = metrics.start_run(trigger)
    token = metrics.current_run_id.set(run_id)
    stats = RunStats()
    digest = DigestResult()
    new_articles: list[dict[str, Any]] = []
    status = "failed"
    tools: WebTools | None = None

    try:
        known_urls = storage.recent_urls()
        tools = WebTools()
        topics_ok = 0

        for topic in settings.topics:
            try:
                found = run_topic_agent(topic, tools, known_urls)
            except Exception as exc:
                logger.exception("Agente fallito per l'argomento %r", topic)
                message = metrics.describe_error(exc)
                stats.errors.append(f"{topic}: {message}")
                metrics.add_run_topic(run_id, topic, 0, 0, message)
                continue
            topics_ok += 1

            topic_new = 0
            for item in found:
                outcome = _save(item, topic, tools)
                match outcome.status:
                    case "new":
                        topic_new += 1
                        stats.new_count += 1
                        stats.unreadable += int(outcome.unreadable)
                        if outcome.article:
                            new_articles.append(outcome.article)
                            known_urls.append(outcome.article["url"])
                    case "duplicate":
                        stats.duplicates += 1
                        metrics.add_skipped(run_id, topic, item["title"], item["url"], "duplicate")
                    case "embedding_failed":
                        stats.failed += 1
                        metrics.add_skipped(
                            run_id, topic, item["title"], item["url"], "embedding_failed"
                        )
                    case _:
                        assert_never(outcome.status)
            stats.found += len(found)
            metrics.add_run_topic(run_id, topic, len(found), topic_new, None)

        if send_email and new_articles:
            digest = send_digest(new_articles)

        if topics_ok == 0:
            status = "failed"
        elif stats.errors or stats.failed or digest.failed:
            status = "partial"
        else:
            status = "ok"
    except Exception as exc:
        # Un guasto imprevisto non deve far saltare lo scheduler: lo si registra e basta.
        logger.exception("Pipeline interrotta da un errore imprevisto")
        stats.errors.append(metrics.describe_error(exc))
        status = "failed"
    finally:
        # La chiusura della raccolta viene prima di tutto il resto: se tools.close() fallisse,
        # la raccolta non deve restare segnata "in corso" per sempre.
        metrics.finish_run(
            run_id,
            status=status,
            found=stats.found,
            new_count=stats.new_count,
            duplicates=stats.duplicates,
            unreadable=stats.unreadable,
            failed=stats.failed,
            emails_sent=digest.sent,
            emails_failed=digest.failed,
            errors=stats.errors,
        )
        metrics.current_run_id.reset(token)
        if tools is not None:
            tools.close()

    logger.info("Pipeline %s: %d articoli nuovi", status, stats.new_count)
    return stats.new_count


def _save(item: dict[str, Any], topic: str, tools: WebTools) -> SaveOutcome:
    url = normalize_url(item["url"])
    if storage.url_exists(url):
        return SaveOutcome("duplicate")

    # il testo completo si salva insieme al riassunto: il riassunto perde dettagli ricercabili
    content = tools.read_url(item["url"])
    image_url = find_cover_image(item["url"], tools.images_for(item["url"]))
    article = {**item, "url": url, "topic": topic, "content": content, "image_url": image_url}

    chunks = chunk_text(article["title"], f"{article['summary']}\n\n{content}")
    try:
        vectors, _ = embed(chunks, operation="index_embed")
    except Exception:
        logger.exception("Embedding fallito per %s", url)
        return SaveOutcome("embedding_failed")

    if storage.insert_article(article, chunks, vectors) is None:
        return SaveOutcome("duplicate")
    return SaveOutcome("new", article=article, unreadable=not content)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    storage.init_db()
    metrics.fail_orphan_runs()
    run_pipeline(send_email=False, trigger="cli")
