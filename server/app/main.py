from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from . import alerts
from .config import Settings, get_settings
from .db import Base, make_engine, make_session_factory
from .errors import install_error_handlers
from .mailer import ConsoleMailer, Mailer, SmtpMailer
from .routers import account, admin, auth, beta, changes, company, misc, notifications
from .telegram import BotApiSender, Sender

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def create_app(settings: Settings | None = None, *, mailer: Mailer | None = None, telegram: Sender | None = None,
               today: Callable[[], date] | None = None, llm=None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="LawTracker API", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    engine = make_engine(settings.database_url)
    from . import models  # noqa: F401  (регистрирует таблицы)
    if settings.auto_create_tables:
        Base.metadata.create_all(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.mailer = mailer or (SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_user,
                                             settings.smtp_password, settings.mail_from)
                                  if settings.smtp_host else ConsoleMailer())
    app.state.telegram = telegram or BotApiSender(settings.bot_token)
    app.state.today = today
    from ingest.llm import make_llm
    app.state.llm = llm if llm is not None else make_llm(settings)      # None — модель не настроена
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
        try:
            response = await call_next(request)
        except Exception:  # необработанная ошибка: в лог, алерт в Telegram, ответ без деталей
            logging.getLogger("lawtracker").exception("Необработанная ошибка %s %s", request.method, path)
            alerts.notify_error(settings, request.app.state.telegram, f"{request.method} {path}")
            response = JSONResponse({"error": {"code": "server_error", "message": "Внутренняя ошибка сервера",
                                               "fields": {}}}, status_code=500)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    for r in (auth.router, account.router, company.router, changes.router, notifications.router, misc.router,
              beta.router, admin.router, admin.page_router):
        app.include_router(r)

    site = Path(settings.site_file) if settings.site_file else Path(__file__).resolve().parents[2] / "index.html"
    if site.is_file():
        @app.get("/", include_in_schema=False)
        def index():
            """Сайт отдаётся тем же сервером: запросы к /api идут на тот же адрес (без CORS и сторонних cookie)."""
            return FileResponse(site, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
    return app
