from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import metrics
from .config import get_settings

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com"
MAX_CONTENT_CHARS = 20_000  # testo salvato nell'archivio
MAX_AGENT_CHARS = 5_000  # testo mostrato all'agente
_TRACKING_KEYS = {"fbclid", "gclid", "ref"}


def normalize_url(url: str) -> str:
    """Toglie parametri di tracking, frammento e slash finale: l'URL è la chiave di deduplica."""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


class WebTools:
    """Tool a disposizione dell'agente. Tiene in cache il testo delle pagine lette."""

    def __init__(self) -> None:
        settings = get_settings()
        self._time_range = settings.search_time_range
        self._client = httpx.Client(
            base_url=TAVILY_URL,
            timeout=45,
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
        )
        self._pages: dict[str, str] = {}
        self._images: dict[str, list[str]] = {}

    def close(self) -> None:
        self._client.close()

    def images_for(self, url: str) -> list[str]:
        """Immagini trovate nella pagina quando è stata letta (vuoto se non letta o senza immagini)."""
        return self._images.get(normalize_url(url), [])

    def search_web(self, query: str) -> list[dict[str, Any]]:
        started = time.perf_counter()
        ok = False
        try:
            response = self._client.post(
                "/search",
                json={
                    "query": query,
                    "topic": "news",
                    "time_range": self._time_range,
                    "max_results": 6,
                },
            )
            response.raise_for_status()
            ok = True
        finally:
            metrics.record_usage(
                "search", units=1, duration_ms=metrics.elapsed_ms(started), ok=ok
            )
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": (item.get("content") or "")[:400],
                "published_date": item.get("published_date"),
            }
            for item in response.json().get("results", [])
        ]

    def read_url(self, url: str) -> str:
        """Testo completo della pagina (in cache). Stringa vuota se non leggibile."""
        key = normalize_url(url)
        if key in self._pages:
            return self._pages[key]
        text = ""
        started = time.perf_counter()
        ok = False
        try:
            # include_images non costa crediti in più: serve per scegliere la copertina
            response = self._client.post("/extract", json={"urls": [url], "include_images": True})
            response.raise_for_status()
            ok = True
            results = response.json().get("results", [])
            if results:
                text = (results[0].get("raw_content") or "")[:MAX_CONTENT_CHARS]
                self._images[key] = [
                    image for image in results[0].get("images") or [] if isinstance(image, str)
                ]
        except httpx.HTTPError as exc:
            logger.warning("Lettura fallita per %s: %s", url, exc)
        finally:
            metrics.record_usage(
                "extract", units=1, duration_ms=metrics.elapsed_ms(started), ok=ok
            )
        self._pages[key] = text
        return text

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Esegue il tool richiesto dall'agente e restituisce una stringa per il modello."""
        try:
            if name == "search_web":
                return json.dumps(self.search_web(arguments["query"]), ensure_ascii=False)
            if name == "read_url":
                text = self.read_url(arguments["url"])
                return text[:MAX_AGENT_CHARS] or "Contenuto non disponibile per questo URL."
            return f"Tool sconosciuto: {name}"
        except Exception as exc:  # l'errore torna all'agente, che può riprovare o cambiare strada
            logger.warning("Tool %s fallito: %s", name, exc)
            return f"Errore nel tool {name}: {exc}"
