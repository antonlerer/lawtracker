from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Change

from .analyze import Analyzer
from .enrich import enrich
from .llm import LLM, make_llm
from .pravo import fetch_document, fetch_rss, parse_rss
from .text import fetch_text


log = logging.getLogger("lawtracker.ingest")


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
        try:
            outcome = _process(db, http, analyzer, text_fetcher, eo)
        except Exception:  # сбой одного акта (сеть, модель, БД) не останавливает проход
            db.rollback()
            log.exception("ingest: акт %s не обработан", eo)
            stats["errors"] += 1
            continue
        for k in outcome:
            stats[k] += 1
    db.commit()
    log.info("ingest: %s", stats)
    return stats


def _process(db: Session, http, analyzer, text_fetcher, eo: str) -> list[str]:
    raw = fetch_document(http, eo)
    if raw is None:
        return ["errors"]
    done: list[str] = []
    fields = None
    if analyzer:
        try:
            result = analyzer.analyze(raw, text_fetcher(http, eo))
        except Exception:  # модель недоступна или ответила мусором — продолжаем без неё
            log.exception("ingest: разбор акта %s не удался, используются правила", eo)
            result = None
        if result is not None:
            if result.relevant is False:
                return ["skipped"]
            if result.fields:
                fields = result.fields
                done.append("analyzed")
    if fields is None:
        fields = enrich(raw, None)
        if fields is None:
            return ["skipped"]
        done.append("fallback")
    try:
        with db.begin_nested():
            db.add(Change(id=f"pravo-{eo}", **fields))
    except IntegrityError:   # параллельный проход уже добавил этот акт
        return ["skipped"]
    db.commit()
    return done + ["added"]

