from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import auth, pipeline, rag, storage
from .backup import run_backup
from .config import get_settings
from .templating import templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SESSION_MAX_AGE = 30 * 24 * 3600
MAX_QUESTION_CHARS = 500


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    storage.init_db()

    # Lo scheduler vive nel processo: per questo l'app gira con UN solo worker.
    scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.timezone))
    job_defaults: dict[str, Any] = {"coalesce": True, "misfire_grace_time": 3600, "max_instances": 1}
    scheduler.add_job(
        pipeline.run_pipeline,
        CronTrigger(hour=settings.digest_hour, minute=settings.digest_minute),
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
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "show_nav": True,
        "topics": storage.list_topics(),
        "selected_topic": topic or "",
        "articles": storage.list_articles(topic=topic or None),
        "running": pipeline.is_running(),
        "flash": None,
        "question": "",
        "days": 0,
        "result": None,
        "error": None,
    }
    context.update(extra or {})
    return context


@app.get("/", response_class=HTMLResponse)
def index(request: Request, topic: str = "", flash: str = "") -> Response:
    return templates.TemplateResponse(
        request, "index.html", _page_context(topic, {"flash": flash or None})
    )


@app.post("/ask", response_class=HTMLResponse)
def ask(
    request: Request,
    question: str = Form(...),
    topic: str = Form(""),
    days: int = Form(0),
) -> Response:
    question = question.strip()[:MAX_QUESTION_CHARS]
    extra: dict[str, Any] = {"question": question, "days": days}
    if question:
        try:
            extra["result"] = rag.answer(question, topic=topic or None, days=days or None)
        except Exception:
            logger.exception("Errore nel RAG")
            extra["error"] = "Qualcosa è andato storto durante la ricerca. Riprova."
    return templates.TemplateResponse(request, "index.html", _page_context(None, extra))


@app.post("/run")
def run_now() -> Response:
    if pipeline.is_running():
        message = "La raccolta è già in corso."
    else:
        threading.Thread(target=pipeline.run_pipeline, daemon=True).start()
        message = "Raccolta avviata: ci vorrà qualche minuto, poi ricarica la pagina."
    return RedirectResponse(f"/?flash={message}", status_code=303)
