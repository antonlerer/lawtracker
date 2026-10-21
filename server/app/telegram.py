"""Отправка в Telegram и разбор входящих сообщений бота (webhook)."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import TelegramLink, utcnow

log = logging.getLogger("lawtracker.telegram")
CODE_RE = re.compile(r"\bLT-[A-Z0-9]{4}\b", re.I)


class Sender(Protocol):
    def send(self, chat_id: str, text: str) -> bool: ...


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, chat_id: str, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True


class BotApiSender:
    def __init__(self, token: str) -> None:
        self.token = token

    def send(self, chat_id: str, text: str) -> bool:
        if not self.token:
            log.warning("BOT_TOKEN не задан: сообщение не отправлено")
            return False
        try:
            r = httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage", timeout=10,
                           json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
            return r.status_code == 200
        except httpx.HTTPError:
            log.exception("telegram send failed")
            return False


def effective_status(link: TelegramLink | None, now: datetime | None = None) -> str:
    """Статус для клиента: просроченный код превращается в 'error' (как в демо)."""
    if link is None:
        return "none"
    now = now or utcnow()
    if link.status == "pending" and link.code_expires and link.code_expires < now:
        return "error"
    return link.status


def handle_update(db: Session, update: dict, sender: Sender) -> str:
    """Обрабатывает Update от Telegram. Возвращает краткий итог для логов/тестов."""
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    chat_id = chat.get("id")
    if chat_id is None or chat.get("type") != "private":
        return "ignored"
    chat_id = str(chat_id)
    username = (msg.get("from") or {}).get("username") or ""

    if text.startswith("/start"):
        sender.send(chat_id, "Это бот LawTracker. Получите код в кабинете (раздел «Telegram») и отправьте его сюда.")
        return "start"
    if text.startswith("/stop"):
        link = db.scalar(select(TelegramLink).where(TelegramLink.chat_id == chat_id))
        if link:
            link.status, link.chat_id = "disabled", None
            db.commit()
        sender.send(chat_id, "Уведомления отключены. Подключить снова можно в кабинете.")
        return "stop"

    m = CODE_RE.search(text)
    if not m:
        sender.send(chat_id, "Не понял сообщение. Отправьте код подключения из кабинета, например LT-AB12.")
        return "unknown"
    code = m.group(0).upper()
    link = db.scalar(select(TelegramLink).where(TelegramLink.code == code, TelegramLink.status == "pending"))
    if link is None or (link.code_expires and link.code_expires < utcnow()):
        sender.send(chat_id, "Код не найден или устарел. Получите новый в кабинете.")
        return "bad_code"
    other = db.scalar(select(TelegramLink).where(TelegramLink.chat_id == chat_id, TelegramLink.user_id != link.user_id))
    if other is not None:  # один Telegram-аккаунт — один пользователь
        other.status, other.chat_id = "disabled", None
    link.status, link.chat_id, link.username = "connected", chat_id, ("@" + username) if username else ""
    link.code, link.code_expires, link.connected_at = None, None, utcnow()
    db.commit()
    sender.send(chat_id, "Готово: Telegram подключён. Дайджесты будут приходить сюда.")
    return "connected"
