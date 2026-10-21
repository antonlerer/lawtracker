"""Изменения законодательства: персонализация по профилю, ленты, карточки, действия."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog as C
from . import rules
from .errors import ApiError
from .models import ActionState, Change, ChangeState, Company, User

PER_PAGE_DEFAULT = 5


@dataclass
class ViewCtx:
    user: User
    company: Company | None
    profile: dict
    started: bool
    plan: str
    ent: dict
    today: date
    read: set[str] = field(default_factory=set)
    saved: set[str] = field(default_factory=set)
    actions: dict[tuple[str, int], str] = field(default_factory=dict)


def build_ctx(db: Session, user: User, company: Company | None, enforce: bool, today: date) -> ViewCtx:
    plan = company.plan if company else "free"
    states = db.scalars(select(ChangeState).where(ChangeState.user_id == user.id)).all()
    acts: dict[tuple[str, int], str] = {}
    if company:
        for a in db.scalars(select(ActionState).where(ActionState.company_id == company.id)):
            acts[(a.change_id, a.idx)] = a.state
    return ViewCtx(
        user=user, company=company, profile=dict(company.profile or {}) if company else {},
        started=bool(company and company.steps_saved), plan=plan, ent=C.entitlements(plan, enforce), today=today,
        read={s.change_id for s in states if s.read}, saved={s.change_id for s in states if s.saved}, actions=acts)


def evaluate(ch: Change, v: ViewCtx) -> tuple[bool, list[str]]:
    """Касается ли изменение компании и почему. Пустой профиль или бесплатный тариф — не персонализируем."""
    if not v.started or not v.ent["personal"]:
        return False, []
    prof_regions = v.profile.get("regions") or []
    if ch.regions and prof_regions and "Вся Россия" not in prof_regions and not set(ch.regions) & set(prof_regions):
        return False, []
    reasons = rules.match(ch.requires or [], ch.reasons or [], v.profile)
    return bool(reasons), reasons


def days_left(ch: Change, v: ViewCtx) -> int | None:
    if ch.is_draft or ch.effective_at is None:
        return None
    return (ch.effective_at - v.today).days


def open_actions(ch: Change, v: ViewCtx) -> int:
    return sum(1 for i in range(len(ch.actions or [])) if v.actions.get((ch.id, i), "todo") != "done")


def needs_attention(ch: Change, v: ViewCtx, personal: bool) -> bool:
    if not personal or ch.is_draft:
        return False
    dl = days_left(ch, v)
    urgent = ch.impact == "high" or (dl is not None and 0 <= dl <= 30)
    return urgent and open_actions(ch, v) > 0


def _status(ch: Change, dl: int | None) -> str:
    if ch.is_draft:
        return "draft"
    return "in_force" if dl is not None and dl <= 0 else "upcoming"


def serialize(ch: Change, v: ViewCtx, detail: bool = False, related: dict[str, Change] | None = None) -> dict:
    personal, reasons = evaluate(ch, v)
    dl = days_left(ch, v)
    out = {
        "id": ch.id, "title": ch.title, "summary": ch.summary, "topic": ch.topic, "topic_label": C.TOPICS.get(ch.topic, ch.topic),
        "impact": ch.impact, "agency": ch.agency, "published_at": ch.published_at.isoformat(),
        "effective_at": ch.effective_at.isoformat() if ch.effective_at else None, "days_left": dl,
        "is_draft": ch.is_draft, "status": _status(ch, dl), "confidence": ch.confidence, "reviewed": ch.reviewed,
        "is_read": ch.id in v.read, "is_saved": ch.id in v.saved, "personal": personal,
        "reasons": reasons if v.ent["reasons"] else [], "attention": needs_attention(ch, v, personal),
        "open_actions": open_actions(ch, v) if v.ent["actions"] else None,
    }
    if not detail:
        return out
    tl = [{"key": "published", "date": ch.published_at.isoformat(), "done": True},
          {"key": "today", "date": v.today.isoformat(), "done": True}]
    if not ch.is_draft and ch.effective_at:
        tl.append({"key": "effective", "date": ch.effective_at.isoformat(), "done": ch.effective_at <= v.today})
    actions = []
    if v.ent["actions"]:
        for i, a in enumerate(ch.actions or []):
            due = date.fromisoformat(a["due"]) if a.get("due") else None
            actions.append({"index": i, "title": a["title"], "responsible": a.get("responsible", ""),
                            "due": a.get("due"), "due_in_days": (due - v.today).days if due else None,
                            "state": v.actions.get((ch.id, i), "todo")})
    basis = []
    if v.started:
        if v.profile.get("industry"):
            basis.append("Отрасль: " + v.profile["industry"])
        if v.profile.get("headcount"):
            basis.append("Численность: " + v.profile["headcount"])
    out.update({
        "what": ch.what, "who": ch.who, "comment": ch.comment, "quote": ch.quote, "doc": ch.doc,
        "source_url": ch.source_url, "timeline": tl, "actions": actions, "actions_locked": not v.ent["actions"],
        "profile_basis": basis,
        "related": [{"id": r.id, "title": r.title, "topic": r.topic, "impact": r.impact}
                    for r in (related or {}).values()],
    })
    return out


def visible_query(v: ViewCtx):
    q = select(Change).where(Change.status == "published")
    if v.ent["archive_days"]:
        q = q.where(Change.published_at >= v.today - timedelta(days=v.ent["archive_days"]))
    return q


def get_change(db: Session, v: ViewCtx, change_id: str) -> Change:
    ch = db.scalar(visible_query(v).where(Change.id == change_id))
    if ch is None:
        raise ApiError(404, "not_found", "Материал не найден")
    return ch


def list_changes(db: Session, v: ViewCtx, *, q: str = "", period: str = "all", topic: str = "", impact: str = "",
                 status: str = "all", sort: str = "date", personal: bool = False, page: int = 1,
                 per_page: int = PER_PAGE_DEFAULT, detail: bool = False) -> dict:
    query = visible_query(v)
    if topic:
        query = query.where(Change.topic == topic)
    if impact:
        query = query.where(Change.impact == impact)
    if period != "all":
        query = query.where(Change.published_at >= v.today - timedelta(days=int(period)))
    needle = q.strip().casefold()   # поиск в Python: LOWER()/LIKE в SQLite не понимают кириллицу
    items = []
    rows = db.scalars(query).all()
    related_map: dict[str, Change] = {}
    if detail:
        wanted = {r for ch in rows for r in (ch.related or [])}
        if wanted:
            related_map = {c.id: c for c in db.scalars(select(Change).where(Change.id.in_(wanted),
                                                                          Change.status == "published"))}
    for ch in rows:
        if needle:
            hay = " ".join([ch.title, ch.summary, ch.what, ch.agency, C.TOPICS.get(ch.topic, "")]).casefold()
            if needle not in hay:
                continue
        s = serialize(ch, v, detail=detail, related={r: related_map[r] for r in (ch.related or []) if r in related_map})
        if personal and not s["personal"]:
            continue
        if status == "new" and s["is_read"]:
            continue
        if status == "read" and not s["is_read"]:
            continue
        if status == "saved" and not s["is_saved"]:
            continue
        if status == "attn" and not s["attention"]:
            continue
        items.append(s)
    keys = {
        "date": lambda s: (s["published_at"],),
        "impact": lambda s: (C.IMPACT_RANK[s["impact"]], s["published_at"]),
    }
    if sort == "eff":
        items.sort(key=lambda s: (s["effective_at"] is None, s["effective_at"] or ""))
    else:
        items.sort(key=keys[sort], reverse=True)
    total = len(items)
    pages = max(1, -(-total // per_page))
    page = min(max(page, 1), pages)
    return {"items": items[(page - 1) * per_page: page * per_page], "total": total, "page": page, "pages": pages,
            "per_page": per_page}


def overview(db: Session, v: ViewCtx, tg_status: str) -> dict:
    from .profile import completeness
    comp = completeness(v.company.steps_saved, v.company.steps_skipped) if v.company else completeness({}, {})
    changes = [serialize(c, v) for c in db.scalars(visible_query(v)).all()]
    attn = sorted([c for c in changes if c["attention"]], key=lambda c: (-C.IMPACT_RANK[c["impact"]],
                                                                       c["effective_at"] or "9999"))
    personal = sorted([c for c in changes if c["personal"] and not c["is_draft"] and c["impact"] != "low"],
                      key=lambda c: (-C.IMPACT_RANK[c["impact"]], c["effective_at"] or "9999"))
    top = personal[0] if personal else next((c for c in changes if c["impact"] == "high"), None)
    att_list = attn[:3] or ([top] if top else [])
    unread = [c for c in changes if not c["is_read"] and c not in att_list]
    fresh = sorted(sorted(unread, key=lambda c: c["published_at"], reverse=True),
                   key=lambda c: -C.IMPACT_RANK[c["impact"]])[:4]
    upcoming = sorted([c for c in changes if not c["is_draft"] and c["days_left"] is not None and c["days_left"] > 0],
                      key=lambda c: c["days_left"])[:3]
    return {
        "stats": {"unread": sum(1 for c in changes if not c["is_read"]), "attention": len(attn),
                  "soon30": sum(1 for c in changes if c["days_left"] is not None and 0 < c["days_left"] <= 30)},
        "attention": att_list, "fresh": fresh, "upcoming": upcoming,
        "profile": {"percent": comp["percent"], "next_step": comp["next_step"], "started": comp["started"]},
        "telegram": tg_status, "personalization_locked": not v.ent["personal"],
    }


def set_flag(db: Session, v: ViewCtx, change_id: str, **flags: bool) -> ChangeState:
    get_change(db, v, change_id)
    st = db.scalar(select(ChangeState).where(ChangeState.user_id == v.user.id, ChangeState.change_id == change_id))
    if st is None:
        st = ChangeState(user_id=v.user.id, change_id=change_id)
        db.add(st)
    for k, val in flags.items():
        setattr(st, k, val)
    db.commit()
    return st


def set_action(db: Session, v: ViewCtx, change_id: str, idx: int, state: str, user_id: int) -> None:
    ch = get_change(db, v, change_id)
    if not v.ent["actions"]:
        raise ApiError(402, "plan_required", "Рекомендации к действию доступны на тарифе «Профессиональный»")
    if v.company is None:
        raise ApiError(409, "company_required", "Сначала создайте компанию")
    if not 0 <= idx < len(ch.actions or []):
        raise ApiError(404, "not_found", "Такого действия нет")
    from .models import utcnow
    row = db.scalar(select(ActionState).where(ActionState.company_id == v.company.id, ActionState.change_id == change_id,
                                              ActionState.idx == idx))
    if row is None:
        row = ActionState(company_id=v.company.id, change_id=change_id, idx=idx)
        db.add(row)
    row.state, row.updated_by, row.updated_at = state, user_id, utcnow()
    db.commit()
