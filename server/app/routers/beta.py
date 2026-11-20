"""Функции беты: обратная связь по карточке, заявки компаний, согласия."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics import track
from ..config import Settings
from ..deps import Ctx, client_ip, current_ctx, get_db, get_settings, record_attempt, throttle
from ..errors import ApiError
from ..models import BetaApplication, Change, Feedback, utcnow
from ..security import normalize_email, valid_email

router = APIRouter(prefix="/api", tags=["beta"])


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: int | None = Field(None, ge=1, le=5)
    useful: bool | None = None
    not_relevant: bool | None = None
    comment: str | None = Field(None, max_length=1000)

    @field_validator("comment")
    @classmethod
    def _strip(cls, v):
        return v.strip() if v else v


def fb_public(f: Feedback | None) -> dict:
    return {"rating": f.rating if f else None, "useful": f.useful if f else None,
            "not_relevant": bool(f.not_relevant) if f else False, "comment": f.comment if f else ""}


@router.get("/changes/{change_id}/feedback")
def get_feedback(change_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    f = db.scalar(select(Feedback).where(Feedback.user_id == ctx.user.id, Feedback.change_id == change_id))
    return fb_public(f)


@router.put("/changes/{change_id}/feedback")
def put_feedback(change_id: str, body: FeedbackIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """Оценка полезности (1–5), «полезно» и «не про нас». Повторный вызов обновляет ответ."""
    sent = body.model_dump(exclude_unset=True)
    if not sent:
        raise ApiError(422, "validation_error", "Укажите оценку или отметку", {"rating": "Нужна оценка или отметка"})
    ch = db.get(Change, change_id)
    if ch is None or ch.status != "published":
        raise ApiError(404, "not_found", "Материал не найден")
    f = db.scalar(select(Feedback).where(Feedback.user_id == ctx.user.id, Feedback.change_id == change_id))
    if f is None:
        f = Feedback(user_id=ctx.user.id, company_id=ctx.company.id if ctx.company else None, change_id=change_id,
                     not_relevant=False)
        db.add(f)
    if "rating" in sent:
        f.rating = body.rating
    if "useful" in sent:
        f.useful = body.useful
        if body.useful:
            f.not_relevant = False
    if "not_relevant" in sent:
        f.not_relevant = bool(body.not_relevant)
        if body.not_relevant:
            f.useful = False
    if "comment" in sent:
        f.comment = body.comment or ""
    f.updated_at = utcnow()
    db.commit()
    track(db, ctx.user.id, f.company_id, "feedback", change_id)
    return fb_public(f)


class Application(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=200)
    person: str = Field("", max_length=200)
    role: str = Field("", max_length=100)
    size: str = Field("", max_length=40)
    industry: str = Field("", max_length=100)
    regions: str = Field("", max_length=300)
    email: str
    phone: str = Field("", max_length=40)
    consent: bool


@router.post("/beta/apply", status_code=201)
def beta_apply(body: Application, request: Request, db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings)):
    """Заявка на участие в закрытой бете (без входа). Заявки смотрит команда в админ-странице /admin."""
    email = normalize_email(body.email)
    fields = {}
    if not valid_email(email):
        fields["email"] = "Некорректный e-mail"
    if not body.consent:
        fields["consent"] = "Нужно согласие на обработку персональных данных"
    if fields:
        raise ApiError(422, "validation_error", "Проверьте введённые данные", fields)
    throttle(db, client_ip(request), "beta_apply", 5, 60)
    record_attempt(db, client_ip(request), "beta_apply")
    d = body.model_dump(exclude={"consent", "email"})
    db.add(BetaApplication(email=email, consent_version=settings.terms_version,
                           **{k: " ".join(v.split()) for k, v in d.items()}))
    db.commit()
    return {"ok": True}


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["change_open"]
    ref: str = Field(max_length=80)


@router.post("/events", status_code=202)
def client_event(body: EventIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """События, которые видит только браузер (открытие карточки: сайт получает материалы списком, а не по одному)."""
    ch = db.get(Change, body.ref)
    if ch is None or ch.status != "published":
        raise ApiError(404, "not_found", "Материал не найден")
    track(db, ctx.user.id, ctx.company.id if ctx.company else None, body.kind, body.ref, dedupe_minutes=30)
    return {"ok": True}
