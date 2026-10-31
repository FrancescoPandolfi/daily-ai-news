from __future__ import annotations

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
        created_at   TEXT NOT NULL
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
    with connect() as conn:
        conn.executescript(_schema(get_settings().embedding_dim))


def _now_iso() -> str:
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
            INSERT INTO articles (url, title, topic, summary, content, published_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO NOTHING
            """,
            (
                article["url"],
                article["title"],
                article["topic"],
                article["summary"],
                article.get("content", ""),
                article.get("published_at"),
                _now_iso(),
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


def list_topics() -> list[str]:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT topic FROM articles ORDER BY topic").fetchall()
    return [row["topic"] for row in rows]


def list_articles(topic: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    query = "SELECT id, url, title, topic, summary, published_at, created_at FROM articles"
    params: list[Any] = []
    if topic:
        query += " WHERE topic = ?"
        params.append(topic)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
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
                   a.published_at, a.created_at, v.distance
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
