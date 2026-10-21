"""Настройки уведомлений и рассылка дайджестов по расписанию."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog as C
from .changes import build_ctx, evaluate, serialize
from .config import Settings
from .mailer import Mailer
from .models import Change, Company, Member, NotificationLog, NotificationPrefs, TelegramLink, User
from .telegram import Sender, effective_status

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class NotifSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cabinet: bool = True
    email: bool = True
    email_freq: Literal["now", "daily", "weekly"] = "daily"
    tg: bool = True
    topics: list[str] = []
    min_impact: Literal["low", "med", "high"] = "med"
    days: list[int] = [1, 2, 3, 4, 5]
    time: str = "09:00"
    pause: bool = False
    pause_until: date | None = None

    @field_validator("topics")
    @classmethod
    def _topics(cls, v):
        bad = [t for t in v if t not in C.TOPICS]
        if bad:
            raise ValueError(f"Неизвестные тематики: {', '.join(bad)}")
        return list(dict.fromkeys(v))

    @field_validator("days")
    @classmethod
    def _days(cls, v):
        if not v or any(not isinstance(d, int) or not 1 <= d <= 7 for d in v):
            raise ValueError("Выберите хотя бы один день недели (1–7)")
        return sorted(set(v))

    @field_validator("time")
    @classmethod
    def _time(cls, v):
        if not TIME_RE.match(v):
            raise ValueError("Время в формате ЧЧ:ММ")
        return v


def load_prefs(db: Session, user_id: int) -> NotifSettings:
    row = db.get(NotificationPrefs, user_id)
    return NotifSettings.model_validate(row.data) if row and row.data else NotifSettings()


def save_prefs(db: Session, user_id: int, prefs: NotifSettings) -> None:
    row = db.get(NotificationPrefs, user_id)
    data = prefs.model_dump(mode="json")
    if row is None:
        db.add(NotificationPrefs(user_id=user_id, data=data))
    else:
        row.data = data
    db.commit()


def paused(prefs: NotifSettings, today: date) -> bool:
    return prefs.pause and (prefs.pause_until is None or today <= prefs.pause_until)


def _due(prefs: NotifSettings, local_now: datetime, now_utc: datetime, last_sent: datetime | None) -> bool:
    if prefs.email_freq == "now":
        return True
    if local_now.isoweekday() not in prefs.days or local_now.strftime("%H:%M") < prefs.time:
        return False
    if last_sent is None:
        return True
    gap = timedelta(days=6) if prefs.email_freq == "weekly" else timedelta(hours=20)
    return (now_utc - last_sent) >= gap


def _line(item: dict) -> str:
    left = item["days_left"]
    tail = f" — осталось {left} дн." if left is not None and left > 0 else ""
    return f"• {item['title']} ({C.IMPACT_NAMES[item['impact']].lower()} уровень влияния){tail}"


def render_message(items: list[dict], base_url: str, urgent: bool) -> tuple[str, str]:
    head = "Срочное изменение для вашей компании" if urgent else "Дайджест LawTracker"
    body = [f"{head}: {len(items)} {'изменение' if len(items) == 1 else 'изменений'}", ""]
    body += [_line(i) for i in items]
    body += ["", f"Открыть кабинет: {base_url}/#/app"]
    return head, "\n".join(body)


def candidates(db: Session, user: User, company: Company, prefs: NotifSettings, settings: Settings, today: date,
               already: set[str]) -> list[dict]:
    v = build_ctx(db, user, company, settings.enforce_plan_limits, today)
    out = []
    for ch in db.scalars(select(Change).where(Change.status == "published", Change.is_draft.is_(False))).all():
        if ch.id in already or ch.id in v.read:
            continue
        personal, _ = evaluate(ch, v)
        if not personal:
            continue
        if prefs.topics and ch.topic not in prefs.topics:
            continue
        if C.IMPACT_RANK[ch.impact] < C.IMPACT_RANK[prefs.min_impact]:
            continue
        out.append(serialize(ch, v))
    return sorted(out, key=lambda s: (-C.IMPACT_RANK[s["impact"]], s["effective_at"] or "9999"))


def dispatch(db: Session, settings: Settings, mailer: Mailer, tg: Sender, now: datetime | None = None) -> dict:
    """Один проход рассылки: для каждого пользователя отправляет то, что пора. Возвращает счётчики."""
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    now_utc = now.astimezone(timezone.utc).replace(tzinfo=None)
    today = local_now.date()
    stats = {"email": 0, "telegram": 0, "skipped_paused": 0}
    for user, member in db.execute(select(User, Member).join(Member, Member.user_id == User.id)
                                   .where(Member.status == "active")).all():
        company = db.get(Company, member.company_id)
        if not company or not company.steps_saved:
            continue
        prefs = load_prefs(db, user.id)
        if paused(prefs, today):
            stats["skipped_paused"] += 1
            continue
        ent = C.entitlements(company.plan, settings.enforce_plan_limits)
        link = db.get(TelegramLink, user.id)
        channels: list[str] = []
        if prefs.email and user.email_verified:
            channels.append("email")
        if prefs.tg and ent["telegram"] and link and effective_status(link) == "connected" and link.chat_id:
            channels.append("telegram")
        for channel in channels:
            logs = db.scalars(select(NotificationLog).where(NotificationLog.user_id == user.id,
                                                            NotificationLog.channel == channel)).all()
            already = {cid for log in logs for cid in log.change_ids}
            last_digest = max((l.sent_at for l in logs if l.kind == "digest"), default=None)
            cands = candidates(db, user, company, prefs, settings, today, already)
            if not cands:
                continue
            urgent = [c for c in cands if c["impact"] == "high"]
            batch, kind = (cands, "digest") if _due(prefs, local_now, now_utc, last_digest) else (urgent, "urgent")
            if not batch:
                continue
            subject, text = render_message(batch, settings.base_url, kind == "urgent")
            ok = False
            if channel == "email":
                to = [(company.profile or {}).get("contact_email") or user.email]
                if member.role == "owner":
                    to += [r for r in (company.profile or {}).get("recipients", []) if r not in to]
                ok = all([mailer.send(addr, subject, text) for addr in to])
            else:
                ok = tg.send(link.chat_id, text)
            if ok:
                db.add(NotificationLog(user_id=user.id, channel=channel, kind=kind, change_ids=[c["id"] for c in batch],
                                       sent_at=now_utc))
                db.commit()
                stats["email" if channel == "email" else "telegram"] += 1
    return stats
