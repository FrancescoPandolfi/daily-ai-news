from __future__ import annotations

import json
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

from . import auth, metrics, monitoring, pipeline, rag, storage
from .backup import run_backup
from .config import get_settings
from .templating import render_answer, templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SESSION_MAX_AGE = 30 * 24 * 3600
MAX_QUESTION_CHARS = 500
MAX_RECIPIENTS = 20
ARTICLES_PER_PAGE = 20
RAG_ERROR_MESSAGE = "Qualcosa è andato storto durante la ricerca. Riprova."


def _archive_page(topic: str | None, page: int) -> dict[str, Any]:
    """Carica una pagina dell'archivio e i metadati per i controlli Precedente/Successiva."""
    total = storage.count_articles(topic)
    total_pages = max(1, (total + ARTICLES_PER_PAGE - 1) // ARTICLES_PER_PAGE) if total else 1
    page = min(max(1, page), total_pages)
    offset = (page - 1) * ARTICLES_PER_PAGE
    return {
        "articles": storage.list_articles(
            topic=topic, limit=ARTICLES_PER_PAGE, offset=offset
        ),
        "page": page,
        "total_pages": total_pages,
        "total_articles": total,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    storage.init_db()
    interrupted = metrics.fail_orphan_runs()
    if interrupted:
        logger.warning("%d raccolta/e interrotta/e da un riavvio, segnate come fallite", interrupted)

    # Lo scheduler vive nel processo: per questo l'app gira con UN solo worker.
    scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.timezone))
    application.state.scheduler = scheduler
    job_defaults: dict[str, Any] = {"coalesce": True, "misfire_grace_time": 3600, "max_instances": 1}
    scheduler.add_job(
        pipeline.run_pipeline,
        CronTrigger(hour=settings.digest_hour, minute=settings.digest_minute),
        kwargs={"trigger": "scheduler"},
        id="daily_digest",
        **job_defaults,
    )
    scheduler.add_job(run_backup, CronTrigger(hour=3, minute=0), id="daily_backup", **job_defaults)
    scheduler.start()
    logger.info(
        "Scheduler avviato: digest alle %02d:%02d (%s)",
        settings.digest_hour,
        settings.digest_minute,
        settings.timezone,
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


settings = get_settings()

# Niente documentazione automatica: l'app non espone nulla senza login.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# L'ordine conta: l'ultimo middleware aggiunto è il più esterno.
# La sessione deve avvolgere l'autenticazione, che ne legge il contenuto.
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.base_url.startswith("https"),
    same_site="lax",
    max_age=SESSION_MAX_AGE,
)
app.include_router(auth.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _page_context(
    topic: str | None,
    extra: dict[str, Any] | None = None,
    page: int = 1,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "show_nav": True,
        "topics": storage.list_topics(),
        "selected_topic": topic or "",
        "ask_topic": "",
        "running": pipeline.is_running(),
        "recipients_ui": settings.recipients_ui,
        "sandbox_sender": settings.email_from.lower().rstrip("> ").endswith("@resend.dev"),
        "recipients": storage.list_recipients(),
        "max_recipients": MAX_RECIPIENTS,
        "flash": None,
        "flash_in_dialog": False,
        "question": "",
        "days": 0,
        "result": None,
        "error": None,
    }
    context.update(_archive_page(topic or None, page))
    context.update(extra or {})
    return context


def _redirect_with_flash(request: Request, text: str, in_dialog: bool = False) -> Response:
    """Salva un messaggio in sessione e torna alla home (pattern post/redirect/get)."""
    request.session["flash"] = {"text": text, "dialog": in_dialog}
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, topic: str = "", page: int = 1) -> Response:
    flash = request.session.pop("flash", None) or {}
    extra = {"flash": flash.get("text"), "flash_in_dialog": bool(flash.get("dialog"))}
    return templates.TemplateResponse(
        request, "index.html", _page_context(topic, extra, page=page)
    )


@app.post("/ask", response_class=HTMLResponse)
def ask(
    request: Request,
    question: str = Form(...),
    topic: str = Form(""),
    days: int = Form(0),
) -> Response:
    question = question.strip()[:MAX_QUESTION_CHARS]
    extra: dict[str, Any] = {"question": question, "days": days, "ask_topic": topic}
    if question:
        try:
            extra["result"] = rag.answer(question, topic=topic or None, days=days or None)
        except Exception:
            logger.exception("Errore nel RAG")
            extra["error"] = RAG_ERROR_MESSAGE
    return templates.TemplateResponse(request, "index.html", _page_context(None, extra))


def _ndjson(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _answer_events(question: str, topic: str | None, days: int | None) -> Iterator[str]:
    """Traduce gli eventi del RAG in righe JSON per il browser.

    Alla fine manda già pronto l'HTML della risposta e delle fonti: così la pagina non deve
    rifare in JavaScript le citazioni cliccabili e le schede delle fonti.
    """
    try:
        for event in rag.answer_stream(question, topic=topic, days=days):
            if event["type"] == "delta":
                yield _ndjson(event)
            else:
                sources_html = templates.get_template("_sources.html").render(result=event)
                yield _ndjson(
                    {
                        "type": "final",
                        "answer_html": str(render_answer(event["answer"])),
                        "sources_html": sources_html,
                    }
                )
    except Exception:
        logger.exception("Errore nel RAG")
        yield _ndjson({"type": "error", "message": RAG_ERROR_MESSAGE})


@app.post("/ask/stream")
def ask_stream(
    question: str = Form(...),
    topic: str = Form(""),
    days: int = Form(0),
) -> Response:
    """Come /ask, ma la risposta arriva a pezzi (una riga JSON per evento) mentre viene scritta."""
    question = question.strip()[:MAX_QUESTION_CHARS]
    if not question:
        return Response("Domanda vuota", status_code=400)
    return StreamingResponse(
        _answer_events(question, topic or None, days or None),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # chiede a nginx di non tenere in coda i pezzi
        },
    )


@app.post("/run")
def run_now(request: Request) -> Response:
    if pipeline.is_running():
        message = "La raccolta è già in corso."
    else:
        threading.Thread(
            target=pipeline.run_pipeline, kwargs={"trigger": "manual"}, daemon=True
        ).start()
        message = "Raccolta avviata: ci vorrà qualche minuto, poi ricarica la pagina."
    return _redirect_with_flash(request, message)


@app.get("/monitoraggio", response_class=HTMLResponse)
def monitoring_page(request: Request) -> Response:
    scheduler = getattr(request.app.state, "scheduler", None)
    job = scheduler.get_job("daily_digest") if scheduler else None
    report = monitoring.build_report(next_run=job.next_run_time if job else None)
    return templates.TemplateResponse(
        request,
        "monitoring.html",
        {"show_nav": True, "active": "monitoring", "report": report},
    )


@app.post("/recipients/add")
def add_recipient(request: Request, email: str = Form(...)) -> Response:
    if not settings.recipients_ui:
        return Response(status_code=404)
    normalized = storage.normalize_email(email)
    if normalized is None:
        message = "Indirizzo email non valido."
    elif len(storage.list_recipients()) >= MAX_RECIPIENTS:
        message = f"Hai raggiunto il limite di {MAX_RECIPIENTS} destinatari."
    elif storage.add_recipient(normalized):
        message = f"{normalized} riceverà il digest."
    else:
        message = f"{normalized} è già nell'elenco."
    return _redirect_with_flash(request, message, in_dialog=True)


@app.post("/recipients/remove")
def remove_recipient(request: Request, recipient_id: int = Form(...)) -> Response:
    if not settings.recipients_ui:
        return Response(status_code=404)
    message = (
        "Destinatario rimosso."
        if storage.remove_recipient(recipient_id)
        else "Destinatario non trovato."
    )
    return _redirect_with_flash(request, message, in_dialog=True)
