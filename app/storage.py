from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sqlite_vec

from .config import get_settings


def _schema(dim: int) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS articles (
        id           INTEGER PRIMARY KEY,
        url          TEXT NOT NULL UNIQUE,
        title        TEXT NOT NULL,
        topic        TEXT NOT NULL,
        summary      TEXT NOT NULL,
        content      TEXT NOT NULL DEFAULT '',
        published_at TEXT,
        created_at   TEXT NOT NULL,
        image_url    TEXT                         -- NULL = mai cercata, '' = cercata e non trovata
    );
    CREATE INDEX IF NOT EXISTS idx_articles_created ON articles(created_at);

    CREATE TABLE IF NOT EXISTS chunks (
        id         INTEGER PRIMARY KEY,
        article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        idx        INTEGER NOT NULL,
        text       TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_article ON chunks(article_id);

    CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{dim}]);

    CREATE TABLE IF NOT EXISTS recipients (
        id         INTEGER PRIMARY KEY,
        email      TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    );

    -- Monitoraggio -------------------------------------------------------
    CREATE TABLE IF NOT EXISTS runs (
        id            INTEGER PRIMARY KEY,
        trigger_kind  TEXT NOT NULL,              -- scheduler | manual | cli
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        status        TEXT NOT NULL,              -- running | ok | partial | failed
        found         INTEGER NOT NULL DEFAULT 0, -- articoli proposti dall'agente
        new_count     INTEGER NOT NULL DEFAULT 0, -- salvati in archivio
        duplicates    INTEGER NOT NULL DEFAULT 0, -- già presenti
        unreadable    INTEGER NOT NULL DEFAULT 0, -- salvati col solo riassunto (pagina non letta)
        failed        INTEGER NOT NULL DEFAULT 0, -- scartati per errore di indicizzazione
        emails_sent   INTEGER NOT NULL DEFAULT 0,
        emails_failed INTEGER NOT NULL DEFAULT 0,
        errors        TEXT NOT NULL DEFAULT '[]'  -- elenco JSON di messaggi
    );
    CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

    CREATE TABLE IF NOT EXISTS run_topics (
        id        INTEGER PRIMARY KEY,
        run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        topic     TEXT NOT NULL,
        found     INTEGER NOT NULL DEFAULT 0,
        new_count INTEGER NOT NULL DEFAULT 0,
        error     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_run_topics_run ON run_topics(run_id);

    CREATE TABLE IF NOT EXISTS skipped_items (
        id     INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        topic  TEXT NOT NULL,
        title  TEXT NOT NULL,
        url    TEXT NOT NULL,
        reason TEXT NOT NULL                      -- duplicate | embedding_failed
    );
    CREATE INDEX IF NOT EXISTS idx_skipped_run ON skipped_items(run_id);

    CREATE TABLE IF NOT EXISTS api_usage (
        id          INTEGER PRIMARY KEY,
        ts          TEXT NOT NULL,
        run_id      INTEGER,                      -- NULL per le chiamate fuori da una raccolta
        operation   TEXT NOT NULL,
        model       TEXT,
        tokens_in   INTEGER NOT NULL DEFAULT 0,
        tokens_out  INTEGER NOT NULL DEFAULT 0,
        units       INTEGER NOT NULL DEFAULT 0,   -- chiamate / crediti / email
        cost_usd    REAL NOT NULL DEFAULT 0,      -- stima, non fattura
        price_known INTEGER NOT NULL DEFAULT 1,
        duration_ms INTEGER,
        ok          INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_usage_ts ON api_usage(ts);
    CREATE INDEX IF NOT EXISTS idx_usage_run ON api_usage(run_id);

    CREATE TABLE IF NOT EXISTS queries (
        id         INTEGER PRIMARY KEY,
        ts         TEXT NOT NULL,
        question   TEXT NOT NULL,
        topic      TEXT,
        days       INTEGER,
        latency_ms INTEGER NOT NULL,
        ok         INTEGER NOT NULL,
        error      TEXT,
        sources    INTEGER NOT NULL DEFAULT 0,
        cost_usd   REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_queries_ts ON queries(ts);
    """


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = Path(get_settings().db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    settings = get_settings()
    with connect() as conn:
        # EMAIL_TO riempie i destinatari solo alla creazione della tabella: se poi li togli
        # tutti dall'interfaccia, un riavvio non deve rimetterli.
        first_run = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'recipients'").fetchone() is None
        )
        conn.executescript(_schema(settings.embedding_dim))
        _add_missing_columns(conn)
        if first_run:
            for raw in re.split(r"[,;\s]+", settings.email_to):
                email = normalize_email(raw)
                if email:
                    conn.execute(
                        "INSERT OR IGNORE INTO recipients (email, created_at) VALUES (?, ?)",
                        (email, now_iso()),
                    )


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Aggiorna un database creato da una versione precedente: CREATE IF NOT EXISTS non lo fa."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    if "image_url" not in columns:
        conn.execute("ALTER TABLE articles ADD COLUMN image_url TEXT")


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254


def normalize_email(raw: str) -> str | None:
    """Restituisce l'indirizzo in minuscolo se ha un formato plausibile, altrimenti None."""
    email = raw.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_PATTERN.match(email):
        return None
    return email


def list_recipients() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT id, email FROM recipients ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def add_recipient(email: str) -> bool:
    """True se l'indirizzo è stato aggiunto, False se era già presente."""
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO recipients (email, created_at) VALUES (?, ?) ON CONFLICT(email) DO NOTHING",
            (email, now_iso()),
        )
    return cursor.rowcount == 1


def remove_recipient(recipient_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM recipients WHERE id = ?", (recipient_id,))
    return cursor.rowcount == 1


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago_iso(days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def url_exists(url: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,)).fetchone()
    return row is not None


def recent_urls(days: int = 14, limit: int = 150) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT url FROM articles WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (_days_ago_iso(days), limit),
        ).fetchall()
    return [row["url"] for row in rows]


def insert_article(
    article: dict[str, Any],
    chunks: list[str],
    vectors: list[list[float]],
) -> int | None:
    """Salva articolo, chunk e vettori. Restituisce l'id, o None se l'URL c'era già."""
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO articles (url, title, topic, summary, content, published_at, created_at, image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO NOTHING
            """,
            (
                article["url"],
                article["title"],
                article["topic"],
                article["summary"],
                article.get("content", ""),
                article.get("published_at"),
                now_iso(),
                article.get("image_url") or "",  # la ricerca è già stata fatta: '' = nessuna immagine
            ),
        )
        if cursor.rowcount == 0:
            return None
        article_id = int(cursor.lastrowid)
        for idx, (text, vector) in enumerate(zip(chunks, vectors, strict=True)):
            chunk_cursor = conn.execute(
                "INSERT INTO chunks (article_id, idx, text) VALUES (?, ?, ?)",
                (article_id, idx, text),
            )
            conn.execute(
                "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                (int(chunk_cursor.lastrowid), sqlite_vec.serialize_float32(vector)),
            )
    return article_id


def articles_without_image_search(limit: int) -> list[dict[str, Any]]:
    """Articoli per cui l'immagine non è mai stata cercata (salvati prima di questa funzione)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, url FROM articles WHERE image_url IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_article_image(article_id: int, image_url: str | None) -> None:
    """Salva l'immagine trovata, o '' se non c'è: così non viene cercata di nuovo."""
    with connect() as conn:
        conn.execute(
            "UPDATE articles SET image_url = ? WHERE id = ?", (image_url or "", article_id)
        )


def list_topics() -> list[str]:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT topic FROM articles ORDER BY topic").fetchall()
    return [row["topic"] for row in rows]


def count_articles(topic: str | None = None) -> int:
    query = "SELECT COUNT(*) AS n FROM articles"
    params: list[Any] = []
    if topic:
        query += " WHERE topic = ?"
        params.append(topic)
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return int(row["n"])


def list_articles(
    topic: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = "SELECT id, url, title, topic, summary, published_at, created_at, image_url FROM articles"
    params: list[Any] = []
    if topic:
        query += " WHERE topic = ?"
        params.append(topic)
    query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, max(0, offset)])
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def search_chunks(
    query_vector: list[float],
    top_k: int,
    topic: str | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Ricerca semantica con filtri opzionali su argomento e periodo.

    Il KNN prende prima un insieme più largo (top_k * 10) e i filtri
    sui metadati sono applicati dopo: va bene per un archivio personale.
    """
    since = _days_ago_iso(days) if days else None
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.text, a.id AS article_id, a.title, a.url, a.topic,
                   a.published_at, a.created_at, a.image_url, v.distance
            FROM (
                SELECT rowid, distance FROM vec_chunks
                WHERE embedding MATCH ? AND k = ?
            ) AS v
            JOIN chunks   AS c ON c.id = v.rowid
            JOIN articles AS a ON a.id = c.article_id
            WHERE (? IS NULL OR a.topic = ?)
              AND (? IS NULL OR a.created_at >= ?)
            ORDER BY v.distance
            LIMIT ?
            """,
            (
                sqlite_vec.serialize_float32(query_vector),
                max(top_k * 10, 50),
                topic,
                topic,
                since,
                since,
                top_k,
            ),
        ).fetchall()
    return [dict(row) for row in rows]
