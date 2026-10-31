from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

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

    def close(self) -> None:
        self._client.close()

    def search_web(self, query: str) -> list[dict[str, Any]]:
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
        try:
            response = self._client.post("/extract", json={"urls": [url]})
            response.raise_for_status()
            results = response.json().get("results", [])
            if results:
                text = (results[0].get("raw_content") or "")[:MAX_CONTENT_CHARS]
        except httpx.HTTPError as exc:
            logger.warning("Lettura fallita per %s: %s", url, exc)
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
