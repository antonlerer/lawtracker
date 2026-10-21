"""Измерение качества ИИ-разбора на выборке, размеченной юристом.

Формат строки gold-файла (JSON Lines):
{"id": "a1", "title": "...", "type": "Федеральный закон", "number": "301-ФЗ", "published": "2026-09-10",
 "text": "полный текст или фрагмент акта",
 "expected": {"relevant": true, "topic": "pd", "impact": "high", "effective_date": "2026-12-09", "is_draft": false},
 "profiles": [{"profile": {"pd_customers": true}, "applies": true}, {"profile": {"industry": "Медицина"}, "applies": false}]}
Поля expected и profiles необязательны: считаются только те метрики, для которых есть разметка.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from app import rules

from .analyze import Analyzer
from .pravo import RawAct


def load_gold(path: str | Path) -> list[dict]:
    items = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("#"):
            try:
                items.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{n}: некорректный JSON ({exc})") from None
    return items


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def evaluate(items: list[dict], analyzer: Analyzer, today: date | None = None) -> dict:
    today = today or date.today()
    c = defaultdict(int)
    by_conf: dict[str, list[bool]] = defaultdict(list)
    rows = []
    tp = fp = fn = tn = 0
    for it in items:
        raw = RawAct(it.get("id", ""), it.get("type", ""), it.get("published", ""), it.get("number", ""), it["title"], "")
        res = analyzer.analyze(raw, it.get("text"), today)
        exp = it.get("expected", {})
        row = {"id": it.get("id"), "ok": res.relevant is not None, "notes": res.notes}
        c["n"] += 1
        if res.relevant is None:
            c["failed"] += 1
            rows.append(row)
            continue
        c["analyzed"] += 1
        if "relevant" in exp:
            c["rel_n"] += 1
            c["rel_ok"] += res.relevant == exp["relevant"]
        f = res.fields
        if f and exp.get("relevant", True):
            correct_all = True
            for key, got in (("topic", f["topic"]), ("impact", f["impact"]), ("is_draft", f["is_draft"])):
                if key in exp:
                    c[key + "_n"] += 1
                    ok = got == exp[key]
                    c[key + "_ok"] += ok
                    correct_all &= ok
            if "effective_date" in exp:
                c["date_n"] += 1
                got = f["effective_at"].isoformat() if f["effective_at"] else None
                ok = got == exp["effective_date"]
                c["date_ok"] += ok
                correct_all &= ok
                row["effective"] = {"got": got, "expected": exp["effective_date"]}
            by_conf[f["confidence"]].append(correct_all)
            rep = res.report
            c["quotes"] += rep.get("quotes_total", 0)
            c["quotes_ok"] += rep.get("quotes_verified", 0)
            c["dropped"] += rep.get("dropped_actions", 0) + rep.get("dropped_reasons", 0)
            c["unground"] += len(rep.get("ungrounded_numbers", []))
            for case in it.get("profiles", []):
                hit = bool(rules.match(f["requires"], f["reasons"], case["profile"]))
                if hit and case["applies"]:
                    tp += 1
                elif hit and not case["applies"]:
                    fp += 1
                elif not hit and case["applies"]:
                    fn += 1
                else:
                    tn += 1
        rows.append(row)
    return {
        "items": c["n"], "analysis_failed": c["failed"],
        "relevance_accuracy": _rate(c["rel_ok"], c["rel_n"]), "topic_accuracy": _rate(c["topic_ok"], c["topic_n"]),
        "impact_accuracy": _rate(c["impact_ok"], c["impact_n"]), "draft_accuracy": _rate(c["is_draft_ok"], c["is_draft_n"]),
        "effective_date_accuracy": _rate(c["date_ok"], c["date_n"]),
        "quotes_verified_rate": _rate(c["quotes_ok"], c["quotes"]),
        "dropped_unsupported_per_item": _rate(c["dropped"], c["analyzed"]),
        "ungrounded_numbers_total": c["unground"],
        "applicability": {"precision": _rate(tp, tp + fp), "recall": _rate(tp, tp + fn), "cases": tp + fp + fn + tn,
                          "false_positives": fp, "false_negatives": fn},
        "accuracy_by_confidence": {k: {"n": len(v), "all_correct": _rate(sum(v), len(v))} for k, v in sorted(by_conf.items())},
        "rows": rows,
    }


def format_report(rep: dict) -> str:
    def pct(v):
        return "—" if v is None else f"{v * 100:.0f}%"
    a = rep["applicability"]
    lines = [f"Актов: {rep['items']}, разбор не удался: {rep['analysis_failed']}",
             f"Релевантность: {pct(rep['relevance_accuracy'])}   Тематика: {pct(rep['topic_accuracy'])}   "
             f"Влияние: {pct(rep['impact_accuracy'])}   Проект/принят: {pct(rep['draft_accuracy'])}",
             f"Срок вступления в силу: {pct(rep['effective_date_accuracy'])}",
             f"Цитаты найдены в тексте: {pct(rep['quotes_verified_rate'])}   Отброшено необоснованного на акт: "
             f"{rep['dropped_unsupported_per_item']}   Выдуманных чисел: {rep['ungrounded_numbers_total']}",
             f"Применимость к профилям ({a['cases']}): точность {pct(a['precision'])}, полнота {pct(a['recall'])}, "
             f"ложных показов {a['false_positives']}, пропусков {a['false_negatives']}"]
    for k, v in rep["accuracy_by_confidence"].items():
        lines.append(f"Уверенность «{k}»: акты {v['n']}, всё верно в {pct(v['all_correct'])}")
    return "\n".join(lines)
