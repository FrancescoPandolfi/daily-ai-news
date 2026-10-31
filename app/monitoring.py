"""Calcola i dati della pagina di monitoraggio a partire dalle tabelle delle metriche."""

from __future__ import annotations

import json
import math
import resource
import shutil
import socket
import ssl
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import storage
from .config import get_settings
from .metrics import TAVILY_OPERATIONS, TIMESTAMP_FORMAT
from .pricing import TAVILY_FREE_CREDITS_PER_MONTH

STARTED_AT = time.time()

DAYS_WINDOW = 30
RECENT_RUNS = 20
RECENT_QUERIES = 15
SLOW_QUERY_MS = 10_000
STALE_AFTER_HOURS = 30  # la raccolta è giornaliera: oltre 30 ore qualcosa non va
RUNNING_TOO_LONG_MINUTES = 60
CERT_CACHE_OK_SECONDS = 6 * 3600
CERT_CACHE_FAIL_SECONDS = 600

# etichetta mostrata per ciascun gruppo di operazioni
COST_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Agente · OpenAI", ("agent_step",)),
    ("Indicizzazione · OpenAI", ("index_embed",)),
    ("Domande · OpenAI", ("rag_embed", "rag_answer")),
    ("Ricerca web · Tavily", ("search", "extract")),
    ("Email · Resend", ("send_email",)),
]

_cert_cache: dict[str, Any] = {"at": 0.0, "ttl": 0.0, "days": None, "host": None}


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _to_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def build_report(next_run: datetime | None) -> dict[str, Any]:
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    with storage.connect() as conn:
        runs = _runs_section(conn)
        return {
            "status": _status_section(conn, now, next_run),
            "costs": _costs_section(conn, tz, local_now, month_start, today_start),
            "runs": runs,
            "rag": _rag_section(conn, now),
            "system": _system_section(conn, now),
        }


# --- stato ---------------------------------------------------------------------------


def _status_section(
    conn: sqlite3.Connection, now: datetime, next_run: datetime | None
) -> dict[str, Any]:
    last = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    backup_at = _last_backup()

    base: dict[str, Any] = {
        "next_run": next_run,
        "last_backup": backup_at,
        "last_run": dict(last) if last else None,
    }

    if last is None:
        return {
            **base,
            "level": "idle",
            "headline": "Nessuna raccolta ancora",
            "detail": "Avvia la prima dalla home con «Avvia raccolta».",
        }

    started = parse_ts(last["started_at"])
    if last["status"] == "running":
        minutes = int((now - started).total_seconds() // 60)
        if minutes > RUNNING_TOO_LONG_MINUTES:
            return {
                **base,
                "level": "warn",
                "headline": "Raccolta in corso da troppo tempo",
                "detail": f"È partita {minutes} minuti fa: potrebbe essersi bloccata.",
            }
        return {
            **base,
            "level": "running",
            "headline": "Raccolta in corso",
            "detail": f"Avviata {minutes} minuti fa. La pagina si aggiorna da sola.",
        }

    finished = parse_ts(last["finished_at"]) if last["finished_at"] else started
    hours_since = (now - finished).total_seconds() / 3600

    if last["status"] == "failed":
        level, headline, detail = "bad", "Ultima raccolta fallita", _first_error(last)
    elif last["status"] == "partial":
        level, headline, detail = (
            "warn",
            "Ultima raccolta completata con errori",
            _first_error(last),
        )
    else:
        level, headline = "ok", "Tutto a posto"
        detail = f"Ultima raccolta: {last['new_count']} articoli nuovi."

    if level in ("ok", "warn") and hours_since > STALE_AFTER_HOURS:
        level = "bad"
        headline = "Nessuna raccolta da molte ore"
        detail = f"L'ultima è finita {int(hours_since)} ore fa, ma dovrebbe essere giornaliera."

    return {**base, "level": level, "headline": headline, "detail": detail}


def _first_error(run: sqlite3.Row) -> str:
    errors = json.loads(run["errors"] or "[]")
    return errors[0] if errors else "Nessun dettaglio disponibile."


def _last_backup() -> datetime | None:
    backup_dir = Path(get_settings().db_path).parent / "backups"
    files = sorted(backup_dir.glob("news-*.db"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    return datetime.fromtimestamp(files[-1].stat().st_mtime, tz=timezone.utc)


# --- costi ---------------------------------------------------------------------------


def _costs_section(
    conn: sqlite3.Connection,
    tz: ZoneInfo,
    local_now: datetime,
    month_start: datetime,
    today_start: datetime,
) -> dict[str, Any]:
    window_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=DAYS_WINDOW - 1
    )
    since = _to_ts(min(month_start, window_start))
    rows = conn.execute(
        """
        SELECT ts, run_id, operation, tokens_in, tokens_out, units, cost_usd, price_known
        FROM api_usage WHERE ts >= ?
        """,
        (since,),
    ).fetchall()

    month_ts = _to_ts(month_start)
    today_ts = _to_ts(today_start)
    month_total = today_total = 0.0
    by_group: dict[str, dict[str, float]] = {
        label: {"cost": 0.0, "calls": 0, "tokens": 0} for label, _ in COST_GROUPS
    }
    group_of = {op: label for label, ops in COST_GROUPS for op in ops}
    tavily_credits = 0
    per_day: dict[str, float] = {}
    run_costs: dict[int, float] = {}

    for row in rows:
        local_day = parse_ts(row["ts"]).astimezone(tz).date().isoformat()
        per_day[local_day] = per_day.get(local_day, 0.0) + row["cost_usd"]
        if row["run_id"] is not None:
            run_costs[row["run_id"]] = run_costs.get(row["run_id"], 0.0) + row["cost_usd"]
        if row["ts"] >= month_ts:
            month_total += row["cost_usd"]
            group = by_group[group_of[row["operation"]]]
            group["cost"] += row["cost_usd"]
            group["calls"] += 1
            group["tokens"] += row["tokens_in"] + row["tokens_out"]
            if row["operation"] in TAVILY_OPERATIONS:
                tavily_credits += row["units"]
        if row["ts"] >= today_ts:
            today_total += row["cost_usd"]

    unknown_models = [
        r["model"]
        for r in conn.execute(
            "SELECT DISTINCT model FROM api_usage WHERE price_known = 0 AND model IS NOT NULL"
        )
    ]

    run_rows = conn.execute(
        "SELECT id, new_count FROM runs WHERE started_at >= ? AND status != 'running'",
        (_to_ts(window_start),),
    ).fetchall()
    costs_of_runs = [run_costs.get(r["id"], 0.0) for r in run_rows]
    month_runs = conn.execute(
        "SELECT id, new_count FROM runs WHERE started_at >= ? AND status != 'running'",
        (month_ts,),
    ).fetchall()
    month_new = sum(r["new_count"] for r in month_runs)
    month_run_cost = sum(run_costs.get(r["id"], 0.0) for r in month_runs)

    days = [
        (window_start + timedelta(days=i)).date().isoformat() for i in range(DAYS_WINDOW)
    ]
    peak = max((per_day.get(d, 0.0) for d in days), default=0.0)
    series = [
        {
            "date": d,
            "label": f"{d[8:10]}/{d[5:7]}",
            "cost": per_day.get(d, 0.0),
            "height": max(3.0, per_day.get(d, 0.0) / peak * 100) if peak else 3.0,
            "empty": per_day.get(d, 0.0) == 0.0,
            "today": d == local_now.date().isoformat(),
        }
        for d in days
    ]

    groups = [
        {"label": label, **by_group[label], "share": by_group[label]["cost"] / month_total * 100 if month_total else 0}
        for label, _ in COST_GROUPS
    ]
    return {
        "month": month_total,
        "today": today_total,
        "avg_per_run": statistics.mean(costs_of_runs) if costs_of_runs else None,
        "per_article": month_run_cost / month_new if month_new else None,
        "groups": groups,
        "series": series,
        "tavily_credits": tavily_credits,
        "tavily_free": TAVILY_FREE_CREDITS_PER_MONTH,
        "tavily_pct": min(100.0, tavily_credits / TAVILY_FREE_CREDITS_PER_MONTH * 100),
        "unknown_models": unknown_models,
        "month_label": month_start.strftime("%m/%Y"),
    }


# --- raccolte ------------------------------------------------------------------------


def _runs_section(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*, COALESCE(u.cost, 0) AS cost
        FROM runs r
        LEFT JOIN (SELECT run_id, SUM(cost_usd) AS cost FROM api_usage GROUP BY run_id) u
               ON u.run_id = r.id
        ORDER BY r.started_at DESC, r.id DESC
        LIMIT ?
        """,
        (RECENT_RUNS,),
    ).fetchall()
    if not rows:
        return []

    ids = [row["id"] for row in rows]
    marks = ",".join("?" * len(ids))
    topics: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for row in conn.execute(f"SELECT * FROM run_topics WHERE run_id IN ({marks}) ORDER BY id", ids):
        topics[row["run_id"]].append(dict(row))
    skipped: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for row in conn.execute(f"SELECT * FROM skipped_items WHERE run_id IN ({marks}) ORDER BY id", ids):
        skipped[row["run_id"]].append(dict(row))

    result = []
    for row in rows:
        run = dict(row)
        started = parse_ts(row["started_at"])
        run["duration_s"] = (
            (parse_ts(row["finished_at"]) - started).total_seconds() if row["finished_at"] else None
        )
        run["errors"] = json.loads(row["errors"] or "[]")
        run["topics"] = topics[row["id"]]
        run["skipped"] = skipped[row["id"]]
        run["discarded"] = row["duplicates"] + row["failed"]
        result.append(run)
    return result


# --- risposte (RAG) ------------------------------------------------------------------


def _percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _rag_section(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    since = _to_ts(now - timedelta(days=DAYS_WINDOW))
    rows = conn.execute(
        "SELECT * FROM queries WHERE ts >= ? ORDER BY ts DESC, id DESC", (since,)
    ).fetchall()
    ok_latencies = [r["latency_ms"] for r in rows if r["ok"]]
    errors = sum(1 for r in rows if not r["ok"])
    return {
        "count": len(rows),
        "errors": errors,
        "error_pct": errors / len(rows) * 100 if rows else 0.0,
        "median_ms": int(statistics.median(ok_latencies)) if ok_latencies else None,
        "p95_ms": _percentile(ok_latencies, 95),
        "avg_cost": statistics.mean(r["cost_usd"] for r in rows) if rows else None,
        "slow_ms": SLOW_QUERY_MS,
        "recent": [dict(r) for r in rows[:RECENT_QUERIES]],
    }


# --- sistema -------------------------------------------------------------------------


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _system_section(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    db_path = Path(get_settings().db_path)
    db_bytes = sum(
        p.stat().st_size for p in (db_path, Path(f"{db_path}-wal")) if p.exists()
    )
    disk = shutil.disk_usage(db_path.parent)
    return {
        "db_mb": db_bytes / 1_048_576,
        "articles": _count(conn, "articles"),
        "chunks": _count(conn, "chunks"),
        "recipients": _count(conn, "recipients"),
        "disk_used_pct": disk.used / disk.total * 100,
        "disk_free_gb": disk.free / 1_073_741_824,
        "rss_mb": _rss_mb(),
        "uptime_s": time.time() - STARTED_AT,
        "cert_days": _certificate_days_left(now),
    }


def _rss_mb() -> float:
    """Memoria del processo: su Linux quella attuale, altrove il picco."""
    try:
        with open("/proc/self/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1_048_576 if sys.platform == "darwin" else peak / 1024


def _certificate_days_left(now: datetime) -> int | None:
    """Giorni alla scadenza del certificato del dominio pubblico (None se non verificabile)."""
    parts = urlsplit(get_settings().base_url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    cache = _cert_cache
    if cache["host"] == parts.hostname and time.time() - cache["at"] < cache["ttl"]:
        return cache["days"]

    days: int | None = None
    try:
        context = ssl.create_default_context()
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=3) as sock:
            with context.wrap_socket(sock, server_hostname=parts.hostname) as tls:
                not_after = tls.getpeercert()["notAfter"]  # type: ignore[index]
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=timezone.utc)
        days = (expires - now).days
    except (OSError, ssl.SSLError, KeyError, TypeError):
        days = None

    cache.update(
        at=time.time(),
        ttl=CERT_CACHE_OK_SECONDS if days is not None else CERT_CACHE_FAIL_SECONDS,
        days=days,
        host=parts.hostname,
    )
    return days
