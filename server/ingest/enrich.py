"""Превращает сырой акт в запись Change с атрибутами для персонализации.

Источник данных — название и тип акта. Поэтому без модели заполняются только тематика, значимость
и грубые правила применимости; пояснения, «кого касается» и план действий НЕ придумываются.
Запись создаётся с reviewed=False — юрист проверяет её в админке.
"""
from __future__ import annotations

import re
from datetime import date

from app import catalog as C
from app import rules

from .pravo import RawAct

_W = r"(?<![а-яёa-z0-9])"  # начало слова: «ии» не должно находиться внутри «Федерации»
TOPIC_STEMS: dict[str, list[str]] = {
    "pd": ["персональн"],
    "tax": ["налог", "ндс", "страхов", "бухгалтер", "отчётност", "отчетност", "акциз"],
    "labor": ["труд", "работник", "занятост", "отпуск", "заработн"],
    "ecom": ["интернет-торгов", "маркетплейс", "электронн\\w* торгов", "дистанционн\\w* торгов"],
    "ads": ["реклам", "маркировк"],
    "it": ["информационн", "цифров", "искусственн\\w* интеллект", "программн", "кибербезопасн", "связи"],
    "lic": ["лицензи"],
    "log": ["перевозк", "транспорт", "логистик", "грузоперевоз", "дорожн"],
    "ved": ["таможн", "экспорт", "импорт", "внешнеэкономическ", "валютн"],
}
SKIP = [r"о награждении", r"о назначении", r"об освобождении", r"о присвоении", r"ратификац", r"о помиловании",
        r"об утверждении состава", r"о почётн", r"о почетн", r"о даче согласия"]

TOPIC_RULES: dict[str, list[tuple[dict, str]]] = {
    "pd": [({"field": "pd_customers", "op": "is_true"}, "Компания собирает персональные данные клиентов"),
           ({"field": "pd_employees", "op": "is_true"}, "Компания обрабатывает персональные данные сотрудников"),
           ({"field": "pd_external", "op": "is_true"}, "Данные обрабатывает внешний сервис"),
           ({"field": "pd_cross", "op": "is_true"}, "Указана передача данных за границу")],
    "tax": [({"field": "form", "op": "not_empty"}, "Налоговые нормы касаются всех компаний (форма: {form})")],
    "labor": [({"field": "headcount", "op": "not_empty"}, "Указана численность сотрудников: {headcount}"),
              ({"field": "remote", "op": "is_true"}, "Есть сотрудники на дистанционной работе"),
              ({"field": "foreign_staff", "op": "is_true"}, "Есть иностранные сотрудники")],
    "ecom": [({"field": "digital", "op": "contains", "value": "Интернет-магазин"}, "У компании есть интернет-магазин"),
             ({"field": "digital", "op": "contains", "value": "Маркетплейсы"}, "Продажи через маркетплейсы"),
             ({"field": "industry", "op": "eq", "value": "Интернет-торговля"}, "Отрасль: интернет-торговля")],
    "ads": [({"field": "activities", "op": "contains", "value": "Маркетинг и реклама"},
             "Направление деятельности: маркетинг и реклама"),
            ({"field": "digital", "op": "contains", "value": "E-mail и SMS-рассылки"}, "Ведутся e-mail и SMS-рассылки")],
    "it": [({"field": "industry", "op": "eq", "value": "IT и технологии"}, "Отрасль: IT и технологии"),
           ({"field": "digital", "op": "contains", "value": "ИИ-сервисы"}, "Компания использует ИИ-сервисы"),
           ({"field": "digital", "op": "contains", "value": "Облачные хранилища"}, "Компания использует облачные хранилища")],
    "lic": [({"field": "licenses", "op": "not_empty"}, "В профиле указаны лицензии")],
    "log": [({"field": "industry", "op": "eq", "value": "Транспорт и логистика"}, "Отрасль: транспорт и логистика"),
            ({"field": "activities", "op": "contains", "value": "Грузоперевозки"},
             "Направление деятельности: грузоперевозки")],
    "ved": [({"field": "ved_export", "op": "is_true"}, "Компания экспортирует товары или услуги"),
            ({"field": "ved_import", "op": "is_true"}, "Компания импортирует товары")],
}

SYSTEM = """Ты помогаешь юристам сортировать акты. Тебе дано ТОЛЬКО название и тип акта. Не выдумывай содержание.
Верни JSON: {"relevant": true|false, "topic": "<ключ>", "impact": "high|med|low", "summary": "...",
"reasons": [{"when": <правило>, "text": "<почему акт может касаться компании>"}]}.
Ключи тематик: %s. relevant=false для награждений, назначений, ратификаций и актов без значения для бизнеса.
summary — одно предложение, строго по названию; если не уверен, верни пустую строку.
Правило: {"field": F, "op": OP, "value": V} либо {"any": [...]} / {"all": [...]}.
Разрешённые поля и операции: %s."""


def classify_by_rules(title: str) -> str | None:
    low = title.lower().replace("ё", "е")
    best, score = None, 0
    for topic, stems in TOPIC_STEMS.items():
        n = sum(1 for s in stems if re.search(_W + s.replace("ё", "е"), low))
        if n > score:
            best, score = topic, n
    return best


def impact_by_type(doc_type: str) -> str:
    t = (doc_type or "").lower()
    if "федеральный закон" in t or "кодекс" in t:
        return "high"
    if "указ" in t or "постановление" in t:
        return "med"
    return "low"


def _fields_doc() -> str:
    return "; ".join(f"{f} ({k}): {', '.join(sorted(rules.OPS_BY_KIND[k]))}" for f, k in rules.FIELD_KINDS.items())


def _llm(raw: RawAct, llm) -> dict | None:
    data = llm.complete_json(SYSTEM % (", ".join(C.TOPICS), _fields_doc()), f"Тип: {raw.type}\nНазвание: {raw.title}")
    if not data:
        return None
    try:
        if data.get("relevant") is False:
            return {"relevant": False}
        if data.get("topic") not in C.TOPICS or data.get("impact") not in C.IMPACT_RANK:
            return None
        reasons = []
        for r in data.get("reasons") or []:
            rules.validate_rule(r["when"])
            if isinstance(r.get("text"), str) and r["text"].strip():
                reasons.append({"when": r["when"], "text": r["text"].strip()[:300]})
        if not reasons:
            return None
        return {"relevant": True, "topic": data["topic"], "impact": data["impact"],
                "summary": str(data.get("summary") or "")[:500], "reasons": reasons}
    except (KeyError, TypeError, rules.RuleError):
        return None  # ответ модели не прошёл проверку — откатываемся на правила


def enrich(raw: RawAct, llm=None, today: date | None = None) -> dict | None:
    """Возвращает поля Change либо None, если акт не относится ни к одной тематике."""
    low = raw.title.lower()
    if any(re.search(p, low) for p in SKIP):
        return None
    via_llm = _llm(raw, llm) if llm else None
    if via_llm is not None and not via_llm["relevant"]:
        return None
    if via_llm:
        topic, impact, reasons, summary, confidence = (via_llm["topic"], via_llm["impact"], via_llm["reasons"],
                                                       via_llm["summary"], "mid")
    else:
        topic = classify_by_rules(raw.title)
        if topic is None:
            return None
        impact, confidence, summary = impact_by_type(raw.type), "low", ""
        reasons = [{"when": w, "text": t} for w, t in TOPIC_RULES[topic]]
    try:
        published = date.fromisoformat(raw.date) if raw.date else (today or date.today())
    except ValueError:
        published = today or date.today()
    return {
        "source_key": raw.eo_number, "topic": topic, "impact": impact, "published_at": published,
        "effective_at": None, "is_draft": False, "status": "published", "agency": "",
        "doc": f"{raw.type} {raw.number}".strip(), "source_url": raw.document_url, "confidence": confidence,
        "reviewed": False, "title": raw.title, "summary": summary or raw.title, "what": "", "who": "", "comment": "",
        "quote": "", "requires": [], "reasons": reasons, "regions": [], "actions": [], "related": [],
    }
