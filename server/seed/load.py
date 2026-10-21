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
