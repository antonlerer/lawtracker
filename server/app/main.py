from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .config import Settings, get_settings
from .db import Base, make_engine, make_session_factory
from .errors import install_error_handlers
from .mailer import ConsoleMailer, Mailer, SmtpMailer
from .routers import account, auth, changes, company, misc, notifications
from .telegram import BotApiSender, Sender

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def create_app(settings: Settings | None = None, *, mailer: Mailer | None = None, telegram: Sender | None = None,
               today: Callable[[], date] | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="LawTracker API", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    engine = make_engine(settings.database_url)
    from . import models  # noqa: F401  (регистрирует таблицы)
    Base.metadata.create_all(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.mailer = mailer or (SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_user,
                                             settings.smtp_password, settings.mail_from)
                                  if settings.smtp_host else ConsoleMailer())
    app.state.telegram = telegram or BotApiSender(settings.bot_token)
    app.state.today = today
    install_error_handlers(app)

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        """Защита от CSRF поверх SameSite=Lax: изменяющий запрос должен быть JSON или нести X-Requested-With.

        Межсайтовая форма так сделать не может (нужен CORS-preflight, который мы не разрешаем).
        Webhook Telegram и админ-методы (токен в заголовке) исключены.
        """
        path = request.url.path
        if request.method in UNSAFE and path.startswith("/api/") and "/telegram/webhook/" not in path:
            ctype = request.headers.get("content-type", "")
            if not (ctype.startswith("application/json") or request.headers.get("x-requested-with")
                    or request.headers.get("x-admin-token")):
                return JSONResponse({"error": {"code": "csrf", "message": "Недопустимый тип запроса", "fields": {}}},
                                    status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    for r in (auth.router, account.router, company.router, changes.router, notifications.router, misc.router):
        app.include_router(r)

    site = Path(settings.site_file) if settings.site_file else Path(__file__).resolve().parents[2] / "index.html"
    if site.is_file():
        @app.get("/", include_in_schema=False)
        def index():
            """Сайт отдаётся тем же сервером: запросы к /api идут на тот же адрес (без CORS и сторонних cookie)."""
            return FileResponse(site, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
    return app
