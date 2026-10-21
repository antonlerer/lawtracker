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
    n = sub.add_parser("notify", help="рассылка дайджестов")
    n.add_argument("--loop", type=int, default=0, help="повторять каждые N секунд (0 — один проход)")
    sub.add_parser("ingest", help="забрать новые акты с pravo.gov.ru")
    args = ap.parse_args()
    settings = get_settings()
    factory = _factory(settings)

    if args.cmd == "seed":
        from seed.load import load_demo_changes
        with factory() as db:
            print("Загружено изменений:", load_demo_changes(db))
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
