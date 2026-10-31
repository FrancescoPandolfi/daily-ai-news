from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CITATION = re.compile(r"[ \t]*\[(\d+)\]")  # lo spazio prima lo gestisce il margine del badge
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def render_answer(text: str) -> Markup:
    """Mostra la risposta del modello: testo escapato, **grassetto** e citazioni [n] cliccabili.

    L'escape viene prima di tutto: le sostituzioni lavorano su testo già sicuro.
    """
    html = str(escape(text))
    html = _BOLD.sub(r"<strong>\1</strong>", html)
    html = _CITATION.sub(r'<a class="cite" href="#src-\1">\1</a>', html)
    return Markup(html)


def _to_local(value: str | datetime) -> datetime:
    moment = (
        datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        if isinstance(value, str)
        else value
    )
    return moment.astimezone(ZoneInfo(get_settings().timezone))


def local_datetime(value: str | datetime | None, fmt: str = "%d/%m %H:%M") -> str:
    """Data e ora nel fuso dell'app (i dati sono salvati in UTC)."""
    return _to_local(value).strftime(fmt) if value else "—"


def relative_time(value: str | datetime | None) -> str:
    """«3 ore fa», «tra 2 ore»: per capire a colpo d'occhio quanto è recente un evento."""
    if not value:
        return "—"
    delta = datetime.now(timezone.utc) - _to_local(value)
    future = delta < timedelta(0)
    seconds = abs(delta.total_seconds())
    if seconds < 60:
        text = "pochi secondi"
    elif seconds < 3600:
        text = f"{int(seconds // 60)} min"
    elif seconds < 86400:
        text = f"{int(seconds // 3600)} h"
    else:
        text = f"{int(seconds // 86400)} g"
    return f"tra {text}" if future else f"{text} fa"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    if total < 60:
        return f"{total} s"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {rest:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def milliseconds(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value} ms" if value < 1000 else f"{value / 1000:.1f} s".replace(".", ",")


def usd(value: float | None) -> str:
    """Importo in dollari con la virgola decimale; più cifre per le somme minuscole."""
    if value is None:
        return "—"
    if value == 0:
        return "$0"
    digits = 2 if value >= 1 else 3 if value >= 0.01 else 4
    return f"${value:.{digits}f}".replace(".", ",")


def decimal(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


templates.env.filters.update(
    answer=render_answer,
    localdt=local_datetime,
    ago=relative_time,
    duration=duration,
    ms=milliseconds,
    usd=usd,
    dec=decimal,
)
