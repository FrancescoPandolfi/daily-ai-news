from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

from .config import get_settings

logger = logging.getLogger(__name__)

KEEP_BACKUPS = 7


def run_backup() -> Path:
    """Copia consistente del database in <cartella dati>/backups, tenendo le ultime 7."""
    db_path = Path(get_settings().db_path)
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"news-{date.today().isoformat()}.db"

    source = sqlite3.connect(db_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)  # usa l'API di backup di SQLite: sicura anche con scritture in corso
    finally:
        target.close()
        source.close()

    for old in sorted(backup_dir.glob("news-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    logger.info("Backup creato: %s", destination)
    return destination
