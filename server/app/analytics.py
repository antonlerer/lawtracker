"""События активности и отчёты для проверки гипотез беты."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Event, Feedback, User, utcnow

# Действия, которые считаются «активностью» (регистрация и шаги профиля сами по себе активностью не считаются)
ACTIVE_KINDS = {"feed_open", "change_open", "action", "feedback", "saved"}
KINDS = {"signup", "profile_step", "feed_open", "change_open", "action", "feedback", "saved", "email_click"}


def track(db: Session, user_id: int | None, company_id: int | None, kind: str, ref: str = "",
          dedupe_minutes: int = 0) -> None:
    """Записать событие. dedupe_minutes>0: не писать повтор того же события пользователя за этот период."""
    if dedupe_minutes and user_id is not None:
        since = utcnow() - timedelta(minutes=dedupe_minutes)
        if db.scalar(select(Event.id).where(Event.user_id == user_id, Event.kind == kind, Event.ref == ref,
                                            Event.created_at >= since).limit(1)):
            return
    db.add(Event(user_id=user_id, company_id=company_id, kind=kind, ref=ref[:80]))
    db.commit()


def week_start(d: datetime) -> datetime:
    day = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def weekly_report(db: Session, weeks: int = 8, now: datetime | None = None) -> dict:
    now = now or utcnow()
    first = week_start(now) - timedelta(weeks=weeks - 1)
    events = db.scalars(select(Event).where(Event.created_at >= first)).all()
    fbs = db.scalars(select(Feedback).where(Feedback.updated_at >= first)).all()
    users = db.scalars(select(User.created_at)).all()
    rows = []
    for i in range(weeks):
        start = first + timedelta(weeks=i)
        end = start + timedelta(weeks=1)
        ev = [e for e in events if start <= e.created_at < end]
        active = [e for e in ev if e.kind in ACTIVE_KINDS]
        registered_total = sum(1 for c in users if c < end)
        openers = {e.user_id for e in ev if e.kind == "feed_open" and e.user_id}
        fb = [f for f in fbs if start <= f.updated_at < end]
        rated = [f.rating for f in fb if f.rating]
        rows.append({
            "week": start.date().isoformat(),
            "signups": sum(1 for e in ev if e.kind == "signup"),
            "registered_total": registered_total,
            "profile_steps": sum(1 for e in ev if e.kind == "profile_step"),
            "active_users": len({e.user_id for e in active if e.user_id}),
            "active_companies": len({e.company_id for e in active if e.company_id}),
            "feed_openers": len(openers),
            "feed_open_share": round(len(openers) / registered_total, 3) if registered_total else 0,
            "change_opens": sum(1 for e in ev if e.kind == "change_open"),
            "actions_marked": sum(1 for e in ev if e.kind == "action"),
            "email_clicks": sum(1 for e in ev if e.kind == "email_click"),
            "feedback_count": len(fb),
            "avg_rating": round(sum(rated) / len(rated), 2) if rated else None,
            "not_relevant_share": round(sum(1 for f in fb if f.not_relevant) / len(fb), 3) if fb else None,
        })
    return {"weeks": rows}


def feedback_report(db: Session) -> dict:
    fbs = db.scalars(select(Feedback)).all()
    rated = [f.rating for f in fbs if f.rating]
    per: dict[str, list[Feedback]] = defaultdict(list)
    for f in fbs:
        per[f.change_id].append(f)
    items = []
    for cid, lst in per.items():
        r = [f.rating for f in lst if f.rating]
        items.append({"change_id": cid, "votes": len(lst),
                      "avg_rating": round(sum(r) / len(r), 2) if r else None,
                      "useful": sum(1 for f in lst if f.useful),
                      "not_relevant": sum(1 for f in lst if f.not_relevant)})
    items.sort(key=lambda x: (-x["not_relevant"], x["avg_rating"] if x["avg_rating"] is not None else 9))
    return {"total": len(fbs), "avg_rating": round(sum(rated) / len(rated), 2) if rated else None,
            "not_relevant_share": round(sum(1 for f in fbs if f.not_relevant) / len(fbs), 3) if fbs else None,
            "useful_share": round(sum(1 for f in fbs if f.useful) / len(fbs), 3) if fbs else None,
            "targets": {"avg_rating_min": 4.0, "not_relevant_share_max": 0.2}, "changes": items}
