"""Immagine di copertina degli articoli.

Si salva solo l'indirizzo dell'immagine (il browser la scarica dal sito originale): niente file
sul nostro disco. La copertina viene dai metadati della pagina (`og:image`, quella delle anteprime
sui social); se mancano si ripiega sulle immagini che Tavily ha trovato nella pagina, scartando
loghi, icone e simili.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from . import storage

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 8.0
FETCH_DEADLINE_SECONDS = 15.0  # tetto totale, anche se il sito risponde a gocce
MAX_HTML_BYTES = 300_000
MAX_REDIRECTS = 3
MAX_IMAGE_URL_CHARS = 1000
ALLOWED_PORTS = {None, 80, 443}
USER_AGENT = "Mozilla/5.0 (compatible; DailyAINews/1.0)"

# In ordine di preferenza
_META_KEYS = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")

_JUNK_EXTENSIONS = (".svg", ".gif", ".ico")
# Confronto per parola intera: «icon» non deve scartare «silicon»
_JUNK_WORDS = {
    "logo", "logos", "icon", "icons", "avatar", "avatars", "sprite", "sprites", "pixel", "favicon",
    "badge", "badges", "button", "buttons", "tracking", "spacer", "blank", "placeholder",
    "emoji", "gravatar", "ads", "advert", "banner",
}
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_TINY_SIZE = re.compile(r"(?<!\d)(\d{1,2})x(\d{1,2})(?!\d)")  # 1x1, 16x16, 32x32...


class _MetaImageParser(HTMLParser):
    """Raccoglie i meta tag delle immagini e si ferma alla fine dell'<head>."""

    def __init__(self) -> None:
        super().__init__()
        self.found: dict[str, str] = {}
        self.head_closed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta" or self.head_closed:  # i meta nel corpo della pagina non sono della pagina
            return
        values = {name.lower(): (value or "") for name, value in attrs}
        key = (values.get("property") or values.get("name") or "").strip().lower()
        content = values.get("content", "").strip()
        if key in _META_KEYS and content:
            self.found.setdefault(key, content)

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.head_closed = True


def clean_image_url(raw: str, base: str) -> str | None:
    """Indirizzo assoluto e sicuro da mettere in un <img>, oppure None.

    Solo http/https (niente `javascript:` o `data:`); http viene portato a https, perché
    la pagina è https e il browser blocca o riscrive il contenuto misto.
    """
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_IMAGE_URL_CHARS or re.search(r"\s", candidate):
        return None
    url = urljoin(base, candidate)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return "https" + url[len("http") :] if parts.scheme == "http" else url


def pick_from_candidates(candidates: list[str], base: str) -> str | None:
    """Prima immagine plausibile tra quelle della pagina: scarta loghi, icone, pixel e simili."""
    for raw in candidates:
        url = clean_image_url(raw, base)
        if not url:
            continue
        path = urlsplit(url).path.lower()
        if path.endswith(_JUNK_EXTENSIONS):
            continue
        if _JUNK_WORDS.intersection(_WORD_SPLIT.split(path)):
            continue
        tiny = _TINY_SIZE.search(path)
        if tiny and int(tiny.group(1)) < 100 and int(tiny.group(2)) < 100:
            continue
        return url
    return None


def _is_public_host(host: str) -> bool:
    """False per indirizzi interni (rete locale, loopback, metadati AWS...) o host che non si risolvono."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return False
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            return False
    return bool(infos)


def _is_safe_to_fetch(url: str) -> bool:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in {"http", "https"}
        and bool(parts.hostname)
        and port in ALLOWED_PORTS
        and _is_public_host(parts.hostname or "")
    )


def fetch_page_head(url: str, client: httpx.Client | None = None) -> str:
    """Scarica l'inizio della pagina HTML (fino alla fine dell'<head>). Stringa vuota se non si può.

    Le pagine arrivano da una ricerca sul web, quindi il server non deve poter essere usato per
    raggiungere la rete interna: ogni indirizzo, anche dopo un reindirizzamento, deve essere pubblico.
    """
    own_client = client is None
    http = client or httpx.Client(
        timeout=FETCH_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not _is_safe_to_fetch(current):
                logger.info("Indirizzo non consentito per la ricerca dell'immagine: %s", current)
                return ""
            with http.stream("GET", current, follow_redirects=False) as response:
                location = response.headers.get("location")
                if response.is_redirect and location:
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
                    return ""
                return _read_head(response)
        return ""
    finally:
        if own_client:
            http.close()


def _read_head(response: httpx.Response) -> str:
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    data = b""
    for chunk in response.iter_bytes():
        data += chunk
        if len(data) >= MAX_HTML_BYTES or b"</head" in data.lower() or time.monotonic() > deadline:
            break
    return data.decode(response.encoding or "utf-8", errors="ignore")


def cover_from_metadata(page_url: str, client: httpx.Client | None = None) -> str | None:
    try:
        head = fetch_page_head(page_url, client)
    except Exception as exc:  # una pagina che non risponde non deve fermare la raccolta
        logger.info("Pagina non raggiungibile per l'immagine (%s): %s", page_url, exc)
        return None
    parser = _MetaImageParser()
    try:
        parser.feed(head)
    except Exception:  # HTML molto rotto: si usa quello che il parser ha già trovato
        logger.debug("HTML non leggibile fino in fondo: %s", page_url)
    for key in _META_KEYS:
        if key in parser.found:
            url = clean_image_url(parser.found[key], page_url)
            if url:
                return url
    return None


def find_cover_image(
    page_url: str, candidates: list[str], client: httpx.Client | None = None
) -> str | None:
    """Copertina dell'articolo: metadati della pagina, altrimenti la migliore tra `candidates`."""
    return cover_from_metadata(page_url, client) or pick_from_candidates(candidates, page_url)


def backfill(limit: int = 200) -> tuple[int, int]:
    """Cerca l'immagine degli articoli salvati prima di questa funzione (solo dai metadati: nessun
    credito Tavily). Restituisce (articoli controllati, immagini trovate)."""
    articles = storage.articles_without_image_search(limit)
    found = 0
    for article in articles:
        image = cover_from_metadata(article["url"])
        storage.set_article_image(article["id"], image)
        found += int(image is not None)
    return len(articles), found


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    storage.init_db()
    checked, found = backfill()
    print(f"Controllati {checked} articoli, immagini trovate: {found}")
