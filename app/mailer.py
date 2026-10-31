from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from typing import Any

import httpx

from .config import get_settings
from .templating import templates

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


def send_digest(articles: list[dict[str, Any]]) -> bool:
    """Invia via email il digest degli articoli nuovi, raggruppati per argomento."""
    settings = get_settings()
    if not settings.resend_api_key or not settings.email_to:
        logger.warning("Email non configurata (RESEND_API_KEY / EMAIL_TO): digest non inviato")
        return False

    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in articles:
        by_topic[article["topic"]].append(article)

    html = templates.env.get_template("digest_email.html").render(
        by_topic=dict(by_topic),
        total=len(articles),
        base_url=settings.base_url,
        today=date.today().strftime("%d/%m/%Y"),
    )
    response = httpx.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={
            "from": settings.email_from,
            "to": [settings.email_to],
            "subject": f"AI News del {date.today().strftime('%d/%m/%Y')} ({len(articles)} articoli)",
            "html": html,
        },
        timeout=30,
    )
    if response.is_error:
        logger.error("Invio email fallito: %s %s", response.status_code, response.text)
        return False
    logger.info("Digest inviato a %s", settings.email_to)
    return True
