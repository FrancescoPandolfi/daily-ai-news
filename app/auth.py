from __future__ import annotations

import hmac
import time
from collections import defaultdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .config import get_settings
from .templating import templates

PUBLIC_PATHS = {"/login", "/health"}
MAX_FAILED_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60

router = APIRouter()
_failed_attempts: dict[str, list[float]] = defaultdict(list)


class AuthMiddleware(BaseHTTPMiddleware):
    """Blocca qualsiasi richiesta senza sessione valida, tranne /login e /health."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in PUBLIC_PATHS or request.session.get("auth") is True:
            return await call_next(request)
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=303)
        return Response("Non autorizzato", status_code=401)


def _is_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failed_attempts[ip] if now - t < WINDOW_SECONDS]
    _failed_attempts[ip] = recent
    return len(recent) >= MAX_FAILED_ATTEMPTS


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html", {"show_nav": False, "error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)) -> Response:
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"show_nav": False, "error": "Troppi tentativi. Riprova tra qualche minuto."},
            status_code=429,
        )

    expected = get_settings().app_password.encode()
    if not hmac.compare_digest(password.encode(), expected):
        _failed_attempts[ip].append(time.monotonic())
        return templates.TemplateResponse(
            request,
            "login.html",
            {"show_nav": False, "error": "Password errata."},
            status_code=401,
        )

    _failed_attempts.pop(ip, None)
    request.session.clear()
    request.session["auth"] = True
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
