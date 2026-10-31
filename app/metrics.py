"""Scrittura delle metriche. Un errore qui non deve MAI far fallire una raccolta o una risposta."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import storage
from .config import get_settings
from .pricing import TAVILY_FREE_CREDITS_PER_MONTH, Operation, estimate_cost

logger = logging.getLogger(__name__)

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TAVILY_OPERATIONS = ("search", "extract")

# Id della raccolta in corso nel thread corrente: permette di attribuire ogni chiamata
# alle API alla sua raccolta senza passarlo a mano in tutte le funzioni.
current_run_id: ContextVar[int | None] = ContextVar("current_run_id", default=None)

MAX_ERROR_CHARS = 300


def month_start_ts() -> str:
    """Inizio del mese corrente nel fuso dell'app, in UTC (il formato con cui si salvano le date)."""
    tz = ZoneInfo(get_settings().timezone)
    start = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _tavily_free_credits_left(conn: sqlite3.Connection) -> int:
    used = conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM api_usage WHERE operation IN ('search', 'extract') AND ts >= ?",
        (month_start_ts(),),
    ).fetchone()[0]
    return max(0, TAVILY_FREE_CREDITS_PER_MONTH - int(used))


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def describe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


def record_usage(
    operation: Operation,
    *,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    units: int = 0,
    duration_ms: int | None = None,
    ok: bool = True,
) -> float:
    """Salva una chiamata a un servizio esterno. Restituisce il costo stimato in USD."""
    cost, price_known = 0.0, True
    try:
        with storage.connect() as conn:
            free_left = _tavily_free_credits_left(conn) if operation in TAVILY_OPERATIONS else 0
            cost, price_known = estimate_cost(
                operation, model, tokens_in, tokens_out, units, free_units_left=free_left
            )
            conn.execute(
                """
                INSERT INTO api_usage (ts, run_id, operation, model, tokens_in, tokens_out,
                                       units, cost_usd, price_known, duration_ms, ok)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    storage.now_iso(),
                    current_run_id.get(),
                    operation,
                    model,
                    tokens_in,
                    tokens_out,
                    units,
                    cost,
                    int(price_known),
                    duration_ms,
                    int(ok),
                ),
            )
    except Exception:
        logger.exception("Metrica non salvata (%s)", operation)
    return cost


def record_query(
    question: str,
    topic: str | None,
    days: int | None,
    latency_ms: int,
    error: str | None,
    sources: int,
    cost_usd: float,
) -> None:
    try:
        with storage.connect() as conn:
            conn.execute(
                """
                INSERT INTO queries (ts, question, topic, days, latency_ms, ok, error, sources, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    storage.now_iso(),
                    question,
                    topic,
                    days,
                    latency_ms,
                    int(error is None),
                    error,
                    sources,
                    cost_usd,
                ),
            )
    except Exception:
        logger.exception("Metrica della domanda non salvata")


def start_run(trigger_kind: str) -> int:
    with storage.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO runs (trigger_kind, started_at, status) VALUES (?, ?, 'running')",
            (trigger_kind, storage.now_iso()),
        )
        return int(cursor.lastrowid)


def finish_run(
    run_id: int,
    *,
    status: str,
    found: int,
    new_count: int,
    duplicates: int,
    unreadable: int,
    failed: int,
    emails_sent: int,
    emails_failed: int,
    errors: list[str],
) -> None:
    try:
        with storage.connect() as conn:
            conn.execute(
                """
                UPDATE runs SET finished_at = ?, status = ?, found = ?, new_count = ?,
                       duplicates = ?, unreadable = ?, failed = ?, emails_sent = ?,
                       emails_failed = ?, errors = ?
                WHERE id = ?
                """,
                (
                    storage.now_iso(),
                    status,
                    found,
                    new_count,
                    duplicates,
                    unreadable,
                    failed,
                    emails_sent,
                    emails_failed,
                    json.dumps(errors, ensure_ascii=False),
                    run_id,
                ),
            )
    except Exception:
        logger.exception("Chiusura della raccolta %d non salvata", run_id)


def add_run_topic(run_id: int, topic: str, found: int, new_count: int, error: str | None) -> None:
    try:
        with storage.connect() as conn:
            conn.execute(
                "INSERT INTO run_topics (run_id, topic, found, new_count, error) VALUES (?, ?, ?, ?, ?)",
                (run_id, topic, found, new_count, error),
            )
    except Exception:
        logger.exception("Dettaglio argomento non salvato")


def add_skipped(run_id: int, topic: str, title: str, url: str, reason: str) -> None:
    try:
        with storage.connect() as conn:
            conn.execute(
                "INSERT INTO skipped_items (run_id, topic, title, url, reason) VALUES (?, ?, ?, ?, ?)",
                (run_id, topic, title, url, reason),
            )
    except Exception:
        logger.exception("Articolo scartato non registrato")


def fail_orphan_runs() -> int:
    """All'avvio: una raccolta rimasta 'in corso' è stata interrotta da un riavvio."""
    with storage.connect() as conn:
        cursor = conn.execute(
            """
            UPDATE runs SET status = 'failed', finished_at = ?,
                   errors = ?
            WHERE status = 'running'
            """,
            (
                storage.now_iso(),
                json.dumps(["Interrotta: l'app è stata riavviata durante la raccolta"]),
            ),
        )
        return cursor.rowcount
