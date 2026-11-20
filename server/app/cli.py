"""Команды: python -m app.cli {seed,notify,ingest,serve}"""
from __future__ import annotations

import argparse
import logging
import time

from .config import get_settings
from .db import Base, make_engine, make_session_factory
from .mailer import ConsoleMailer, SmtpMailer
from .telegram import BotApiSender


def _factory(settings):
    from . import models  # noqa: F401
    engine = make_engine(settings.database_url)
    if settings.auto_create_tables:
        Base.metadata.create_all(engine)
    return make_session_factory(engine)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(prog="lawtracker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="загрузить демо-изменения")
    sub.add_parser("demo", help="создать демо-пользователя с заполненным профилем (demo@example.org)")
    n = sub.add_parser("notify", help="рассылка дайджестов")
    n.add_argument("--loop", type=int, default=0, help="повторять каждые N секунд (0 — один проход)")
    ig = sub.add_parser("ingest", help="забрать новые акты с pravo.gov.ru")
    ig.add_argument("--loop", type=int, default=0, help="повторять каждые N секунд (0 — один проход)")
    sub.add_parser("check-config", help="проверить боевую конфигурацию (код возврата 1, если есть ошибки)")
    sub.add_parser("purge-demo", help="удалить демо-изменения и демо-пользователя из боевой базы")
    rp = sub.add_parser("report", help="недельный отчёт беты: активность и обратная связь")
    rp.add_argument("--weeks", type=int, default=8)
    rp.add_argument("--json", action="store_true")
    inv = sub.add_parser("invite", help="создать коды приглашения в закрытую бету")
    inv.add_argument("--count", type=int, default=1)
    inv.add_argument("--email", default="")
    sub.add_parser("create-tables", help="создать таблицы без Alembic (только для разработки)")
    an = sub.add_parser("analyze", help="ИИ-разбор акта из текстового файла (проверка промпта и модели)")
    an.add_argument("--file", required=True, help="файл с текстом акта (UTF-8)")
    an.add_argument("--title", required=True)
    an.add_argument("--type", default="Федеральный закон")
    an.add_argument("--number", default="")
    an.add_argument("--published", default="", help="дата публикации ГГГГ-ММ-ДД")
    ev = sub.add_parser("evaluate", help="измерить качество ИИ-разбора на выборке, размеченной юристом")
    ev.add_argument("--gold", required=True, help="файл JSON Lines (пример: eval/gold_example.jsonl)")
    ev.add_argument("--json", action="store_true", help="вывести полный отчёт в JSON")
    args = ap.parse_args()
    settings = get_settings()
    if args.cmd == "check-config":
        from .prod import config_problems
        problems = config_problems(settings)
        for level, msg in problems:
            print(f"{level}: {msg}")
        errors = sum(1 for lv, _ in problems if lv == "ERROR")
        print(f"Итого: ошибок {errors}, предупреждений {len(problems) - errors}")
        raise SystemExit(1 if errors else 0)
    if args.cmd in ("seed", "demo") and settings.app_env == "production":
        raise SystemExit("В APP_ENV=production демо-данные не загружаются. Очистить: python -m app.cli purge-demo")
    factory = _factory(settings)

    if args.cmd == "seed":
        from seed.load import load_demo_changes
        with factory() as db:
            print("Загружено изменений:", load_demo_changes(db))
    elif args.cmd == "demo":
        from seed.load import create_demo_account, load_demo_changes
        with factory() as db:
            load_demo_changes(db)
            print(create_demo_account(db))
    elif args.cmd in ("analyze", "evaluate"):
        import json
        from pathlib import Path

        from ingest.analyze import Analyzer
        from ingest.evaluate import evaluate, format_report, load_gold
        from ingest.llm import make_llm
        from ingest.pravo import RawAct
        llm = make_llm(settings)
        if llm is None:
            raise SystemExit("Модель не настроена: задайте GIGACHAT_AUTH_KEY или LLM_BASE_URL + LLM_MODEL (+ LLM_API_KEY)")
        analyzer = Analyzer(llm, settings.analysis_passes)
        if args.cmd == "analyze":
            raw = RawAct("cli", args.type, args.published, args.number, args.title, "")
            res = analyzer.analyze(raw, Path(args.file).read_text(encoding="utf-8"))
            print(json.dumps({"relevant": res.relevant, "notes": res.notes, "report": res.report,
                              "fields": res.fields}, ensure_ascii=False, indent=2, default=str))
        else:
            rep = evaluate(load_gold(args.gold), analyzer)
            print(json.dumps(rep, ensure_ascii=False, indent=2, default=str) if args.json else format_report(rep))
    elif args.cmd == "notify":
        from .notify import dispatch
        mailer = (SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_user, settings.smtp_password,
                             settings.mail_from) if settings.smtp_host else ConsoleMailer())
        tg = BotApiSender(settings.bot_token)
        while True:
            with factory() as db:
                print(dispatch(db, settings, mailer, tg))
            if not args.loop:
                break
            time.sleep(args.loop)
    elif args.cmd == "ingest":
        from ingest.run import run_ingest
        while True:
            try:
                with factory() as db:
                    print(run_ingest(db, settings), flush=True)
            except Exception:  # сбой одного прохода не должен останавливать службу
                logging.getLogger("lawtracker").exception("ingest: проход завершился ошибкой")
                if not args.loop:
                    raise
            if not args.loop:
                break
            time.sleep(args.loop)
    elif args.cmd == "purge-demo":
        from .prod import purge_demo
        with factory() as db:
            print(purge_demo(db))
    elif args.cmd == "report":
        from .analytics import feedback_report, weekly_report
        with factory() as db:
            rep = {"weekly": weekly_report(db, args.weeks)["weeks"], "feedback": feedback_report(db)}
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            print("Неделя      рег.  активных  комп.  лента   оценка  «не про нас»")
            for w in rep["weekly"]:
                print(f"{w['week']}  {w['signups']:>4}  {w['active_users']:>8}  {w['active_companies']:>5}  "
                      f"{w['feed_open_share']:>5.0%}  {str(w['avg_rating'] or '—'):>6}  "
                      f"{'—' if w['not_relevant_share'] is None else format(w['not_relevant_share'], '.0%'):>10}")
            fb = rep["feedback"]
            print(f"\nОбратная связь: ответов {fb['total']}, средняя оценка {fb['avg_rating']}, "
                  f"«не про нас» {fb['not_relevant_share']} (цели: оценка ≥ 4, нерелевантных < 20 %)")
    elif args.cmd == "invite":
        from .models import BetaInvite
        from .routers.admin import new_invite_code
        with factory() as db:
            codes = [BetaInvite(code=new_invite_code(), email=args.email.strip().lower())
                     for _ in range(1 if args.email else args.count)]
            db.add_all(codes)
            db.commit()
            print("\n".join(c.code for c in codes))
    elif args.cmd == "create-tables":
        print("Таблицы созданы")


if __name__ == "__main__":
    main()
