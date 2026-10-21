from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./lawtracker.db"))
    cookie_name: str = "lt_session"
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))
    session_days: int = field(default_factory=lambda: int(os.getenv("SESSION_DAYS", "14")))
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000"))
    timezone: str = field(default_factory=lambda: os.getenv("TZ_NAME", "Europe/Moscow"))
    # Telegram
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    bot_username: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_USERNAME", "LawTrackerBot"))
    telegram_webhook_secret: str = field(default_factory=lambda: os.getenv("TELEGRAM_WEBHOOK_SECRET", ""))
    # Почта (если SMTP_HOST пуст — письма пишутся в лог)
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    mail_from: str = field(default_factory=lambda: os.getenv("MAIL_FROM", "LawTracker <noreply@example.org>"))
    # Админ-доступ (если пуст — админские методы отключены)
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))
    # Ограничения тарифов (в демо отключены, в боевой версии включены)
    enforce_plan_limits: bool = field(default_factory=lambda: _bool("ENFORCE_PLAN_LIMITS", True))
    # Режим показа: владелец компании может сам переключать тариф (платежи не подключены). В продакшене выключен.
    demo_mode: bool = field(default_factory=lambda: _bool("DEMO_MODE", False))
    # Файл сайта, который сервер отдаёт по адресу «/» (по умолчанию ../index.html рядом с папкой server)
    site_file: str = field(default_factory=lambda: os.getenv("SITE_FILE", ""))
    # GigaChat для разбора актов (необязательно)
    gigachat_auth_key: str = field(default_factory=lambda: os.getenv("GIGACHAT_AUTH_KEY", ""))
    gigachat_scope: str = field(default_factory=lambda: os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"))
    gigachat_model: str = field(default_factory=lambda: os.getenv("GIGACHAT_MODEL", "GigaChat-2"))
    gigachat_verify_ssl: bool = field(default_factory=lambda: _bool("GIGACHAT_VERIFY_SSL", True))


def get_settings() -> Settings:
    return Settings()
