from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

log = logging.getLogger("lawtracker.mail")


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> bool: ...


class ConsoleMailer:
    """Без SMTP письма пишутся в лог; `outbox` нужен тестам."""

    def __init__(self) -> None:
        self.outbox: list[dict] = []

    def send(self, to: str, subject: str, body: str) -> bool:
        self.outbox.append({"to": to, "subject": subject, "body": body})
        log.info("mail to=%s subject=%s", to, subject)
        return True


class SmtpMailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str) -> None:
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send(self, to: str, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
            return True
        except Exception:  # письмо не должно ронять запрос; ошибка уходит в лог
            log.exception("smtp send failed to=%s", to)
            return False
