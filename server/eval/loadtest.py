"""Замер скорости на тестовых данных: python eval/loadtest.py [--changes 400] [--users 50] [--repeat 5]

Создаёт в базе из DATABASE_URL (лучше временной!) изменения и пользователей с компаниями, затем замеряет основные
страницы через тот же ASGI-стек (без сети). Критерий беты: каждая страница отвечает быстрее 1–2 секунд.
"""
from __future__ import annotations

import argparse
import copy
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import AuthSession, Change, Company, Member, User, utcnow  # noqa: E402
from app.security import hash_token  # noqa: E402
from seed.load import load_demo_changes  # noqa: E402
from tests.conftest import DEMO_PROFILE  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--changes", type=int, default=400)
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--limit", type=float, default=1.0, help="допустимое время ответа, сек")
    a = ap.parse_args()
    s = Settings(cookie_secure=False, admin_token="x" * 40, enforce_plan_limits=False)
    app = create_app(s)
    if "sqlite" in s.database_url and ":memory:" not in s.database_url and s.database_url != "sqlite://":
        print("Внимание: тест пишет данные в", s.database_url)
    flat = {}
    for step in DEMO_PROFILE.values():
        flat.update(step)
    with app.state.session_factory() as db:
        load_demo_changes(db)
        base = db.query(Change).all()
        for i in range(a.changes):
            src = base[i % len(base)]
            d = {c.name: copy.deepcopy(getattr(src, c.name)) for c in Change.__table__.columns}
            d.update(id=f"load-{i}", source_key=None, title=f"{src.title} (вариант {i})",
                     published_at=src.published_at - timedelta(days=i % 90))
            db.add(Change(**d))
        tokens = []
        for i in range(a.users):
            u = User(email=f"load{i}@example.org", name=f"Пользователь {i}", password_hash="x", email_verified=True)
            db.add(u)
            db.flush()
            c = Company(plan="pro", profile=flat, steps_saved={k: True for k in DEMO_PROFILE})
            db.add(c)
            db.flush()
            db.add(Member(company_id=c.id, user_id=u.id, role="owner", status="active"))
            tok = f"loadtoken-{i}"
            db.add(AuthSession(user_id=u.id, token_hash=hash_token(tok), expires_at=utcnow() + timedelta(days=1)))
            tokens.append(tok)
        db.commit()
    cl = TestClient(app)
    cl.cookies.set(s.cookie_name, tokens[0])
    paths = [("обзор", "/api/overview"), ("лента, 1-я страница", "/api/changes?per_page=20"),
             ("лента, только мои", "/api/changes?personal=true&per_page=20&detail=true"),
             ("поиск по кириллице", "/api/changes?q=персональных&per_page=20"),
             ("фильтр по влиянию", "/api/changes?impact=high&sort=eff"), ("сохранённое", "/api/saved"),
             ("карточка", "/api/changes/load-7")]
    bad = 0
    print(f"Изменений в базе: {a.changes + len(base)}, пользователей: {a.users}, повторов: {a.repeat}")
    for name, path in paths:
        times = []
        for i in range(a.repeat):
            cl.cookies.set(s.cookie_name, tokens[i % len(tokens)])
            t = time.perf_counter()
            r = cl.get(path, headers={"X-Requested-With": "load"})
            times.append(time.perf_counter() - t)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
        worst = max(times)
        flag = "ОК " if worst < a.limit else "МЕДЛЕННО"
        bad += worst >= a.limit
        print(f"{flag}  {name:<24} среднее {statistics.mean(times)*1000:6.0f} мс   максимум {worst*1000:6.0f} мс")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
