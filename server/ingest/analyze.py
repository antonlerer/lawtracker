"""ИИ-разбор акта: вызов модели, разбор ответа и проверка кодом всего, что можно проверить.

Модель предлагает — код проверяет. Цитаты ищутся в тексте дословно, даты считаются программой, правила применимости
проходят `rules.validate_rule`, числа в пояснениях сверяются с текстом. Всё непроверенное отбрасывается, а уверенность
понижается; запись в любом случае создаётся с reviewed=False (проверяет юрист).
"""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import catalog as C
from app import rules

from .llm import LLM
from .pravo import RawAct
from .prompts import MAX_BASIS, MAX_QUOTE, PROMPT_VERSION, REPAIR_PROMPT, system_prompt, user_prompt

LEVELS = ["low", "mid", "high"]
EXCERPT_BUDGET = 24000
KEY_RE = re.compile(r"вступа[её]т в силу|срок|не позднее|обязан|обязательн|запрещ|штраф|ответственност|утрачива|по истечении|"
                    r"настоящий федеральный закон|настоящ\w+ (?:постановление|приказ)|с 1 \w+ \d{4}", re.I)


# ---------- схема ответа модели (лишние ключи игнорируются, типы приводятся строго) ----------

class ActionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = ""
    responsible: str = ""
    basis_quote: str | None = None
    due_phrase: str | None = None


class ReasonOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    when: dict
    text: str = ""
    basis_quote: str | None = None


class AnalysisOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    relevant: bool
    irrelevant_reason: str = ""
    is_draft: bool = False
    topic: str | None = None
    secondary_topics: list[str] = Field(default_factory=list)
    impact: Literal["high", "med", "low"] = "low"
    impact_reason: str = ""
    summary: str = ""
    what_changed: str = ""
    who_affected: str = ""
    comment: str = ""
    effective_phrase: str | None = None
    effective_date: str | None = None
    key_quote: str | None = None
    actions: list[ActionOut] = Field(default_factory=list)
    requires: list[dict] = Field(default_factory=list)
    reasons: list[ReasonOut] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    confidence: Literal["high", "mid", "low"] = "low"
    uncertainties: list[str] = Field(default_factory=list)


@dataclass
class AnalysisResult:
    relevant: bool | None                       # None — анализ не удался (нет ответа модели / нет текста)
    fields: dict | None = None                  # поля Change; None, если акт нерелевантен или анализ не удался
    notes: list[str] = field(default_factory=list)
    report: dict = field(default_factory=dict)
    answers: list[dict] = field(default_factory=list)


# ---------- нормализация и поиск цитат ----------

_TRANS = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "‹": '"', "›": '"', " ": " ",
                        "–": "-", "—": "-", "−": "-", "‑": "-", "ё": "е", "Ё": "Е"})


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").translate(_TRANS)).strip().casefold()


def quote_in(haystack_norm: str, quote: str | None, limit: int = MAX_QUOTE) -> bool:
    """Цитата найдена в тексте дословно (с точностью до пробелов, видов кавычек и тире)."""
    if not quote or len(quote) > limit + 20:
        return False
    parts = [p.strip() for p in re.split(r"\.\.\.|…|\[…\]", norm(quote)) if p.strip()]
    return bool(parts) and all(len(p) >= 8 and p in haystack_norm for p in parts)


def parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


def select_excerpt(text: str, budget: int = EXCERPT_BUDGET) -> tuple[str, bool]:
    """Для длинных актов: начало + фрагменты со сроками и обязанностями (в исходном порядке)."""
    text = text.strip()
    if len(text) <= budget:
        return text, False
    head_len = min(8000, budget // 3)
    head, rest = text[:head_len], text[head_len:]
    chunks = [c.strip() for c in re.split(r"(?<=[.;:])\s+|\n+", rest) if c.strip()]
    scored = [(i, c) for i, c in enumerate(chunks) if KEY_RE.search(c)]
    chosen, used = [], len(head)
    for i, c in scored:
        if used + len(c) + 5 > budget:
            break
        chosen.append((i, c))
        used += len(c) + 5
    return head + "\n---\n" + "\n".join(c for _, c in sorted(chosen)), True


# ---------- даты ----------

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
          "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
NUM_WORDS = {"десяти": 10, "пятнадцати": 15, "двадцати": 20, "тридцати": 30, "сорока пяти": 45, "шестидесяти": 60,
             "девяноста": 90, "ста": 100, "ста двадцати": 120, "ста восьмидесяти": 180,
             "одного": 1, "двух": 2, "трех": 3, "четырех": 4, "пяти": 5, "шести": 6, "девяти": 9, "двенадцати": 12}
_NUM = r"(\d+|" + "|".join(sorted(NUM_WORDS, key=len, reverse=True)) + r")"


def _num(token: str) -> int:
    return int(token) if token.isdigit() else NUM_WORDS[token]


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def resolve_date(phrase: str | None, published: date | None) -> tuple[date | None, str]:
    """Считает дату из фразы акта. Возвращает (дата|None, пояснение). Никогда не угадывает."""
    p = norm(phrase or "")
    if not p:
        return None, ""
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", p)
    if m:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))), "календарная дата в тексте"
        except ValueError:
            return None, "некорректная дата в тексте"
    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", p)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))), "календарная дата в тексте"
        except ValueError:
            return None, "некорректная дата в тексте"
    if "опубликован" in p and published:
        if "рабоч" in p:
            return None, "срок в рабочих днях: считать по календарю вручную"
        m = re.search(r"по истечении\s+" + _NUM + r"\s+(?:календарных\s+)?дн", p)
        if m:
            return published + timedelta(days=_num(m.group(1))), "по формуле от даты публикации"
        m = re.search(r"по истечении\s+" + _NUM + r"\s+месяц", p)
        if m:
            return _add_months(published, _num(m.group(1))), "по формуле от даты публикации"
        if re.search(r"со дня (?:его |их )?(?:официального )?опубликования", p):
            return published, "со дня опубликования"
    return None, ""


def date_in_text(text_norm: str, d: date) -> bool:
    rus = next(k for k, v in MONTHS.items() if v == d.month)
    return (f"{d.day} {rus} {d.year}" in text_norm or f"{d.day:02d}.{d.month:02d}.{d.year}" in text_norm)


# ---------- числа в пояснениях ----------

_NUMBER_RE = re.compile(r"\d[\d\s.,]*\d|\d")


def _digit_tokens(s: str) -> set[str]:
    return {re.sub(r"\D", "", m) for m in _NUMBER_RE.findall(s or "")}


def ungrounded_numbers(texts: list[str], source_norm: str) -> list[str]:
    """Числа (2+ цифры) из пояснений, которых нет в тексте акта. Признак выдумки модели."""
    known = _digit_tokens(source_norm)
    wanted = set().union(*[_digit_tokens(t) for t in texts]) if texts else set()
    return sorted(n for n in wanted if len(n) >= 2 and n not in known)


# ---------- анализатор ----------

class Analyzer:
    def __init__(self, llm: LLM, passes: int = 2, budget: int = EXCERPT_BUDGET) -> None:
        self.llm, self.passes, self.budget = llm, max(1, passes), budget

    # --- вызов модели ---
    def _ask(self, system: str, user: str, temperature: float) -> tuple[AnalysisOut | None, dict | None, str]:
        reply = self.llm.chat(system, user, temperature=temperature)
        if reply is None:
            return None, None, "нет ответа модели"
        data = parse_json(reply)
        if data is None:
            problem = "ответ не является JSON-объектом"
        else:
            try:
                return AnalysisOut.model_validate(data), data, ""
            except ValidationError as exc:
                problem = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:4])
        # одна попытка починить формат
        fix = self.llm.chat(system, user + "\n\n" + REPAIR_PROMPT.format(problem=problem), temperature=0.0)
        data = parse_json(fix)
        if data is not None:
            try:
                return AnalysisOut.model_validate(data), data, ""
            except ValidationError:
                pass
        return None, None, f"ответ не прошёл проверку формата ({problem})"

    def analyze(self, raw: RawAct, text: str | None, today: date | None = None) -> AnalysisResult:
        today = today or date.today()
        if not text or len(text.strip()) < 80:
            return AnalysisResult(None, notes=["Нет текста акта: ИИ-разбор невозможен, нужен текст или ручная работа юриста"])
        excerpt, truncated = select_excerpt(text, self.budget)
        published = _parse_iso(raw.date)
        system = system_prompt()
        user = user_prompt(title=raw.title, doc_type=raw.type, number=raw.number, published=raw.date, today=today,
                           url=raw.document_url, text=excerpt, truncated=truncated)
        outs: list[AnalysisOut] = []
        answers: list[dict] = []
        failures: list[str] = []
        for i in range(self.passes):
            out, data, problem = self._ask(system, user, 0.0 if i == 0 else 0.3)
            if out is None:
                failures.append(problem)
            else:
                outs.append(out)
                answers.append(data)
        report = {"prompt_version": PROMPT_VERSION, "model": self.llm.name, "passes_requested": self.passes,
                  "passes_ok": len(outs), "truncated": truncated, "text_chars": len(text)}
        if not outs:
            return AnalysisResult(None, notes=["ИИ-разбор не удался: " + "; ".join(dict.fromkeys(failures))],
                                  report=report)
        src = norm(excerpt)
        meta_norm = norm(f"{raw.title} {raw.type} {raw.number} {raw.date}")
        relevant_votes = [o.relevant for o in outs]
        if not any(relevant_votes):
            report["agree"] = len(set(relevant_votes)) == 1
            return AnalysisResult(False, notes=[outs[0].irrelevant_reason or "Акт не относится к бизнесу"],
                                  report=report, answers=answers)
        main = next(o for o in outs if o.relevant)
        main_date = self._effective(main, published, src, [])
        agree, diffs = True, []
        for other in outs:
            if other is main:
                continue
            if not other.relevant:
                agree = False
                diffs.append("релевантность")
                continue
            if other.topic != main.topic:
                agree = False
                diffs.append("тематика")
            if other.impact != main.impact:
                agree = False
                diffs.append("уровень влияния")
            if self._effective(other, published, src, []) != main_date:
                agree = False
                diffs.append("срок вступления в силу")
        report["agree"] = agree
        notes: list[str] = []
        if not agree:
            notes.append("Проходы модели разошлись: " + ", ".join(dict.fromkeys(diffs)) + ". Нужна проверка юристом")
        fields = self._build(raw, main, text, src, meta_norm, published, truncated, agree, report, notes)
        return AnalysisResult(True, fields, notes, report, answers)

    # --- срок вступления в силу ---
    def _effective(self, out: AnalysisOut, published: date | None, src: str, notes: list[str]) -> date | None:
        if out.is_draft:
            return None
        if out.effective_phrase and quote_in(src, out.effective_phrase, 400):
            d, how = resolve_date(out.effective_phrase, published)
            if d:
                return d
            if how:
                notes.append(f"Срок вступления в силу: {how}")
        elif out.effective_phrase:
            notes.append("Фраза о вступлении в силу не найдена в тексте и не использована")
        if out.effective_date:
            d = _parse_iso(out.effective_date)
            if d and date_in_text(src, d):
                return d
            if d:
                notes.append("Дата вступления в силу, названная моделью, не найдена в тексте и не использована")
        return None

    # --- сборка и проверка ---
    def _build(self, raw: RawAct, out: AnalysisOut, text: str, src: str, meta_norm: str, published: date | None,
               truncated: bool, agree: bool, report: dict, notes: list[str]) -> dict | None:
        from .enrich import TOPIC_RULES, classify_by_rules

        level = LEVELS.index(out.confidence)
        topic = out.topic if out.topic in C.TOPICS else None
        if topic is None:
            topic = classify_by_rules(raw.title)
            notes.append("Тематика от модели недопустима, определена по правилам")
            level = 0
        if topic is None:
            report["error"] = "тематика не определена"
            return None

        quotes_total = quotes_ok = 0

        key_quote = None
        if out.key_quote:
            quotes_total += 1
            if quote_in(src, out.key_quote):
                quotes_ok += 1
                key_quote = out.key_quote.strip()
            else:
                notes.append("Ключевая цитата не найдена в тексте и убрана")
                level = min(level, 1)

        reasons, dropped_reasons = [], 0
        for r in out.reasons:
            try:
                rules.validate_rule(r.when)
            except rules.RuleError as exc:
                dropped_reasons += 1
                notes.append(f"Причина отброшена: недопустимое правило ({exc})")
                continue
            if not r.text.strip():
                dropped_reasons += 1
                continue
            if r.basis_quote:
                quotes_total += 1
                if not quote_in(src, r.basis_quote, MAX_BASIS):
                    dropped_reasons += 1
                    notes.append("Причина отброшена: цитата-основание не найдена в тексте")
                    continue
                quotes_ok += 1
            reasons.append({"when": r.when, "text": r.text.strip()[:160]})
        requires = []
        for q in out.requires:
            try:
                rules.validate_rule(q)
                requires.append(q)
            except rules.RuleError as exc:
                notes.append(f"Жёсткое условие отброшено: недопустимое правило ({exc})")
        if not reasons:
            reasons = [{"when": w, "text": t} for w, t in TOPIC_RULES[topic]]
            requires = []                      # шаблон по тематике шире: жёсткие условия модели к нему не относятся
            notes.append("Причины применимости заменены шаблоном по тематике: от модели не осталось проверенных")
            level = min(level, 1)
        elif dropped_reasons:
            level = min(level, 1)

        effective = self._effective(out, published, src, notes)

        actions, dropped_actions = [], 0
        for a in out.actions[:5]:
            if not a.title.strip():
                continue
            quotes_total += 1
            if not quote_in(src, a.basis_quote, MAX_BASIS):
                dropped_actions += 1
                continue
            quotes_ok += 1
            due, _ = resolve_date(a.due_phrase, published) if a.due_phrase and quote_in(src, a.due_phrase, 400) else (None, "")
            actions.append({"title": a.title.strip()[:160], "responsible": a.responsible.strip()[:80],
                            "due": (due or effective).isoformat() if (due or effective) else None})
        if dropped_actions:
            notes.append(f"Действий отброшено как необоснованные текстом: {dropped_actions}")
            level = min(level, 1)

        regions = [r for r in out.regions if r in C.REGIONS]
        if len(regions) != len(out.regions):
            notes.append("Часть регионов от модели не найдена в справочнике и отброшена")

        explain = [out.summary, out.what_changed, out.who_affected, out.comment]
        bad_numbers = ungrounded_numbers(explain, src + " " + meta_norm)
        if bad_numbers:
            notes.append("В пояснениях есть числа, которых нет в тексте: " + ", ".join(bad_numbers) + ". Проверьте вручную")
            level = 0
        if truncated:
            notes.append("Текст акта сокращён для разбора: возможны пропуски")
            level = min(level, 1)
        if not agree:
            level = 0
        if not out.summary.strip():
            notes.append("Модель не дала краткое изложение, использовано название акта")

        comment = out.comment.strip()[:500]
        for u in out.uncertainties[:5]:
            notes.append("Требует проверки: " + u.strip()[:200])
        report.update({"quotes_total": quotes_total, "quotes_verified": quotes_ok, "dropped_reasons": dropped_reasons,
                       "dropped_actions": dropped_actions, "ungrounded_numbers": bad_numbers,
                       "model_confidence": out.confidence})
        if out.uncertainties:
            comment = (comment + " Что проверить юристу: " + "; ".join(u.strip() for u in out.uncertainties[:3])).strip()
        quote = key_quote
        if quote and not quote.startswith("«"):
            quote = f"«{quote}»"
        return {
            "source_key": raw.eo_number, "topic": topic, "impact": out.impact, "published_at": published or date.today(),
            "effective_at": effective, "is_draft": out.is_draft, "status": "published", "agency": "",
            "doc": f"{raw.type} {raw.number}".strip(), "source_url": raw.document_url, "confidence": LEVELS[level],
            "reviewed": False, "title": raw.title, "summary": (out.summary.strip() or raw.title)[:400],
            "what": out.what_changed.strip()[:900], "who": out.who_affected.strip()[:400], "comment": comment[:700],
            "quote": quote or "", "requires": requires, "reasons": reasons, "regions": regions, "actions": actions,
            "related": [],
        }


def _parse_iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None
