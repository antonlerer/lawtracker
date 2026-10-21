from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Change

from .enrich import enrich
from .gigachat import GigaChat
from .pravo import fetch_document, fetch_rss, parse_rss


def run_ingest(db: Session, settings: Settings, *, http: httpx.Client | None = None, llm=None, limit: int = 200) -> dict:
    """Один проход: RSS → новые акты → разбор → запись в БД. Возвращает счётчики."""
    http = http or httpx.Client(headers={"User-Agent": "LawTracker/1.0"})
    if llm is None and settings.gigachat_auth_key:
        llm = GigaChat(settings.gigachat_auth_key, settings.gigachat_scope, settings.gigachat_model,
                       settings.gigachat_verify_ssl)
    stats = {"seen": 0, "new": 0, "added": 0, "skipped": 0, "errors": 0}
    eos = parse_rss(fetch_rss(http))[:limit]
    stats["seen"] = len(eos)
    known = set(db.scalars(select(Change.source_key).where(Change.source_key.in_(eos))).all())
    for eo in eos:
        if eo in known:
            continue
        stats["new"] += 1
        raw = fetch_document(http, eo)
        if raw is None:
            stats["errors"] += 1
            continue
        fields = enrich(raw, llm)
        if fields is None:
            stats["skipped"] += 1
            continue
        db.add(Change(id=f"pravo-{eo}", **fields))
        stats["added"] += 1
    db.commit()
    return stats
