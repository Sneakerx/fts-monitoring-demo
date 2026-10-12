"""FastAPI application: web portal + JSON API, hosting the background services."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import plotly
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from ..config import Settings, get_settings
from ..models import User
from ..security import verify_password
from ..services import Services
from ..timeutil import utcnow
from . import admin, api, pages
from .auth import CurrentUser, Forbidden, LoginRequired, current_user
from .common import audit, render, services

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
PLOTLY_JS = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = Services(settings, start_scheduler=start_scheduler)
        app.state.services.start()
        yield
        app.state.services.stop()

    app = FastAPI(title="Instrument Monitoring Facility", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.web.secret_key,
                       max_age=settings.web.session_max_age_s, same_site="lax")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/static-vendor/plotly.min.js", include_in_schema=False)
    def plotly_js() -> FileResponse:
        return FileResponse(PLOTLY_JS, media_type="application/javascript",
                            headers={"Cache-Control": "max-age=86400"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    # ---- error handling ---------------------------------------------------
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": f"role '{exc.required}' required"}, status_code=403)
        user = CurrentUser(**request.session["user"])
        resp = render(request, "error.html", user, title="Access denied",
                      message=f"This page requires the '{exc.required}' role. You are logged in as "
                              f"'{user.username}' ({user.role}).")
        resp.status_code = 403
        return resp

    # ---- login / logout -------------------------------------------------------
    @app.get("/login", include_in_schema=False)
    def login_form(request: Request, next: str = "/"):
        return render(request, "login.html", None, next=next, error=None)

    @app.post("/login", include_in_schema=False)
    def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
        svc = services(request)
        with svc.db.session() as s:
            u = s.scalar(select(User).where(User.username == username))
            if u is None or not u.active or not verify_password(password, u.pw_hash):
                audit(s, username or "?", "login.failed")
                resp = render(request, "login.html", None, next=next, error="Invalid credentials")
                resp.status_code = 401
                return resp
            u.last_login = utcnow()
            audit(s, u.username, "login")
            request.session["user"] = {"username": u.username, "role": u.role, "full_name": u.full_name}
        if not next.startswith("/") or next.startswith("//"):
            next = "/"
        return RedirectResponse(next, status_code=303)

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request, user: CurrentUser = Depends(current_user)):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    app.include_router(pages.router)
    app.include_router(admin.router)
    app.include_router(api.router)
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.web.host, port=settings.web.port, log_level="warning")


if __name__ == "__main__":
    main()
