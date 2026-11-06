"""Auth middleware, CSRF, templates and shared helpers for the UI and API."""

import hmac
import ipaddress
import secrets
import time
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from queue import Queue
from threading import Thread

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .. import __version__

PUBLIC = ("/login", "/static/", "/api/v1/health", "/metrics")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "–"


def _fmt_size(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


templates.env.filters["time"] = _fmt_time
templates.env.filters["size"] = _fmt_size
templates.env.globals["version"] = __version__


def svc(request: Request):
    return request.app.state.svc


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def render(request: Request, name: str, **ctx) -> object:
    s = svc(request)
    return templates.TemplateResponse(request, name, {
        "base": request.scope.get("root_path", ""),
        "csrf": csrf_token(request),
        "user": getattr(request.state, "user", None),
        "generated_key": s.store.generated_key,
        "locked": s.store.locked,
        "path": request.url.path,
        **ctx,
    })


def redirect(request: Request, path: str, flash: str | None = None) -> RedirectResponse:
    if flash:
        request.session["flash"] = flash
    return RedirectResponse(request.scope.get("root_path", "") + path, status_code=303)


async def csrf_guard(request: Request) -> None:
    """Router dependency: state-changing requests authenticated by cookie need the CSRF token."""
    if request.method in ("GET", "HEAD", "OPTIONS") or getattr(request.state, "auth", "") == "token":
        return
    sent = request.headers.get("X-CSRF-Token")
    if not sent and "form" in request.headers.get("content-type", ""):
        sent = str((await request.form()).get("csrf", ""))
    if not sent or not hmac.compare_digest(sent, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF token missing or wrong; reload the page")


class LoginLimiter:
    def __init__(self, attempts: int = 5, window: float = 300):
        self.attempts, self.window = attempts, window
        self.failures: dict[str, list[float]] = defaultdict(list)

    def blocked(self, ip: str) -> bool:
        now = time.time()
        self.failures[ip] = [t for t in self.failures[ip] if now - t < self.window]
        return len(self.failures[ip]) >= self.attempts

    def fail(self, ip: str) -> None:
        self.failures[ip].append(time.time())


LIMITER = LoginLimiter()


def _trusted(ip: str, nets: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(n, strict=False) for n in nets)
    except ValueError:
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = request.app.state.svc
        path = request.url.path
        root = request.scope.get("root_path", "")
        rel = path[len(root):] if root and path.startswith(root) else path
        request.state.user, request.state.auth = None, ""
        auth = request.headers.get("Authorization", "")
        sec = s.cfg.security
        client = request.client.host if request.client else ""
        if auth.startswith("Bearer ") and s.store.check_token(auth[7:].strip()):
            request.state.user, request.state.auth = "api", "token"
        elif sec.proxy_header and _trusted(client, sec.trusted_proxies) and request.headers.get(sec.proxy_header):
            request.state.user, request.state.auth = request.headers[sec.proxy_header], "proxy"
        elif request.session.get("user"):
            request.state.user, request.state.auth = request.session["user"], "session"
        if request.state.user or any(rel.startswith(p) for p in PUBLIC):
            return await call_next(request)
        if rel.startswith("/api/"):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse(f"{root}/login?next={rel}", status_code=303)


def zip_stream(files: Iterator[tuple[str, object]]) -> Iterator[bytes]:
    """Stream a ZIP while it's being built; files yields (archive path, open binary file)."""
    q: Queue[bytes | None | BaseException] = Queue(maxsize=16)

    class Writer:
        def write(self, b: bytes) -> int:
            q.put(bytes(b))
            return len(b)

        def flush(self) -> None:
            pass

    def produce() -> None:
        try:
            with zipfile.ZipFile(Writer(), "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:  # type: ignore[arg-type]
                for name, fobj in files:
                    with fobj as src, z.open(name, "w", force_zip64=True) as dst:  # type: ignore[attr-defined]
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
            q.put(None)
        except BaseException as e:  # noqa: BLE001
            q.put(e)

    Thread(target=produce, daemon=True).start()
    while (item := q.get()) is not None:
        if isinstance(item, BaseException):
            raise item
        yield item
