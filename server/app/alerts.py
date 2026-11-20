"""Простой алерт о необработанных ошибках: сообщение в Telegram администратору (не чаще раза в 5 минут)."""
from __future__ import annotations

import logging
import time

from .config import Settings

_last = 0.0
log = logging.getLogger("lawtracker.alerts")


def notify_error(settings: Settings, sender, where: str, min_interval: float = 300.0) -> bool:
    global _last
    if not settings.alert_chat_id or not settings.bot_token:
        return False
    now = time.monotonic()
    if _last and now - _last < min_interval:
        return False
    _last = now
    try:
        return bool(sender.send(settings.alert_chat_id, f"LawTracker: ошибка сервера\n{where}\nПодробности в журнале."))
    except Exception:  # алерт не должен ронять запрос
        log.exception("Не удалось отправить алерт")
        return False
