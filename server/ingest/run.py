from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Change

from .analyze import Analyzer
from .enrich import enrich
from .llm import LLM, make_llm
from .pravo import fetch_document, fetch_rss, parse_rss
from .text import fetch_text


def run_ingest(db: Session, settings: Settings, *, http: httpx.Client | None = None, llm: LLM | None = None,
               limit: int = 200, text_fetcher=fetch_text) -> dict:
    """Один проход: RSS → новые акты → текст → ИИ-разбор (если настроена модель) → запись в БД.

    Если модели нет, текст не получен или разбор не удался, запись создаётся по правилам (confidence=low).
    """
    http = http or httpx.Client(headers={"User-Agent": "LawTracker/1.0"})
    llm = llm or make_llm(settings)
    analyzer = Analyzer(llm, settings.analysis_passes) if llm else None
    stats = {"seen": 0, "new": 0, "added": 0, "skipped": 0, "errors": 0, "analyzed": 0, "fallback": 0}
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
        fields = None
        if analyzer:
            result = analyzer.analyze(raw, text_fetcher(http, eo))
            if result.relevant is False:
                stats["skipped"] += 1
                continue
            if result.fields:
                fields = result.fields
                stats["analyzed"] += 1
        if fields is None:
            fields = enrich(raw, None)
            if fields is None:
                stats["skipped"] += 1
                continue
            stats["fallback"] += 1
        db.add(Change(id=f"pravo-{eo}", **fields))
        stats["added"] += 1
    db.commit()
    return stats
