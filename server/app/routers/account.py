from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import (Auth, Ctx, current_auth, current_ctx, get_db, get_settings, record_attempt,
                    throttle)
from ..errors import ApiError
from ..models import (ActionState, AuthSession, ChangeState, ConsentRecord, Feedback, Member, NotificationPrefs,
                      SupportTicket, TelegramLink, User, utcnow)
from ..security import (hash_password, hash_token, normalize_email, numeric_code, password_problem, valid_email,
                        verify_password)
from .auth import user_public

router = APIRouter(prefix="/api/account", tags=["account"])


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=200)
    phone: str | None = Field(None, max_length=40)


@router.patch("")
def patch_account(body: Patch, auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    if body.name is not None:
        auth.user.name = " ".join(body.name.split())
    if body.phone is not None:
        auth.user.phone = body.phone.strip()
    db.commit()
    return {"user": user_public(auth.user)}


class Password(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str
    new: str


@router.post("/password")
def change_password(body: Password, auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    if not verify_password(body.current, auth.user.password_hash):
        raise ApiError(400, "invalid_password", "Текущий пароль указан неверно", {"current": "Неверный пароль"})
    if (problem := password_problem(body.new)):
        raise ApiError(422, "validation_error", "Проверьте введённые данные", {"new": problem})
    auth.user.password_hash = hash_password(body.new)
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == auth.user.id, AuthSession.id != auth.session.id)):
        db.delete(s)  # остальные сессии завершаются
    db.commit()
    return {"ok": True}


@router.get("/sessions")
def sessions(auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == auth.user.id).order_by(AuthSession.last_seen.desc()))
    return {"sessions": [{"id": s.id, "user_agent": s.user_agent, "ip": s.ip, "created_at": s.created_at.isoformat(),
                          "last_seen": s.last_seen.isoformat(), "current": s.id == auth.session.id} for s in rows]}


@router.delete("/sessions/{session_id}")
def end_session(session_id: int, auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    s = db.get(AuthSession, session_id)
    if s is None or s.user_id != auth.user.id or s.id == auth.session.id:
        raise ApiError(404, "not_found", "Сессия не найдена")
    db.delete(s)
    db.commit()
    return {"ok": True}


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str


@router.delete("")
def delete_account(body: Confirm, response: Response, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    if not verify_password(body.password, ctx.user.password_hash):
        raise ApiError(400, "invalid_password", "Неверный пароль", {"password": "Неверный пароль"})
    if ctx.member and ctx.member.role == "owner":
        others = db.scalar(select(func.count()).select_from(Member).where(
            Member.company_id == ctx.company.id, Member.id != ctx.member.id)) or 0
        if others:
            raise ApiError(409, "transfer_required", "Сначала удалите участников или передайте компанию")
        db.delete(ctx.company)  # каскадом удаляются участники и состояния действий
    elif ctx.member:
        db.delete(ctx.member)
    db.flush()
    db.expire(ctx.user)
    db.delete(ctx.user)
    db.commit()
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/export")
def export_data(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """«Выгрузить мои данные»: всё, что сервис хранит о пользователе и его компании, одним JSON-файлом."""
    u = ctx.user
    prefs = db.get(NotificationPrefs, u.id)
    tg = db.get(TelegramLink, u.id)
    out = {
        "exported_at": utcnow().isoformat(),
        "user": {"id": u.id, "name": u.name, "email": u.email, "phone": u.phone,
                 "email_verified": u.email_verified, "created_at": u.created_at.isoformat(),
                 "consent_at": u.consent_at.isoformat() if u.consent_at else None,
                 "consent_version": u.consent_version},
        "consents": [{"doc": c.doc, "version": c.version, "accepted_at": c.accepted_at.isoformat()}
                     for c in db.scalars(select(ConsentRecord).where(ConsentRecord.user_id == u.id))],
        "notification_settings": prefs.data if prefs else {},
        "telegram": {"status": tg.status, "username": tg.username} if tg else None,
        "change_marks": [{"change_id": s.change_id, "read": s.read, "saved": s.saved}
                         for s in db.scalars(select(ChangeState).where(ChangeState.user_id == u.id))],
        "feedback": [{"change_id": f.change_id, "rating": f.rating, "useful": f.useful,
                      "not_relevant": f.not_relevant, "comment": f.comment}
                     for f in db.scalars(select(Feedback).where(Feedback.user_id == u.id))],
        "support_tickets": [{"topic": t.topic, "message": t.message, "status": t.status,
                             "created_at": t.created_at.isoformat()}
                            for t in db.scalars(select(SupportTicket).where(SupportTicket.user_id == u.id))],
        "company": None,
    }
    if ctx.company:
        out["company"] = {"id": ctx.company.id, "plan": ctx.company.plan, "profile": ctx.company.profile or {},
                          "role": ctx.role,
                          "action_states": [{"change_id": a.change_id, "index": a.idx, "state": a.state}
                                            for a in db.scalars(select(ActionState).where(
                                                ActionState.company_id == ctx.company.id))]}
    return Response(content=__import__("json").dumps(out, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="lawtracker-export.json"'})


class EmailChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_email: str
    password: str


@router.post("/email")
def request_email_change(body: EmailChange, request: Request, auth: Auth = Depends(current_auth),
                         db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Шаг 1: код подтверждения уходит на НОВЫЙ адрес; пока он не введён, действует старый."""
    user = auth.user
    if not verify_password(body.password, user.password_hash):
        raise ApiError(400, "invalid_password", "Неверный пароль", {"password": "Неверный пароль"})
    email = normalize_email(body.new_email)
    if not valid_email(email):
        raise ApiError(422, "validation_error", "Некорректный e-mail", {"new_email": "Некорректный e-mail"})
    throttle(db, f"user:{user.id}", "email_change", 5, 60)
    record_attempt(db, f"user:{user.id}", "email_change")
    if email == user.email or db.scalar(select(User).where(User.email == email)):
        raise ApiError(409, "email_taken", "Этот e-mail уже используется", {"new_email": "Уже используется"})
    code = numeric_code()
    user.pending_email, user.pending_email_hash = email, hash_token(code)
    user.pending_email_expires = utcnow() + timedelta(minutes=15)
    user.verify_attempts = 0
    db.commit()
    request.app.state.mailer.send(email, "Подтверждение нового e-mail LawTracker",
                                  f"Код подтверждения нового адреса: {code}\nОн действует 15 минут.")
    extra = {}
    from ..mailer import ConsoleMailer
    if settings.demo_mode and isinstance(request.app.state.mailer, ConsoleMailer):
        extra = {"dev_code": code}
    return {"ok": True, **extra}


class EmailConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=6)


@router.post("/email/confirm")
def confirm_email_change(body: EmailConfirm, auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    user = auth.user
    if (not user.pending_email or not user.pending_email_expires or user.pending_email_expires < utcnow()
            or user.verify_attempts >= 5):
        raise ApiError(400, "code_expired", "Код устарел. Запросите новый", {"code": "Код устарел"})
    user.verify_attempts += 1
    if hash_token(body.code) != user.pending_email_hash:
        db.commit()
        raise ApiError(400, "code_invalid", "Неверный код", {"code": "Неверный код"})
    if db.scalar(select(User).where(User.email == user.pending_email, User.id != user.id)):
        raise ApiError(409, "email_taken", "Этот e-mail уже используется")
    user.email, user.email_verified = user.pending_email, True
    user.pending_email = user.pending_email_hash = user.pending_email_expires = None
    db.commit()
    return {"user": user_public(user)}
