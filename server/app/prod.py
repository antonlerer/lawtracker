"""Проверки боевой конфигурации и очистка демо-данных."""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings


def config_problems(s: Settings) -> list[tuple[str, str]]:
    """Список (уровень, сообщение). Уровень ERROR — запускать бету нельзя, WARN — желательно исправить."""
    out: list[tuple[str, str]] = []
    err = lambda m: out.append(("ERROR", m))  # noqa: E731
    warn = lambda m: out.append(("WARN", m))  # noqa: E731
    if s.app_env != "production":
        warn("APP_ENV не равен production")
    if s.demo_mode:
        err("DEMO_MODE=true: в боевой версии должен быть false (иначе код подтверждения показывается на экране, "
            "а владелец сам меняет тариф)")
    if not s.cookie_secure:
        err("COOKIE_SECURE=false: cookie сессии без флага Secure")
    if not s.admin_token:
        err("ADMIN_TOKEN не задан: админ-страница и методы /api/admin/* отключены (очередь проверки не работает)")
    elif len(s.admin_token) < 32:
        err("ADMIN_TOKEN короче 32 символов")
    if s.database_url.startswith("sqlite"):
        err("DATABASE_URL указывает на SQLite: для беты нужен PostgreSQL")
    if not s.base_url.startswith("https://"):
        err("BASE_URL должен начинаться с https://")
    if s.auto_create_tables:
        warn("AUTO_CREATE_TABLES=true: в продакшене схему ведёт Alembic (задайте false)")
    if not s.smtp_host:
        err("SMTP_HOST не задан: письма (код подтверждения, сброс пароля, дайджест) пишутся только в лог")
    if "example.org" in s.mail_from or "example.org" in s.support_email:
        err("MAIL_FROM / SUPPORT_EMAIL содержат example.org — задайте настоящие адреса")
    if not s.bot_token:
        warn("BOT_TOKEN не задан: Telegram не работает, скройте его на сайте или подключите бота")
    elif not s.telegram_webhook_secret:
        err("TELEGRAM_WEBHOOK_SECRET не задан при заданном BOT_TOKEN")
    if not (s.gigachat_auth_key or (s.llm_base_url and s.llm_model)):
        warn("ИИ-модель не настроена (GIGACHAT_AUTH_KEY или LLM_*): ingest создаст карточки без разбора")
    if not s.gigachat_verify_ssl:
        err("GIGACHAT_VERIFY_SSL=false: проверка сертификата отключена")
    if not s.alert_chat_id:
        warn("ALERT_TELEGRAM_CHAT_ID не задан: об ошибках сервера никто не узнает автоматически")
    if not s.beta_invite_only:
        warn("BETA_INVITE_ONLY=false: регистрация открыта всем (для закрытой беты задайте true)")
    if s.terms_version.endswith("draft"):
        warn("TERMS_VERSION помечена как draft: после утверждения юристом текстов политики и соглашения "
             "обновите версию")
    return out


def demo_change_ids() -> list[str]:
    from seed.load import SEED
    return [it["id"] for it in json.loads(SEED.read_text(encoding="utf-8"))]


def purge_demo(db: Session) -> dict:
    """Удалить демо-изменения (по идентификаторам из seed/changes.json) и демо-пользователя с его компанией."""
    from seed.load import DEMO_EMAIL

    from .models import Change, Company, Member, User
    removed = {"changes": 0, "users": 0, "companies": 0}
    for cid in demo_change_ids():
        ch = db.get(Change, cid)
        if ch is not None:
            db.delete(ch)
            removed["changes"] += 1
    user = db.scalar(select(User).where(User.email == DEMO_EMAIL))
    if user is not None:
        member = db.scalar(select(Member).where(Member.user_id == user.id))
        if member is not None and member.role == "owner":
            others = db.scalar(select(Member).where(Member.company_id == member.company_id, Member.id != member.id))
            company = db.get(Company, member.company_id)
            if company is not None and others is None:
                db.delete(company)
                removed["companies"] += 1
        db.flush()
        db.expire(user)
        db.delete(user)
        removed["users"] += 1
    db.commit()
    return removed
