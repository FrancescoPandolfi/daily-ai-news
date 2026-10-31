from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from . import metrics, storage
from .config import get_settings
from .templating import templates

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
# Resend accetta circa 2 richieste al secondo: una pausa tra un invio e l'altro evita i 429
SEND_INTERVAL_SECONDS = 0.6


@dataclass(frozen=True)
class DigestResult:
    sent: int = 0
    failed: int = 0
    skipped_reason: str | None = None


def send_digest(articles: list[dict[str, Any]]) -> DigestResult:
    """Invia il digest a tutti i destinatari, una email separata ciascuno.

    Un errore su un indirizzo non blocca gli altri.
    """
    settings = get_settings()
    recipients = [r["email"] for r in storage.list_recipients()]
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY mancante: digest non inviato")
        return DigestResult(skipped_reason="RESEND_API_KEY mancante")
    if not recipients:
        logger.warning("Nessun destinatario configurato: digest non inviato")
        return DigestResult(skipped_reason="nessun destinatario")

    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in articles:
        by_topic[article["topic"]].append(article)

    html = templates.env.get_template("digest_email.html").render(
        by_topic=dict(by_topic),
        total=len(articles),
        base_url=settings.base_url,
        today=date.today().strftime("%d/%m/%Y"),
    )
    subject = f"AI News del {date.today().strftime('%d/%m/%Y')} ({len(articles)} articoli)"

    sent = 0
    failed = 0
    with httpx.Client(
        headers={"Authorization": f"Bearer {settings.resend_api_key}"}, timeout=30
    ) as client:
        for index, email in enumerate(recipients):
            if index:
                time.sleep(SEND_INTERVAL_SECONDS)
            started = time.perf_counter()
            ok = False
            payload: dict[str, Any] = {
                "from": settings.email_from,
                "to": [email],
                "subject": subject,
                "html": html,
            }
            if settings.email_reply_to:
                payload["reply_to"] = settings.email_reply_to
            try:
                response = client.post(RESEND_URL, json=payload)
                if response.is_error:
                    logger.error(
                        "Invio fallito verso %s: %s %s", email, response.status_code, response.text
                    )
                else:
                    ok = True
            except httpx.HTTPError:
                logger.exception("Invio fallito verso %s", email)
            metrics.record_usage(
                "send_email", units=1, duration_ms=metrics.elapsed_ms(started), ok=ok
            )
            if ok:
                sent += 1
            else:
                failed += 1

    logger.info("Digest inviato a %d destinatari su %d", sent, len(recipients))
    return DigestResult(sent=sent, failed=failed)
