"""Служебные методы беты: очередь проверки юристом, приглашения, заявки, метрики, отчёт по обратной связи.

Все методы требуют заголовок X-Admin-Token; без заданного ADMIN_TOKEN отвечают 404.
"""
from __future__ import annotations

import csv
import io
import secrets
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics import feedback_report, weekly_report
from ..config import Settings
from ..deps import get_db, get_settings
from ..errors import ApiError
from ..models import BetaApplication, BetaInvite, Change, Feedback
from ..security import normalize_email, valid_email
from .misc import ChangeIn, admin_only, check_change

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(admin_only)])
page_router = APIRouter()

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_invite_code() -> str:
    return "LT-" + "".join(secrets.choice(_ALPHABET) for _ in range(8))


def _row(ch: Change) -> dict:
    return {"id": ch.id, "title": ch.title, "topic": ch.topic, "impact": ch.impact, "status": ch.status,
            "reviewed": ch.reviewed, "confidence": ch.confidence, "published_at": ch.published_at.isoformat(),
            "doc": ch.doc, "source_url": ch.source_url, "created_at": ch.created_at.isoformat()}


# ---------- очередь проверки юристом ----------

@router.get("/changes")
def queue(reviewed: Literal["all", "yes", "no"] = "no", status: Literal["all", "published", "hidden"] = "all",
          limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    q = select(Change).order_by(Change.created_at.desc(), Change.id).limit(limit)
    if reviewed != "all":
        q = q.where(Change.reviewed == (reviewed == "yes"))
    if status != "all":
        q = q.where(Change.status == status)
    return {"changes": [_row(c) for c in db.scalars(q)]}


def _full(ch: Change) -> dict:
    d = ChangeIn.model_validate({k: getattr(ch, k) for k in ChangeIn.model_fields}).model_dump(mode="json")
    return {"id": ch.id, **d}


@router.get("/changes/{change_id}")
def one(change_id: str, db: Session = Depends(get_db)):
    ch = db.get(Change, change_id)
    if ch is None:
        raise ApiError(404, "not_found", "Материал не найден")
    return _full(ch)


class ChangePatch(BaseModel):
    """Частичная правка карточки юристом. Любое поле необязательно; правила проходят ту же проверку, что и при загрузке."""
    model_config = ConfigDict(extra="forbid")
    reviewed: bool | None = None
    status: Literal["published", "hidden"] | None = None
    confidence: Literal["high", "mid", "low"] | None = None
    topic: str | None = None
    impact: Literal["high", "med", "low"] | None = None
    effective_at: date | None = None
    title: str | None = Field(None, min_length=1)
    summary: str | None = None
    what: str | None = None
    who: str | None = None
    comment: str | None = None
    quote: str | None = None
    requires: list[dict] | None = None
    reasons: list[dict] | None = None
    regions: list[str] | None = None
    actions: list[dict] | None = None


@router.patch("/changes/{change_id}")
def patch_change(change_id: str, body: ChangePatch, db: Session = Depends(get_db)):
    ch = db.get(Change, change_id)
    if ch is None:
        raise ApiError(404, "not_found", "Материал не найден")
    patch = body.model_dump(exclude_unset=True)
    merged = {k: getattr(ch, k) for k in ChangeIn.model_fields} | patch
    check_change(ChangeIn.model_validate(merged))
    for k, v in patch.items():       # число действий лучше не менять после публикации: отметки привязаны к номеру
        setattr(ch, k, v)
    db.commit()
    return _full(ch)


# ---------- приглашения и заявки ----------

class InvitesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(1, ge=1, le=100)
    email: str = ""
    note: str = Field("", max_length=200)


@router.post("/invites", status_code=201)
def create_invites(body: InvitesIn, db: Session = Depends(get_db)):
    email = normalize_email(body.email) if body.email else ""
    if email and not valid_email(email):
        raise ApiError(422, "validation_error", "Некорректный e-mail", {"email": "Некорректный e-mail"})
    if email:
        body.count = 1
    codes = []
    for _ in range(body.count):
        inv = BetaInvite(code=new_invite_code(), email=email, note=body.note)
        db.add(inv)
        codes.append(inv.code)
    db.commit()
    return {"codes": codes}


@router.get("/invites")
def list_invites(db: Session = Depends(get_db)):
    rows = db.scalars(select(BetaInvite).order_by(BetaInvite.id.desc()).limit(500)).all()
    return {"invites": [{"code": i.code, "email": i.email, "note": i.note, "used": i.used_at is not None,
                         "used_at": i.used_at.isoformat() if i.used_at else None,
                         "created_at": i.created_at.isoformat()} for i in rows]}


@router.get("/applications")
def applications(db: Session = Depends(get_db)):
    rows = db.scalars(select(BetaApplication).order_by(BetaApplication.id.desc()).limit(500)).all()
    return {"applications": [{"id": a.id, "company": a.company, "person": a.person, "role": a.role, "size": a.size,
                              "industry": a.industry, "regions": a.regions, "email": a.email, "phone": a.phone,
                              "status": a.status, "created_at": a.created_at.isoformat()} for a in rows]}


class AppPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "invited", "declined"]
    send_invite: bool = False


@router.patch("/applications/{app_id}")
def patch_application(app_id: int, body: AppPatch, request: Request, db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)):
    """Одобрить заявку: создаётся код приглашения, привязанный к e-mail, и (если send_invite) уходит письмо."""
    a = db.get(BetaApplication, app_id)
    if a is None:
        raise ApiError(404, "not_found", "Заявка не найдена")
    a.status = body.status
    code = None
    if body.status == "invited":
        inv = db.scalar(select(BetaInvite).where(BetaInvite.email == a.email, BetaInvite.used_at.is_(None)))
        if inv is None:
            inv = BetaInvite(code=new_invite_code(), email=a.email, note=f"заявка {a.id}: {a.company}")
            db.add(inv)
        code = inv.code
        if body.send_invite:
            request.app.state.mailer.send(
                a.email, "Приглашение в закрытую бету LawTracker",
                f"Здравствуйте!\n\nВас приглашают в закрытую бету LawTracker.\n"
                f"Код приглашения: {code}\nРегистрация: {settings.base_url}/#/register\n\n"
                "Сервис даёт информационные материалы и не заменяет юридическую консультацию.")
    db.commit()
    return {"id": a.id, "status": a.status, "code": code}


# ---------- метрики и обратная связь ----------

@router.get("/metrics")
def metrics(weeks: int = Query(8, ge=1, le=52), db: Session = Depends(get_db)):
    return weekly_report(db, weeks)


@router.get("/metrics.csv", response_class=PlainTextResponse)
def metrics_csv(weeks: int = Query(8, ge=1, le=52), db: Session = Depends(get_db)):
    rows = weekly_report(db, weeks)["weeks"]
    return _csv(rows, list(rows[0]) if rows else [])


@router.get("/feedback")
def feedback(db: Session = Depends(get_db)):
    return feedback_report(db)


@router.get("/feedback.csv", response_class=PlainTextResponse)
def feedback_csv(db: Session = Depends(get_db)):
    """Выгрузка для аналитика: одна строка на ответ (без e-mail; пользователь — числовой номер)."""
    rows = [{"user_id": f.user_id, "company_id": f.company_id, "change_id": f.change_id, "rating": f.rating,
             "useful": f.useful, "not_relevant": f.not_relevant, "comment": f.comment,
             "updated_at": f.updated_at.isoformat()} for f in db.scalars(select(Feedback).order_by(Feedback.id))]
    return _csv(rows, ["user_id", "company_id", "change_id", "rating", "useful", "not_relevant", "comment",
                       "updated_at"])


def _csv(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        # защита от «формул» при открытии в Excel
        w.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in r.items()})
    return buf.getvalue()


# ---------- страница администратора ----------

@page_router.get("/admin", include_in_schema=False)
def admin_page():
    """Страница для команды и юриста. Сама по себе данных не содержит: все запросы идут с токеном администратора."""
    return FileResponse(Path(__file__).resolve().parents[1] / "static" / "admin.html",
                        media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})
