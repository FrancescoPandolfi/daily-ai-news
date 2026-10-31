from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Variabile d'ambiente mancante: {name}")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


def _flag(name: str) -> bool:
    return _optional(name).lower() in {"1", "true", "yes", "si", "on"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    tavily_api_key: str
    app_password: str
    session_secret: str

    resend_api_key: str
    email_to: str
    email_from: str
    email_reply_to: str
    recipients_ui: bool

    base_url: str
    topics: tuple[str, ...]
    max_articles_per_topic: int
    search_time_range: str

    llm_model: str
    embedding_model: str
    embedding_dim: int

    timezone: str
    digest_hour: int
    digest_minute: int

    db_path: str
    rag_top_k: int = 8


@lru_cache
def get_settings() -> Settings:
    topics = tuple(
        t.strip()
        for t in _optional("TOPICS", "Modelli linguistici e agenti AI").split(";")
        if t.strip()
    )
    return Settings(
        openai_api_key=_required("OPENAI_API_KEY"),
        tavily_api_key=_required("TAVILY_API_KEY"),
        app_password=_required("APP_PASSWORD"),
        session_secret=_required("SESSION_SECRET"),
        resend_api_key=_optional("RESEND_API_KEY"),
        email_to=_optional("EMAIL_TO"),
        email_from=_optional("EMAIL_FROM", "Daily AI News <onboarding@resend.dev>"),
        email_reply_to=_optional("EMAIL_REPLY_TO"),
        recipients_ui=_flag("RECIPIENTS_UI"),
        base_url=_optional("BASE_URL", "http://localhost:8000").rstrip("/"),
        topics=topics,
        max_articles_per_topic=int(_optional("MAX_ARTICLES_PER_TOPIC", "4")),
        search_time_range=_optional("SEARCH_TIME_RANGE", "day"),
        llm_model=_optional("LLM_MODEL", "gpt-4.1-mini"),
        embedding_model=_optional("EMBEDDING_MODEL", "text-embedding-3-small"),
        embedding_dim=int(_optional("EMBEDDING_DIM", "1536")),
        timezone=_optional("TIMEZONE", "Europe/Rome"),
        digest_hour=int(_optional("DIGEST_HOUR", "7")),
        digest_minute=int(_optional("DIGEST_MINUTE", "30")),
        db_path=_optional("DB_PATH", "data/news.db"),
    )
