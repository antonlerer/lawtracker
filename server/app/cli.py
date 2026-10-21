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
    sub.add_parser("ingest", help="забрать новые акты с pravo.gov.ru")
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
        with factory() as db:
            print(run_ingest(db, settings))


if __name__ == "__main__":
    main()
