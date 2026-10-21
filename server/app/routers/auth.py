from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import (Auth, clear_attempts, client_ip, current_auth, get_db, get_settings, record_attempt, throttle)
from ..errors import ApiError
from ..models import AuthSession, User, utcnow
from ..security import (hash_password, hash_token, new_token, normalize_email, numeric_code, password_problem,
                        valid_email, verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])
_DUMMY_HASH = hash_password("dummy-password-1")  # выравнивает время ответа для несуществующих email


class Register(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    email: str
    password: str
    consent: bool


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str


def open_session(db: Session, request: Request, response: Response, user: User, settings: Settings) -> None:
    token = new_token()
    now = utcnow()
    db.add(AuthSession(user_id=user.id, token_hash=hash_token(token), created_at=now, last_seen=now,
                       expires_at=now + timedelta(days=settings.session_days),
                       user_agent=(request.headers.get("user-agent") or "")[:300], ip=client_ip(request)))
    db.commit()
    response.set_cookie(settings.cookie_name, token, max_age=settings.session_days * 86400, httponly=True,
                        samesite="lax", secure=settings.cookie_secure, path="/")


def user_public(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "phone": user.phone,
            "email_verified": user.email_verified}


def send_code(request: Request, db: Session, user: User) -> None:
    code = numeric_code()
    user.verify_code_hash, user.verify_attempts = hash_token(code), 0
    user.verify_expires = utcnow() + timedelta(minutes=15)
    db.commit()
    request.app.state.mailer.send(user.email, "Код подтверждения LawTracker",
                                  f"Ваш код подтверждения: {code}\nОн действует 15 минут.")


@router.post("/register", status_code=201)
def register(body: Register, request: Request, response: Response, db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)):
    email = normalize_email(body.email)
    fields = {}
    if not valid_email(email):
        fields["email"] = "Некорректный e-mail"
    if (problem := password_problem(body.password)):
        fields["password"] = problem
    if not body.consent:
        fields["consent"] = "Нужно согласие на обработку персональных данных"
    if fields:
        raise ApiError(422, "validation_error", "Проверьте введённые данные", fields)
    throttle(db, client_ip(request), "register", 20, 60)
    record_attempt(db, client_ip(request), "register")
    if db.scalar(select(User).where(User.email == email)):
        # Не раскрываем, что адрес занят, лишь для формы: это же стандартное поведение демо («уже зарегистрирован»).
        raise ApiError(409, "email_taken", "Этот e-mail уже зарегистрирован", {"email": "Уже зарегистрирован"})
    user = User(email=email, name=" ".join(body.name.split()), password_hash=hash_password(body.password),
                consent_at=utcnow())
    db.add(user)
    db.commit()
    send_code(request, db, user)
    open_session(db, request, response, user, settings)
    return {"user": user_public(user)}


@router.post("/login")
def login(body: Login, request: Request, response: Response, db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings)):
    email = normalize_email(body.email)
    key = f"{email}|{client_ip(request)}"
    throttle(db, key, "login", 8, 15)
    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH) and user is not None
    if not ok:
        record_attempt(db, key, "login")
        raise ApiError(401, "invalid_credentials", "Неверная почта или пароль")
    clear_attempts(db, key, "login")
    open_session(db, request, response, user, settings)
    return {"user": user_public(user)}


@router.post("/logout")
def logout(response: Response, auth: Auth = Depends(current_auth), db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings)):
    db.delete(auth.session)
    db.commit()
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    from ..models import Company, Member
    member = db.scalar(select(Member).where(Member.user_id == auth.user.id, Member.status == "active"))
    company = db.get(Company, member.company_id) if member else None
    return {"user": user_public(auth.user),
            "company": {"id": company.id, "name": (company.profile or {}).get("name", ""), "plan": company.plan,
                        "role": member.role} if company else None}


class Code(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=6)


@router.post("/verify-email")
def verify_email(body: Code, auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    user = auth.user
    if user.email_verified:
        return {"ok": True}
    if user.verify_attempts >= 5 or not user.verify_expires or user.verify_expires < utcnow():
        raise ApiError(400, "code_expired", "Код устарел. Запросите новый", {"code": "Код устарел"})
    user.verify_attempts += 1
    if not user.verify_code_hash or hash_token(body.code) != user.verify_code_hash:
        db.commit()
        raise ApiError(400, "code_invalid", "Неверный код", {"code": "Неверный код"})
    user.email_verified, user.verify_code_hash, user.verify_expires = True, None, None
    db.commit()
    return {"ok": True}


@router.post("/resend-code")
def resend_code(request: Request, auth: Auth = Depends(current_auth), db: Session = Depends(get_db)):
    if auth.user.email_verified:
        return {"ok": True}
    throttle(db, f"user:{auth.user.id}", "resend", 5, 60)
    record_attempt(db, f"user:{auth.user.id}", "resend")
    send_code(request, db, auth.user)
    return {"ok": True}


class Forgot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str


@router.post("/forgot")
def forgot(body: Forgot, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Всегда отвечает одинаково — не раскрывает, есть ли такой e-mail."""
    email = normalize_email(body.email)
    throttle(db, client_ip(request), "forgot", 10, 60)
    record_attempt(db, client_ip(request), "forgot")
    user = db.scalar(select(User).where(User.email == email))
    if user:
        token = new_token()
        user.reset_token_hash, user.reset_expires = hash_token(token), utcnow() + timedelta(hours=1)
        db.commit()
        request.app.state.mailer.send(
            user.email, "Сброс пароля LawTracker",
            f"Чтобы задать новый пароль, откройте ссылку (действует 1 час):\n{settings.base_url}/#/reset?token={token}")
    return {"ok": True}


class Reset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    password: str


@router.post("/reset")
def reset(body: Reset, db: Session = Depends(get_db)):
    if (problem := password_problem(body.password)):
        raise ApiError(422, "validation_error", "Проверьте введённые данные", {"password": problem})
    user = db.scalar(select(User).where(User.reset_token_hash == hash_token(body.token)))
    if not user or not user.reset_expires or user.reset_expires < utcnow():
        raise ApiError(400, "token_invalid", "Ссылка недействительна или устарела")
    user.password_hash = hash_password(body.password)
    user.reset_token_hash = user.reset_expires = None
    for s in list(user.sessions):  # после смены пароля все старые сессии завершаются
        db.delete(s)
    db.commit()
    return {"ok": True}
