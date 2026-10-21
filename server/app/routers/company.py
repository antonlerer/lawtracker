from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import catalog as C
from ..config import Settings
from ..deps import Ctx, current_ctx, get_db, get_settings, need
from ..errors import ApiError
from ..models import Company, Member, PlanRequest, User, utcnow
from ..profile import CompanyProfile, StepError, completeness, validate_step
from ..security import invite_code, normalize_email, valid_email

router = APIRouter(prefix="/api", tags=["company"])


def company_payload(ctx: Ctx, settings: Settings) -> dict:
    c = ctx.company
    prof = {**CompanyProfile().model_dump(), **(c.profile or {})}
    return {"id": c.id, "plan": c.plan, "role": ctx.role, "profile": prof,
            "completeness": completeness(c.steps_saved or {}, c.steps_skipped or {}),
            "entitlements": C.entitlements(c.plan, settings.enforce_plan_limits)}


def _members_count(db: Session, company_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Member).where(Member.company_id == company_id)) or 0


class CreateCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")
    form: str
    name: str = Field(min_length=1, max_length=200)


@router.post("/company", status_code=201)
def create_company(body: CreateCompany, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    if ctx.member is not None:
        raise ApiError(409, "already_in_company", "Вы уже состоите в компании")
    if not ctx.user.email_verified:
        raise ApiError(403, "email_not_verified", "Подтвердите e-mail")
    data = validate_fields({"form": body.form, "name": body.name})
    company = Company(plan="free", profile=data, steps_saved={}, steps_skipped={})
    db.add(company)
    db.flush()
    db.add(Member(company_id=company.id, user_id=ctx.user.id, email=ctx.user.email, role="owner", status="active"))
    db.commit()
    ctx = Ctx(ctx.user, ctx.session, db.scalar(select(Member).where(Member.user_id == ctx.user.id)), company)
    return company_payload(ctx, settings)


def validate_fields(data: dict) -> dict:
    try:
        return {k: v for k, v in CompanyProfile.model_validate(data).model_dump().items() if k in data}
    except Exception as exc:  # pydantic.ValidationError
        errors = getattr(exc, "errors", lambda: [])()
        raise ApiError(422, "validation_error", "Проверьте введённые данные",
                       {str(e["loc"][0]): e["msg"].removeprefix("Value error, ") for e in errors}) from None


class Join(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=4, max_length=20)


@router.post("/company/join")
def join_company(body: Join, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)):
    if ctx.member is not None:
        raise ApiError(409, "already_in_company", "Вы уже состоите в компании")
    if not ctx.user.email_verified:
        raise ApiError(403, "email_not_verified", "Подтвердите e-mail")
    inv = db.scalar(select(Member).where(Member.invite_code == body.code.strip().upper(), Member.status == "pending"))
    if inv is None or (inv.invite_expires and inv.invite_expires < utcnow()):
        raise ApiError(404, "invite_invalid", "Код приглашения не найден или устарел", {"code": "Неверный код"})
    if inv.email != ctx.user.email:
        raise ApiError(403, "invite_wrong_email", "Приглашение выдано на другой e-mail", {"code": "Другой e-mail"})
    inv.user_id, inv.status, inv.invite_code, inv.invite_expires = ctx.user.id, "active", None, None
    db.commit()
    company = db.get(Company, inv.company_id)
    return company_payload(Ctx(ctx.user, ctx.session, inv, company), settings)


@router.get("/company")
def get_company(ctx: Ctx = Depends(need("viewer")), settings: Settings = Depends(get_settings)):
    return company_payload(ctx, settings)


class StepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict


@router.put("/company/profile/{step}")
def save_step(step: str, body: StepBody, ctx: Ctx = Depends(need("admin")), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings)):
    if step not in C.STEP_BY_ID:
        raise ApiError(404, "not_found", "Такого шага нет")
    try:
        values = validate_step(step, body.data)
    except StepError as exc:
        raise ApiError(422, "validation_error", "Проверьте введённые данные", exc.errors) from None
    ent = C.entitlements(ctx.plan, settings.enforce_plan_limits)
    if "topics" in values and ent["areas"] is not None and len(values["topics"]) > ent["areas"]:
        raise ApiError(402, "plan_limit", f"На вашем тарифе можно выбрать сфер мониторинга: {ent['areas']}",
                       {"topics": f"Не больше {ent['areas']}"})
    c = ctx.company
    c.profile = {**(c.profile or {}), **values}
    c.steps_saved = {**(c.steps_saved or {}), step: True}
    skipped = dict(c.steps_skipped or {})
    skipped.pop(step, None)
    c.steps_skipped = skipped
    db.commit()
    return company_payload(ctx, settings)


@router.post("/company/profile/{step}/skip")
def skip_step(step: str, ctx: Ctx = Depends(need("admin")), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings)):
    meta = C.STEP_BY_ID.get(step)
    if meta is None:
        raise ApiError(404, "not_found", "Такого шага нет")
    if not meta["opt"]:
        raise ApiError(422, "step_required", "Этот шаг обязателен")
    c = ctx.company
    c.steps_skipped = {**(c.steps_skipped or {}), step: True}
    db.commit()
    return company_payload(ctx, settings)


@router.delete("/company/profile")
def reset_profile(ctx: Ctx = Depends(need("admin")), db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)):
    c = ctx.company
    keep = {k: v for k, v in (c.profile or {}).items() if k in ("name", "form")}
    c.profile, c.steps_saved, c.steps_skipped = keep, {}, {}
    db.commit()
    return company_payload(ctx, settings)


# ---------- команда ----------

def member_payload(m: Member, me: int, db: Session) -> dict:
    user = db.get(User, m.user_id) if m.user_id else None
    return {"id": m.id, "name": user.name if user else "", "email": user.email if user else m.email, "role": m.role,
            "status": m.status, "is_me": m.user_id == me}


@router.get("/team")
def team(ctx: Ctx = Depends(need("viewer")), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    members = db.scalars(select(Member).where(Member.company_id == ctx.company.id).order_by(Member.id)).all()
    ent = C.entitlements(ctx.plan, settings.enforce_plan_limits)
    return {"members": [member_payload(m, ctx.user.id, db) for m in members], "limit": ent["users"],
            "roles": [{"id": k, "name": v, "description": C.ROLE_DESC[k]} for k, v in C.ROLES.items()]}


class Invite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    role: Literal["admin", "member", "viewer"] = "member"


@router.post("/team/invites", status_code=201)
def invite(body: Invite, request: Request, ctx: Ctx = Depends(need("admin")), db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings)):
    email = normalize_email(body.email)
    if not valid_email(email):
        raise ApiError(422, "validation_error", "Некорректный e-mail", {"email": "Некорректный e-mail"})
    if body.role == "admin" and ctx.role != "owner":
        raise ApiError(403, "forbidden", "Назначать администраторов может только владелец")
    ent = C.entitlements(ctx.plan, settings.enforce_plan_limits)
    if ent["users"] is not None and _members_count(db, ctx.company.id) >= ent["users"]:
        raise ApiError(402, "plan_limit", f"На вашем тарифе можно пригласить участников: {ent['users']}")
    if db.scalar(select(Member).where(Member.company_id == ctx.company.id, Member.email == email)):
        raise ApiError(409, "already_invited", "Этот участник уже в команде или приглашён", {"email": "Уже приглашён"})
    other = db.scalar(select(Member).join(User, User.id == Member.user_id).where(User.email == email))
    if other:
        raise ApiError(409, "in_other_company", "Этот пользователь уже состоит в другой компании")
    code = invite_code()
    m = Member(company_id=ctx.company.id, email=email, role=body.role, status="pending", invite_code=code,
               invite_expires=utcnow() + timedelta(days=7))
    db.add(m)
    db.commit()
    _mail_invite(request, ctx, email, code, settings)
    return member_payload(m, ctx.user.id, db)


def _mail_invite(request: Request, ctx: Ctx, email: str, code: str, settings: Settings) -> None:
    name = (ctx.company.profile or {}).get("name") or "компании"
    request.app.state.mailer.send(
        email, "Приглашение в LawTracker",
        f"{ctx.user.name} приглашает вас в «{name}».\nКод приглашения: {code} (действует 7 дней).\n"
        f"Зарегистрируйтесь с этим e-mail и введите код: {settings.base_url}")


def _target(db: Session, ctx: Ctx, member_id: int) -> Member:
    m = db.get(Member, member_id)
    if m is None or m.company_id != ctx.company.id:  # чужую компанию не раскрываем
        raise ApiError(404, "not_found", "Участник не найден")
    if m.role == "owner":
        raise ApiError(403, "forbidden", "Владельца изменить нельзя")
    if m.role == "admin" and ctx.role != "owner":
        raise ApiError(403, "forbidden", "Администраторами управляет владелец")
    return m


@router.post("/team/{member_id}/resend")
def resend(member_id: int, request: Request, ctx: Ctx = Depends(need("admin")), db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings)):
    m = _target(db, ctx, member_id)
    if m.status != "pending":
        raise ApiError(409, "not_pending", "Приглашение уже принято")
    m.invite_code, m.invite_expires = invite_code(), utcnow() + timedelta(days=7)
    db.commit()
    _mail_invite(request, ctx, m.email, m.invite_code, settings)
    return {"ok": True}


class RoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["admin", "member", "viewer"]


@router.patch("/team/{member_id}")
def change_role(member_id: int, body: RoleBody, ctx: Ctx = Depends(need("admin")), db: Session = Depends(get_db)):
    m = _target(db, ctx, member_id)
    if body.role == "admin" and ctx.role != "owner":
        raise ApiError(403, "forbidden", "Назначать администраторов может только владелец")
    m.role = body.role
    db.commit()
    return member_payload(m, ctx.user.id, db)


@router.delete("/team/{member_id}")
def remove_member(member_id: int, ctx: Ctx = Depends(need("admin")), db: Session = Depends(get_db)):
    m = _target(db, ctx, member_id)
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("/team/leave")
def leave(ctx: Ctx = Depends(need("viewer")), db: Session = Depends(get_db)):
    if ctx.role == "owner":
        raise ApiError(409, "owner_cannot_leave", "Владелец не может выйти из компании")
    db.delete(ctx.member)
    db.commit()
    return {"ok": True}


# ---------- тариф ----------

@router.get("/billing")
def billing(ctx: Ctx = Depends(need("viewer")), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    plan = C.PLANS[ctx.plan]
    ent = C.entitlements(ctx.plan, settings.enforce_plan_limits)
    topics = (ctx.company.profile or {}).get("topics", [])
    reqs = db.scalars(select(PlanRequest).where(PlanRequest.company_id == ctx.company.id)
                      .order_by(PlanRequest.id.desc()).limit(5)).all()
    return {"plan": plan, "entitlements": ent, "payments_connected": False,
            "usage": {"members": _members_count(db, ctx.company.id), "areas": len(topics)},
            "requests": [{"plan": r.plan, "period": r.period, "status": r.status, "created_at": r.created_at.isoformat()}
                         for r in reqs]}


class PlanReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["basic", "pro", "corp"]
    period: Literal["m", "y"] = "m"


@router.post("/billing/request", status_code=201)
def plan_request(body: PlanReq, ctx: Ctx = Depends(need("owner")), db: Session = Depends(get_db)):
    """Оплата пока не подключена: фиксируем заявку на смену тарифа, менеджер связывается с владельцем."""
    if body.plan == ctx.plan:
        raise ApiError(409, "same_plan", "Этот тариф уже подключён")
    db.add(PlanRequest(company_id=ctx.company.id, plan=body.plan, period=body.period, requested_by=ctx.user.id))
    db.commit()
    return {"status": "new", "plan": body.plan, "message": "Заявка принята, мы свяжемся с вами"}
