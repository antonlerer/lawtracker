"""Загрузка демо-изменений (те же материалы, что в демоверсии сайта). Даты считаются от сегодняшнего дня."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Change
from app.rules import validate_rule

SEED = Path(__file__).with_name("changes.json")


def load_demo_changes(db: Session, today: date | None = None) -> int:
    today = today or date.today()
    items = json.loads(SEED.read_text(encoding="utf-8"))
    for it in items:
        for r in it["requires"]:
            validate_rule(r)
        for r in it["reasons"]:
            validate_rule(r["when"])
        eff = it.get("effective_offset")
        data = {
            "topic": it["topic"], "impact": it["impact"], "published_at": today + timedelta(days=it["published_offset"]),
            "effective_at": None if eff is None else today + timedelta(days=eff), "is_draft": it["is_draft"],
            "agency": it["agency"], "doc": it["doc"], "confidence": it["confidence"], "reviewed": it["reviewed"],
            "title": it["title"], "summary": it["summary"], "what": it["what"], "who": it["who"],
            "comment": it["comment"], "quote": it["quote"], "requires": it["requires"], "reasons": it["reasons"],
            "regions": it["regions"], "related": it["related"], "status": "published",
            "actions": [{"title": a["title"], "responsible": a["responsible"],
                         "due": (today + timedelta(days=a["due_offset"])).isoformat()} for a in it["actions"]],
        }
        ch = db.get(Change, it["id"])
        if ch is None:
            db.add(Change(id=it["id"], **data))
        else:
            for k, v in data.items():
                setattr(ch, k, v)
    db.commit()
    return len(items)


DEMO_EMAIL, DEMO_PASSWORD = "demo@example.org", "DemoPassw0rd1"


def create_demo_account(db: Session) -> str:
    """Демо-пользователь с компанией «Нордвуд Логистика» на тарифе «Профессиональный» (только для показа)."""
    from sqlalchemy import select

    from app.models import Company, Member, User
    from app.security import hash_password

    if db.scalar(select(User).where(User.email == DEMO_EMAIL)):
        return f"Демо-пользователь уже есть: {DEMO_EMAIL} / {DEMO_PASSWORD}"
    profile = {
        "name": "Нордвуд Логистика", "inn": "7707083893", "form": "ООО", "size": "small",
        "industry": "Транспорт и логистика", "activities": ["Грузоперевозки", "Складская логистика"], "desc": "",
        "topics": ["pd", "tax", "labor", "log"], "regions": ["Санкт-Петербург и ЛО", "Северо-Запад"],
        "branches": "yes", "foreign_share": True, "entities": 2, "headcount": "16–100", "remote": True,
        "foreign_staff": False, "licenses": [], "pd_customers": True, "pd_employees": True, "pd_external": True,
        "pd_cross": False, "ved_export": False, "ved_import": False,
        "digital": ["CRM", "Облачные хранилища", "E-mail и SMS-рассылки"], "contact_name": "Анна Ковалёва",
        "contact_email": DEMO_EMAIL, "contact_role": "Руководитель юридического отдела", "recipients": []}
    user = User(email=DEMO_EMAIL, name="Анна Ковалёва", password_hash=hash_password(DEMO_PASSWORD), email_verified=True)
    company = Company(plan="pro", profile=profile,
                      steps_saved={s: True for s in ("basics", "industry", "regions", "structure", "staff", "pd")})
    db.add_all([user, company])
    db.flush()
    db.add(Member(company_id=company.id, user_id=user.id, email=user.email, role="owner", status="active"))
    db.commit()
    return f"Демо-пользователь создан: {DEMO_EMAIL} / {DEMO_PASSWORD}"
