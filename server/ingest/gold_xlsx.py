"""Эталонная выборка для юриста в Excel и перевод в формат оценки качества.

  python -m ingest.gold_xlsx template gold.xlsx     # пустой шаблон с подсказками и примерами
  python -m ingest.gold_xlsx convert gold.xlsx gold.jsonl
  python -m app.cli evaluate --gold gold.jsonl

Лист «Акты»: одна строка — один акт; юрист вставляет ТЕКСТ акта (или его существенную часть) и свою разметку.
Лист «Профили»: типовые компании (строка — компания). Лист «Применимость»: для каждого акта и профиля — «да»/«нет».
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime

from app import catalog as C

ACT_COLS = ["id", "Название акта", "Вид (закон, приказ…)", "Номер", "Дата публикации (ГГГГ-ММ-ДД)", "ТЕКСТ акта",
            "Значим для малого бизнеса? (да/нет)", "Тематика (код)", "Влияние (high/med/low)",
            "Дата вступления в силу (ГГГГ-ММ-ДД, пусто — не определена)", "Это проект? (да/нет)", "Комментарий юриста"]
PROFILE_BOOL = ["pd_customers", "pd_employees", "pd_external", "pd_cross", "ved_export", "ved_import", "remote",
                "foreign_staff", "foreign_share"]
PROFILE_TEXT = ["industry", "headcount", "size", "branches"]
PROFILE_LIST = ["regions", "digital", "licenses", "activities"]
PROFILE_COLS = ["Код профиля"] + PROFILE_TEXT + PROFILE_BOOL + PROFILE_LIST
EXAMPLE_PROFILES = [
    ["P1", "Транспорт и логистика", "16–100", "small", "yes"] + ["да", "да", "да", "нет", "нет", "нет", "да", "нет", "нет"]
    + ["Санкт-Петербург и ЛО", "CRM; Облачные хранилища", "", "Грузоперевозки"],
    ["P2", "Строительство", "до 15", "micro", "no"] + ["нет"] * 9 + ["Москва и МО", "", "", ""],
]


def make_template(path: str) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Инструкция"
    lines = ["Как заполнять эталонную выборку", "",
             "1. Лист «Профили»: опишите 5–10 типовых компаний (строка = компания). Булевы поля — «да»/«нет», списки — через «;».",
             "2. Лист «Акты»: на каждый реальный акт одна строка. В колонку «ТЕКСТ акта» вставьте текст с pravo.gov.ru (можно главные статьи).",
             "   Разметьте: значим ли акт для малого бизнеса, тематику, влияние, дату вступления в силу, проект ли это.",
             "3. Лист «Применимость»: для каждой пары «акт × профиль» поставьте «да», если акт касается такой компании, иначе «нет».",
             "4. Нужно минимум 30 актов (лучше 50–100): примерно половина значимых и половина нет.", "",
             "Коды тематик: " + "; ".join(f"{k} — {v}" for k, v in C.TOPICS.items()),
             "Влияние: high — высокое (штрафы, сроки, обязательные действия), med — среднее, low — информационное."]
    for i, t in enumerate(lines, 1):
        ws.cell(i, 1, t)
    ws["A1"].font = Font(bold=True, size=14)
    ws.column_dimensions["A"].width = 140
    head = PatternFill("solid", fgColor="DDD6FE")
    acts = wb.create_sheet("Акты")
    acts.append(ACT_COLS)
    acts.append(["a1", "О внесении изменений в статью 10 Федерального закона «О персональных данных»", "Федеральный закон",
                 "1-ФЗ", "2026-09-10", "Статья 1. Внести в статью 10 … (вставьте текст)", "да", "pd", "high", "2026-12-09", "нет",
                 "ПРИМЕР: строку удалите"])
    profiles = wb.create_sheet("Профили")
    profiles.append(PROFILE_COLS)
    for r in EXAMPLE_PROFILES:
        profiles.append(r)
    app = wb.create_sheet("Применимость")
    app.append(["id акта", "Код профиля", "Касается этой компании? (да/нет)"])
    app.append(["a1", "P1", "да"])
    app.append(["a1", "P2", "нет"])
    for sh in (acts, profiles, app):
        for c in sh[1]:
            c.font, c.fill = Font(bold=True), head
            c.alignment = Alignment(wrap_text=True, vertical="top")
        sh.freeze_panes = "A2"
    for col, w in zip("ABCDEFGHIJKL", [10, 50, 22, 12, 18, 80, 18, 14, 16, 26, 14, 40]):
        acts.column_dimensions[col].width = w
    app.column_dimensions["A"].width = 14
    app.column_dimensions["B"].width = 16
    app.column_dimensions["C"].width = 36
    wb.save(path)


def _yes(v) -> bool | None:
    s = str(v or "").strip().lower()
    if s in {"да", "yes", "true", "1", "+"}:
        return True
    if s in {"нет", "no", "false", "0", "-"}:
        return False
    return None


def _date(v) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    date.fromisoformat(s)         # ошибка формата — сразу видна юристу
    return s


def convert(src: str, dst: str) -> int:
    import openpyxl
    wb = openpyxl.load_workbook(src, data_only=True)
    problems: list[str] = []
    profiles: dict[str, dict] = {}
    for n, row in enumerate(wb["Профили"].iter_rows(min_row=2, values_only=True), 2):
        if not row or not row[0]:
            continue
        code, p = str(row[0]).strip(), {}
        vals = dict(zip(PROFILE_COLS[1:], row[1:]))
        for k in PROFILE_TEXT:
            if vals.get(k):
                p[k] = str(vals[k]).strip()
        for k in PROFILE_BOOL:
            b = _yes(vals.get(k))
            if b is not None:
                p[k] = b
        for k in PROFILE_LIST:
            if vals.get(k):
                p[k] = [x.strip() for x in str(vals[k]).split(";") if x.strip()]
        profiles[code] = p
    applic: dict[str, list[dict]] = {}
    for n, row in enumerate(wb["Применимость"].iter_rows(min_row=2, values_only=True), 2):
        if not row or not row[0]:
            continue
        aid, code, ans = str(row[0]).strip(), str(row[1] or "").strip(), _yes(row[2])
        if code not in profiles:
            problems.append(f"Применимость, строка {n}: нет профиля «{code}»")
        elif ans is None:
            problems.append(f"Применимость, строка {n}: ответ должен быть «да» или «нет»")
        else:
            applic.setdefault(aid, []).append({"profile": profiles[code], "applies": ans})
    out = []
    for n, row in enumerate(wb["Акты"].iter_rows(min_row=2, values_only=True), 2):
        if not row or not row[0] or "ПРИМЕР" in str(row[11] or ""):
            continue
        aid, title, typ, num, pub, text, rel, topic, impact, eff, draft, _ = (list(row) + [None] * 12)[:12]
        try:
            item = {"id": str(aid), "title": str(title or "").strip(), "type": str(typ or "").strip(),
                    "number": str(num or "").strip(), "published": _date(pub), "text": str(text or "").strip()}
            exp: dict = {}
            if (r := _yes(rel)) is not None:
                exp["relevant"] = r
            if topic:
                if str(topic).strip() not in C.TOPICS:
                    problems.append(f"Акты, строка {n}: неизвестная тематика «{topic}»")
                exp["topic"] = str(topic).strip()
            if impact:
                if str(impact).strip() not in C.IMPACT_NAMES:
                    problems.append(f"Акты, строка {n}: влияние должно быть high, med или low")
                exp["impact"] = str(impact).strip()
            exp["effective_date"] = _date(eff)
            if (d := _yes(draft)) is not None:
                exp["is_draft"] = d
            item["expected"] = exp
            if aid in applic or str(aid) in applic:
                item["profiles"] = applic.get(str(aid), [])
            if not item["title"] or len(item["text"]) < 80:
                problems.append(f"Акты, строка {n}: нужны название и текст акта (не короче 80 символов)")
            out.append(item)
        except ValueError as exc:
            problems.append(f"Акты, строка {n}: {exc}")
    if problems:
        raise SystemExit("Исправьте в файле:\n  " + "\n  ".join(problems))
    with open(dst, "w", encoding="utf-8") as f:
        f.write("# Эталонная выборка, размечена юристом\n")
        for it in out:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return len(out)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "template":
        make_template(sys.argv[2])
        print("Шаблон создан:", sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "convert":
        print("Актов в выборке:", convert(sys.argv[2], sys.argv[3]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
