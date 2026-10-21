from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import catalog as C
from .config import Settings
from .errors import ApiError
from .models import AuthSession, Company, LoginAttempt, Member, User, utcnow
from .security import hash_token


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@dataclass
class Auth:
    user: User
    session: AuthSession


def current_auth(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Auth:
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise ApiError(401, "unauthorized", "Требуется вход")
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    now = utcnow()
    if sess is None or sess.expires_at < now:
        raise ApiError(401, "unauthorized", "Сессия истекла, войдите снова")
    if (now - sess.last_seen) > timedelta(minutes=1):
        sess.last_seen = now
        db.commit()
    user = db.get(User, sess.user_id)
    if user is None:
        raise ApiError(401, "unauthorized", "Требуется вход")
    return Auth(user, sess)


@dataclass
class Ctx:
    user: User
    session: AuthSession
    member: Member | None
    company: Company | None

    @property
    def role(self) -> str | None:
        return self.member.role if self.member else None

    @property
    def plan(self) -> str:
        return self.company.plan if self.company else "free"


def current_ctx(auth: Auth = Depends(current_auth), db: Session = Depends(get_db)) -> Ctx:
    member = db.scalar(select(Member).where(Member.user_id == auth.user.id, Member.status == "active"))
    company = db.get(Company, member.company_id) if member else None
    return Ctx(auth.user, auth.session, member, company)


def need(min_role: str):
    """Зависимость: пользователь состоит в компании и роль не ниже указанной."""
    def dep(ctx: Ctx = Depends(current_ctx)) -> Ctx:
        if ctx.member is None or ctx.company is None:
            raise ApiError(409, "company_required", "Сначала создайте компанию или присоединитесь к ней")
        if C.ROLE_RANK[ctx.member.role] < C.ROLE_RANK[min_role]:
            raise ApiError(403, "forbidden", "Недостаточно прав для этого действия")
        return ctx
    return dep


def throttle(db: Session, key: str, kind: str, limit: int, minutes: int) -> None:
    since = utcnow() - timedelta(minutes=minutes)
    n = db.scalar(select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.key == key, LoginAttempt.kind == kind, LoginAttempt.created_at >= since))
    if (n or 0) >= limit:
        raise ApiError(429, "too_many_requests", "Слишком много попыток. Попробуйте позже")


def record_attempt(db: Session, key: str, kind: str) -> None:
    db.add(LoginAttempt(key=key, kind=kind))
    db.commit()


def clear_attempts(db: Session, key: str, kind: str) -> None:
    for a in db.scalars(select(LoginAttempt).where(LoginAttempt.key == key, LoginAttempt.kind == kind)):
        db.delete(a)
    db.commit()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""
