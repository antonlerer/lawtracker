from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import Auth, Ctx, current_auth, current_ctx, get_db, get_settings
from ..errors import ApiError
from ..models import AuthSession, Member
from ..security import hash_password, password_problem, verify_password
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
