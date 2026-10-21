"""Профиль компании: типизированные поля вместо тегов.

Каждое поле имеет конкретный тип и допустимые значения из справочников. Клиент заполняет профиль
по шагам (см. catalog.STEPS); применимость изменений считается по этим полям (см. rules.py).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import catalog as C
from .security import inn_valid, valid_email

Size = Literal["micro", "small", "medium", "large"]


def _subset(values: list[str], allowed: list[str], label: str) -> list[str]:
    seen: list[str] = []
    for v in values:
        if v not in allowed:
            raise ValueError(f"{label}: недопустимое значение «{v}»")
        if v not in seen:
            seen.append(v)
    return seen


class CompanyProfile(BaseModel):
    """Все поля профиля с значениями по умолчанию («не заполнено»)."""
    model_config = ConfigDict(extra="forbid")

    # basics
    name: str = Field("", max_length=200)
    inn: str = ""
    form: str = ""
    size: Size | Literal[""] = ""
    # industry
    industry: str = ""
    activities: list[str] = []
    desc: str = Field("", max_length=1000)
    topics: list[str] = []
    # regions
    regions: list[str] = []
    # structure
    branches: Literal["", "yes", "no"] = ""
    foreign_share: bool = False
    entities: int = Field(1, ge=1, le=500)
    # staff
    headcount: str = ""
    remote: bool = False
    foreign_staff: bool = False
    # licenses
    licenses: list[str] = []
    # pd
    pd_customers: bool = False
    pd_employees: bool = False
    pd_external: bool = False
    pd_cross: bool = False
    # ved
    ved_export: bool = False
    ved_import: bool = False
    # digital
    digital: list[str] = []
    # contacts
    contact_name: str = Field("", max_length=200)
    contact_email: str = ""
    contact_role: str = Field("", max_length=200)
    recipients: list[str] = []

    @field_validator("name", "desc", "contact_name", "contact_role")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split()) if v else v

    @field_validator("inn")
    @classmethod
    def _inn(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not inn_valid(v):
            raise ValueError("ИНН указан неверно (10 или 12 цифр, проверяется контрольная сумма)")
        return v

    @field_validator("form")
    @classmethod
    def _form(cls, v: str) -> str:
        if v and v not in C.FORMS:
            raise ValueError("Недопустимая форма")
        return v

    @field_validator("industry")
    @classmethod
    def _industry(cls, v: str) -> str:
        if v and v not in C.INDUSTRIES:
            raise ValueError("Недопустимая отрасль")
        return v

    @field_validator("headcount")
    @classmethod
    def _headcount(cls, v: str) -> str:
        if v and v not in C.HEADCOUNTS:
            raise ValueError("Недопустимая численность")
        return v

    @field_validator("activities")
    @classmethod
    def _activities(cls, v):
        return _subset(v, C.ACTIVITIES, "Направления")

    @field_validator("regions")
    @classmethod
    def _regions(cls, v):
        return _subset(v, C.REGIONS, "Регионы")

    @field_validator("licenses")
    @classmethod
    def _licenses(cls, v):
        return _subset(v, C.LICENSES, "Лицензии")

    @field_validator("digital")
    @classmethod
    def _digital(cls, v):
        return _subset(v, C.DIGITAL, "Сервисы")

    @field_validator("topics")
    @classmethod
    def _topics(cls, v):
        return _subset(v, list(C.TOPICS), "Тематики")

    @field_validator("contact_email")
    @classmethod
    def _cemail(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v and not valid_email(v):
            raise ValueError("Некорректный e-mail")
        return v

    @field_validator("recipients")
    @classmethod
    def _recipients(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v:
            e = item.strip().lower()
            if not valid_email(e):
                raise ValueError(f"Некорректный e-mail получателя: {item}")
            if e not in out:
                out.append(e)
        if len(out) > 20:
            raise ValueError("Не больше 20 получателей")
        return out


# Обязательные поля шагов (что без них шаг считать заполненным нельзя).
REQUIRED = {
    "basics": ["name", "form", "size"],
    "industry": ["industry"],
    "regions": ["regions"],
    "staff": ["headcount"],
    "contacts": ["contact_name", "contact_email"],
}


def validate_step(step_id: str, data: dict) -> dict:
    """Проверяет данные шага и возвращает нормализованные значения только полей этого шага.

    Бросает ValueError со словарём {поле: сообщение}.
    """
    from pydantic import ValidationError

    try:
        # Проверяем полной моделью (все валидаторы), затем берём поля шага.
        full = CompanyProfile.model_validate({k: v for k, v in data.items()})
    except ValidationError as exc:
        errors = {}
        for e in exc.errors():
            loc = str(e["loc"][0]) if e["loc"] else "_"
            msg = e["msg"].removeprefix("Value error, ")
            errors[loc] = msg
        raise StepError(errors) from None
    allowed = set(C.STEP_BY_ID[step_id]["fields"])
    unknown = set(data) - allowed
    if unknown:
        raise StepError({k: "Поле не относится к этому шагу" for k in unknown})
    missing = {f: "Обязательное поле" for f in REQUIRED.get(step_id, []) if not _filled(getattr(full, f))}
    if missing:
        raise StepError(missing)
    dumped = full.model_dump()
    # Не затираем поля шага значениями по умолчанию, если клиент их не прислал.
    return {k: dumped[k] for k in allowed if k in data or k in REQUIRED.get(step_id, [])}


def _filled(v) -> bool:
    return bool(v) if not isinstance(v, bool) else True


class StepError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__(errors)
        self.errors = errors


def completeness(saved: dict, skipped: dict) -> dict:
    done = [s["id"] for s in C.STEPS if saved.get(s["id"]) or (s["opt"] and skipped.get(s["id"]))]
    next_step = next((s["id"] for s in C.STEPS if s["id"] not in done), None)
    return {
        "percent": round(len(done) / len(C.STEPS) * 100),
        "done": done,
        "next_step": next_step,
        "started": bool(saved),
        "steps": [{"id": s["id"], "title": s["title"], "optional": s["opt"], "affects": s["affects"],
                   "state": "saved" if saved.get(s["id"]) else "skipped" if skipped.get(s["id"]) else "todo"}
                  for s in C.STEPS],
    }
