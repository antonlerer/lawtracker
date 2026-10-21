from __future__ import annotations

import hmac
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import catalog as C
from .. import rules
from ..config import Settings
from ..deps import Ctx, client_ip, current_ctx, get_db, get_settings, need, record_attempt, throttle
from ..errors import ApiError
from ..models import Change, Company, PlanRequest, SupportTicket, Waitlist
from ..security import normalize_email, valid_email

router = APIRouter(prefix="/api", tags=["misc"])

# Матрица тарифов из демо (для страницы «Тарифы»).
MATRIX = [
    ("Мониторинг", None),
    ("Профиль компании", [True, True, True, True]),
    ("Подбор изменений по профилю", ["Общий дайджест", True, True, True]),
    ("Объяснение «почему это показано вам»", [False, True, True, True]),
    ("Сферы мониторинга", ["1", "3", "10", "Без ограничений"]),
    ("Уведомления", None),
    ("Кабинет и почта", ["Раз в месяц", True, True, True]),
    ("Уведомления в Telegram", [False, True, True, True]),
    ("Настройка частоты, категорий и порога значимости", [False, True, True, True]),
    ("Работа с изменениями", None),
    ("Сохранённое и отметки «прочитано»", [True, True, True, True]),
    ("Рекомендации к действию, сроки и ответственные", [False, False, True, True]),
    ("Архив изменений", [False, False, True, True]),
    ("Команда и компании", None),
    ("Пользователи", ["1", "1", "до 5", "Без ограничений"]),
    ("Роли и доступы", [False, False, True, True]),
]


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@router.get("/meta")
def meta(request: Request, settings: Settings = Depends(get_settings)):
    """Справочники для форм: профиль, фильтры, роли. Публичный метод (по нему сайт определяет, что сервер доступен)."""
    clock = getattr(request.app.state, "today", None)
    return {"today": (clock() if clock else date.today()).isoformat(), "demo_mode": settings.demo_mode,
            "enforce_plan_limits": settings.enforce_plan_limits,"topics": C.TOPICS, "industries": C.INDUSTRIES, "activities": C.ACTIVITIES, "regions": C.REGIONS,
            "licenses": C.LICENSES, "digital": C.DIGITAL, "forms": C.FORMS, "sizes": C.SIZE_NAMES,
            "headcounts": C.HEADCOUNTS, "roles": C.ROLES, "steps": C.STEPS,
            "impact": C.IMPACT_NAMES}


@router.get("/plans")
def plans():
    return {"plans": [C.PLANS[p] for p in C.PLAN_ORDER], "matrix": MATRIX, "prices_final": False}


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: Literal["Вопрос по работе сервиса", "Ошибка в материале", "Профиль и персонализация",
                   "Telegram и уведомления", "Другое"] = "Вопрос по работе сервиса"
    message: str = Field(max_length=5000)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Введите сообщение")
        return v


@router.post("/support", status_code=201)
def create_ticket(body: Ticket, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    throttle(db, f"user:{ctx.user.id}", "support", 10, 60)
    record_attempt(db, f"user:{ctx.user.id}", "support")
    t = SupportTicket(user_id=ctx.user.id, email=ctx.user.email, topic=body.topic, message=body.message.strip())
    db.add(t)
    db.commit()
    return {"id": t.id, "status": t.status}


@router.get("/support")
def my_tickets(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    rows = db.scalars(select(SupportTicket).where(SupportTicket.user_id == ctx.user.id)
                      .order_by(SupportTicket.id.desc()).limit(20)).all()
    return {"tickets": [{"id": t.id, "topic": t.topic, "message": t.message, "status": t.status,
                         "created_at": t.created_at.isoformat()} for t in rows]}


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    email: str
    message: str = Field(max_length=5000)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Введите сообщение")
        return v


@router.post("/contact", status_code=201)
def contact_form(body: Contact, request: Request, db: Session = Depends(get_db)):
    """Форма на странице «Контакты» (без входа)."""
    email = normalize_email(body.email)
    if not valid_email(email):
        raise ApiError(422, "validation_error", "Некорректный e-mail", {"email": "Некорректный e-mail"})
    throttle(db, client_ip(request), "contact", 5, 60)
    record_attempt(db, client_ip(request), "contact")
    db.add(SupportTicket(user_id=None, email=email, topic="Форма контактов", message=f"{body.name}: {body.message}"))
    db.commit()
    return {"ok": True}


@router.post("/plan/waitlist", status_code=201)
def waitlist(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """«Сообщить мне о запуске» для раздела «Персональный план»."""
    if not db.scalar(select(Waitlist).where(Waitlist.user_id == ctx.user.id, Waitlist.feature == "personal_plan")):
        db.add(Waitlist(user_id=ctx.user.id, feature="personal_plan"))
        db.commit()
    return {"ok": True}


class DemoPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["free", "basic", "pro", "corp"]


@router.put("/demo/plan")
def demo_plan(body: DemoPlan, ctx: Ctx = Depends(need("owner")), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings)):
    """Только DEMO_MODE=true: владелец переключает тариф для показа (оплаты нет). Лимиты тарифа проверяются."""
    if not settings.demo_mode:
        raise ApiError(404, "not_found", "Не найдено")
    ent = C.entitlements(body.plan)
    from sqlalchemy import func

    from ..models import Member
    n = db.scalar(select(func.count()).select_from(Member).where(Member.company_id == ctx.company.id)) or 0
    topics = len((ctx.company.profile or {}).get("topics", []))
    if (ent["users"] is not None and n > ent["users"]) or (ent["areas"] is not None and topics > ent["areas"]):
        raise ApiError(409, "limits_exceeded", "Участников или сфер больше, чем разрешает тариф")
    ctx.company.plan = body.plan
    db.commit()
    return {"plan": ctx.company.plan}


# ---------- служебные методы (заголовок X-Admin-Token) ----------

def admin_only(x_admin_token: str = Header(""), settings: Settings = Depends(get_settings)) -> None:
    if not settings.admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise ApiError(404, "not_found", "Не найдено")  # без подсказок, что метод существует


class ChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str
    impact: Literal["high", "med", "low"]
    published_at: date
    effective_at: date | None = None
    is_draft: bool = False
    status: Literal["published", "hidden"] = "published"
    agency: str = ""
    doc: str = ""
    source_url: str = ""
    confidence: Literal["high", "mid", "low"] = "low"
    reviewed: bool = False
    title: str = Field(min_length=1)
    summary: str = ""
    what: str = ""
    who: str = ""
    comment: str = ""
    quote: str = ""
    requires: list[dict] = []
    reasons: list[dict] = []
    regions: list[str] = []
    actions: list[dict] = []
    related: list[str] = []


def check_change(data: ChangeIn) -> None:
    fields: dict[str, str] = {}
    if data.topic not in C.TOPICS:
        fields["topic"] = "Неизвестная тематика"
    try:
        for r in data.requires:
            rules.validate_rule(r)
        for i, r in enumerate(data.reasons):
            if set(r) != {"when", "text"} or not isinstance(r["text"], str) or not r["text"].strip():
                raise rules.RuleError(f"reasons[{i}]: нужны поля when и text")
            rules.validate_rule(r["when"])
    except rules.RuleError as exc:
        fields["rules"] = str(exc)
    for i, a in enumerate(data.actions):
        if not isinstance(a.get("title"), str) or not a["title"].strip():
            fields[f"actions[{i}]"] = "Нужно название действия"
        elif a.get("due"):
            try:
                date.fromisoformat(a["due"])
            except ValueError:
                fields[f"actions[{i}]"] = "Срок в формате ГГГГ-ММ-ДД"
    if set(data.regions) - set(C.REGIONS):
        fields["regions"] = "Неизвестный регион"
    if fields:
        raise ApiError(422, "validation_error", "Проверьте материал", fields)


@router.put("/admin/changes/{change_id}", dependencies=[Depends(admin_only)])
def admin_upsert_change(change_id: str, body: ChangeIn, db: Session = Depends(get_db)):
    if not change_id.replace("-", "").replace("_", "").isalnum() or len(change_id) > 80:
        raise ApiError(422, "validation_error", "Некорректный идентификатор")
    check_change(body)
    ch = db.get(Change, change_id)
    if ch is None:
        ch = Change(id=change_id, **body.model_dump())
        db.add(ch)
    else:
        for k, v in body.model_dump().items():
            setattr(ch, k, v)
    db.commit()
    return {"id": ch.id, "status": ch.status}


class AnalyzeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=80, max_length=400000)
    apply: bool = False


@router.post("/admin/changes/{change_id}/analyze", dependencies=[Depends(admin_only)])
def admin_analyze(change_id: str, body: AnalyzeIn, request: Request, db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)):
    """ИИ-разбор текста акта для существующего изменения. apply=false — только показать результат (черновик для юриста)."""
    from fastapi.encoders import jsonable_encoder

    from ingest.analyze import Analyzer
    from ingest.pravo import RawAct
    llm = request.app.state.llm
    if llm is None:
        raise ApiError(503, "llm_not_configured", "Модель не настроена (GIGACHAT_AUTH_KEY или LLM_BASE_URL + LLM_MODEL)")
    ch = db.get(Change, change_id)
    if ch is None:
        raise ApiError(404, "not_found", "Материал не найден")
    raw = RawAct(ch.source_key or ch.id, ch.doc, ch.published_at.isoformat(), "", ch.title, ch.source_url)
    clock = getattr(request.app.state, "today", None)
    res = Analyzer(llm, settings.analysis_passes).analyze(raw, body.text, clock() if clock else None)
    applied = False
    if body.apply and res.fields:
        keep = {"source_key", "related", "status", "agency"}
        for k, v in res.fields.items():
            if k not in keep:
                setattr(ch, k, v)
        ch.reviewed = False
        db.commit()
        applied = True
    return jsonable_encoder({"relevant": res.relevant, "applied": applied, "notes": res.notes, "report": res.report,
                             "fields": res.fields})


class PlanSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["free", "basic", "pro", "corp"]


@router.put("/admin/companies/{company_id}/plan", dependencies=[Depends(admin_only)])
def admin_set_plan(company_id: int, body: PlanSet, db: Session = Depends(get_db)):
    c = db.get(Company, company_id)
    if c is None:
        raise ApiError(404, "not_found", "Компания не найдена")
    ent = C.entitlements(body.plan)
    from sqlalchemy import func
    from ..models import Member
    n = db.scalar(select(func.count()).select_from(Member).where(Member.company_id == c.id)) or 0
    topics = len((c.profile or {}).get("topics", []))
    if (ent["users"] is not None and n > ent["users"]) or (ent["areas"] is not None and topics > ent["areas"]):
        raise ApiError(409, "limits_exceeded", "Участников или сфер больше, чем разрешает тариф")
    c.plan = body.plan
    for r in db.scalars(select(PlanRequest).where(PlanRequest.company_id == c.id, PlanRequest.plan == body.plan,
                                                  PlanRequest.status == "new")):
        r.status = "done"
    db.commit()
    return {"id": c.id, "plan": c.plan}


@router.get("/admin/support", dependencies=[Depends(admin_only)])
def admin_tickets(db: Session = Depends(get_db)):
    rows = db.scalars(select(SupportTicket).order_by(SupportTicket.id.desc()).limit(100)).all()
    return {"tickets": [{"id": t.id, "email": t.email, "topic": t.topic, "message": t.message, "status": t.status}
                        for t in rows]}
