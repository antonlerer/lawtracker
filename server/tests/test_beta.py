"""Функции беты: обратная связь, приглашения, согласия, метрики, очередь проверки, экспорт, миграции."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect

from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app
from app.mailer import ConsoleMailer
from app.telegram import FakeSender
from seed.load import load_demo_changes
from tests.conftest import ADMIN, PASSWORD, TODAY, Api

SERVER = Path(__file__).resolve().parents[1]


def first_change_id(api) -> str:
    return api.get("/api/changes?per_page=1").json()["items"][0]["id"]


# ---------- обратная связь ----------

def test_feedback_roundtrip_and_report(owner, env):
    cid = first_change_id(owner)
    assert owner.get(f"/api/changes/{cid}/feedback").json() == {"rating": None, "useful": None,
                                                                "not_relevant": False, "comment": ""}
    assert owner.put(f"/api/changes/{cid}/feedback", {"rating": 5, "useful": True}).json()["rating"] == 5
    r = owner.put(f"/api/changes/{cid}/feedback", {"not_relevant": True}).json()
    assert r["not_relevant"] is True and r["useful"] is False and r["rating"] == 5   # «не про нас» снимает «полезно»
    assert owner.put(f"/api/changes/{cid}/feedback", {}).status_code == 422
    assert owner.put(f"/api/changes/{cid}/feedback", {"rating": 6}).status_code == 422
    assert owner.put("/api/changes/nope/feedback", {"rating": 3}).status_code == 404
    rep = owner.get("/api/admin/feedback", headers=ADMIN).json()
    assert rep["total"] == 1 and rep["avg_rating"] == 5 and rep["not_relevant_share"] == 1
    csv = owner.get("/api/admin/feedback.csv", headers=ADMIN).text
    assert csv.splitlines()[0].startswith("user_id,company_id,change_id") and cid in csv and "@" not in csv


def test_feedback_requires_login(api):
    assert api.put("/api/changes/x/feedback", {"rating": 3}).status_code == 401


# ---------- закрытая бета ----------

@pytest.fixture
def closed():
    s = Settings(database_url="sqlite://", cookie_secure=False, admin_token="admin-secret", beta_invite_only=True)
    mailer, tg = ConsoleMailer(), FakeSender()
    app = create_app(s, mailer=mailer, telegram=tg, today=lambda: TODAY)
    with app.state.session_factory() as db:
        load_demo_changes(db, TODAY)
    return Api(app, mailer, tg)


def reg(api, email, code=""):
    return api.post("/api/auth/register", {"name": "Анна", "email": email, "password": PASSWORD, "consent": True,
                                           "invite_code": code})


def test_invite_only_registration(closed):
    assert reg(closed, "a@example.org").status_code == 403
    assert reg(closed, "a@example.org", "LT-WRONG").json()["error"]["fields"]["invite_code"]
    codes = closed.post("/api/admin/invites", {"count": 2}, headers=ADMIN).json()["codes"]
    assert len(codes) == 2 and all(c.startswith("LT-") for c in codes)
    assert reg(closed, "a@example.org", codes[0].lower()).status_code == 201     # регистр не важен
    assert reg(closed.clone(), "b@example.org", codes[0]).status_code == 403     # код одноразовый
    used = {i["code"]: i["used"] for i in closed.get("/api/admin/invites", headers=ADMIN).json()["invites"]}
    assert used[codes[0]] is True and used[codes[1]] is False


def test_invite_bound_to_email(closed):
    code = closed.post("/api/admin/invites", {"email": "Vip@Example.org"}, headers=ADMIN).json()["codes"][0]
    assert reg(closed, "other@example.org", code).status_code == 403
    assert reg(closed, "vip@example.org", code).status_code == 201


def test_team_invitee_can_register_without_code(closed):
    code = closed.post("/api/admin/invites", {}, headers=ADMIN).json()["codes"][0]
    assert reg(closed, "boss@example.org", code).status_code == 201
    closed.post("/api/auth/verify-email", {"code": closed.last_code("boss@example.org")})
    closed.make_company("pro", profile=False)
    assert closed.post("/api/team/invites", {"email": "mate@example.org", "role": "member"}).status_code == 201
    assert reg(closed.clone(), "mate@example.org").status_code == 201
    assert reg(closed.clone(), "stranger@example.org").status_code == 403


def test_meta_exposes_beta_flags(closed, api):
    m = closed.get("/api/meta").json()
    assert m["invite_only"] is True and "не юридическая консультация" in m["disclaimer"] and m["terms_version"]
    assert api.get("/api/meta").json()["invite_only"] is False


# ---------- заявки ----------

def test_beta_application_flow(env, api):
    body = {"company": "ООО Ромашка", "person": "Иван", "role": "Директор", "size": "малая", "industry": "Торговля",
            "regions": "Москва", "email": "Ivan@Example.org", "phone": "", "consent": True}
    assert api.post("/api/beta/apply", {**body, "consent": False}).status_code == 422
    assert api.post("/api/beta/apply", {**body, "email": "bad"}).status_code == 422
    assert api.post("/api/beta/apply", body).status_code == 201
    apps = api.get("/api/admin/applications", headers=ADMIN).json()["applications"]
    assert len(apps) == 1 and apps[0]["email"] == "ivan@example.org" and apps[0]["status"] == "new"
    r = api.patch(f"/api/admin/applications/{apps[0]['id']}", {"status": "invited", "send_invite": True}, headers=ADMIN)
    code = r.json()["code"]
    assert code.startswith("LT-")
    assert any(code in m["body"] and m["to"] == "ivan@example.org" for m in env.mailer.outbox)
    assert api.post("/api/beta/apply", body).status_code == 201


def test_beta_apply_is_throttled(api):
    body = {"company": "X", "email": "x@example.org", "consent": True}
    codes = [api.post("/api/beta/apply", body).status_code for _ in range(6)]
    assert codes[:5] == [201] * 5 and codes[5] == 429


# ---------- согласия, экспорт, e-mail ----------

def test_consent_is_versioned_and_exported(owner, env):
    data = owner.get("/api/account/export")
    assert data.status_code == 200 and "attachment" in data.headers["content-disposition"]
    j = data.json()
    assert j["user"]["consent_version"] == env.settings.terms_version
    assert {c["doc"] for c in j["consents"]} == {"privacy", "terms"}
    assert j["company"]["profile"]["basics"]["name"] == "Нордвуд Логистика" if "basics" in j["company"]["profile"] \
        else j["company"]["profile"]["name"] == "Нордвуд Логистика"
    assert "password" not in str(j).lower().replace("password_hash", "") or "hash" not in str(j)


def test_export_requires_login(api):
    assert api.get("/api/account/export").status_code == 401


def test_email_change(owner, env):
    r = owner.post("/api/account/email", {"new_email": "new@example.org", "password": "wrong"})
    assert r.status_code == 400
    assert owner.post("/api/account/email", {"new_email": "bad", "password": PASSWORD}).status_code == 422
    assert owner.post("/api/account/email", {"new_email": "new@example.org", "password": PASSWORD}).status_code == 200
    mail = env.mailer.outbox[-1]
    assert mail["to"] == "new@example.org"
    import re
    code = re.search(r"\b(\d{6})\b", mail["body"]).group(1)
    assert owner.post("/api/account/email/confirm", {"code": "000000"}).status_code == 400
    r = owner.post("/api/account/email/confirm", {"code": code})
    assert r.status_code == 200 and r.json()["user"]["email"] == "new@example.org"
    assert owner.get("/api/auth/me").json()["user"]["email"] == "new@example.org"
    other = owner.clone()
    assert other.post("/api/auth/login", {"email": "new@example.org", "password": PASSWORD}).status_code == 200


def test_email_change_rejects_taken_address(owner, env):
    other = owner.clone()
    other.signup("busy@example.org")
    r = owner.post("/api/account/email", {"new_email": "busy@example.org", "password": PASSWORD})
    assert r.status_code == 409


# ---------- метрики ----------

def test_weekly_metrics_collects_events(owner):
    cid = first_change_id(owner)
    owner.get("/api/changes")
    owner.get(f"/api/changes/{cid}")
    owner.put(f"/api/changes/{cid}/feedback", {"rating": 4})
    rep = owner.get("/api/admin/metrics?weeks=2", headers=ADMIN).json()["weeks"]
    last = rep[-1]
    assert last["signups"] == 1 and last["active_users"] == 1 and last["active_companies"] == 1
    assert last["feed_openers"] == 1 and last["feed_open_share"] == 1 and last["change_opens"] == 1
    assert last["feedback_count"] == 1 and last["avg_rating"] == 4
    assert owner.get("/api/admin/metrics.csv?weeks=2", headers=ADMIN).text.startswith("week,signups")


def test_client_event_change_open(owner):
    cid = first_change_id(owner)
    assert owner.post("/api/events", {"kind": "change_open", "ref": cid}).status_code == 202
    assert owner.post("/api/events", {"kind": "change_open", "ref": cid}).status_code == 202      # повтор не дублируется
    assert owner.post("/api/events", {"kind": "signup", "ref": cid}).status_code == 422          # чужие виды событий нельзя
    assert owner.post("/api/events", {"kind": "change_open", "ref": "nope"}).status_code == 404
    assert owner.get("/api/admin/metrics?weeks=1", headers=ADMIN).json()["weeks"][0]["change_opens"] == 1
    assert owner.clone().post("/api/events", {"kind": "change_open", "ref": cid}).status_code == 401


def test_feed_open_not_duplicated_within_ten_minutes(owner):
    for _ in range(4):
        owner.get("/api/changes")
    assert owner.get("/api/admin/metrics", headers=ADMIN).json()["weeks"][-1]["feed_openers"] == 1
    from sqlalchemy import func, select

    from app.models import Event
    with owner.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Event).where(Event.kind == "feed_open")) == 1


# ---------- очередь проверки юристом ----------

def test_review_queue(env, api):
    q = api.get("/api/admin/changes?reviewed=no", headers=ADMIN).json()["changes"]
    assert q and all(not c["reviewed"] for c in q)
    cid = q[0]["id"]
    full = api.get(f"/api/admin/changes/{cid}", headers=ADMIN).json()
    assert full["quote"] is not None and "requires" in full
    r = api.patch(f"/api/admin/changes/{cid}", {"reviewed": True, "confidence": "high", "summary": "Правка юриста"},
                  headers=ADMIN)
    assert r.status_code == 200 and r.json()["reviewed"] is True and r.json()["summary"] == "Правка юриста"
    assert cid not in [c["id"] for c in api.get("/api/admin/changes?reviewed=no", headers=ADMIN).json()["changes"]]
    assert api.patch(f"/api/admin/changes/{cid}", {"status": "hidden"}, headers=ADMIN).json()["status"] == "hidden"
    assert api.patch(f"/api/admin/changes/{cid}", {"topic": "нет-такой"}, headers=ADMIN).status_code == 422
    bad = {"requires": [{"op": "eval", "field": "x"}]}
    assert api.patch(f"/api/admin/changes/{cid}", bad, headers=ADMIN).status_code == 422
    assert api.patch("/api/admin/changes/nope", {"reviewed": True}, headers=ADMIN).status_code == 404


def test_admin_endpoints_hidden_without_token(env, api):
    for path in ("/api/admin/changes", "/api/admin/invites", "/api/admin/applications", "/api/admin/metrics",
                 "/api/admin/feedback", "/api/admin/feedback.csv"):
        assert api.get(path).status_code == 404, path
        assert api.get(path, headers={"X-Admin-Token": "wrong"}).status_code == 404, path


def test_admin_page_is_served(api):
    r = api.get("/admin")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "X-Admin-Token" in r.text


# ---------- письма, заголовки, продакшен ----------

def test_digest_has_disclaimer_review_mark_and_settings_link(env):
    from app.notify import render_message
    subject, text = render_message([{"title": "Тест", "impact": "high", "days_left": 5, "reviewed": False}],
                                   "https://lt.example", False, "help@lt.example")
    assert "не проверено юристом" in text and "не юридическая консультация" in text
    assert "https://lt.example/#/app/notifications" in text and "help@lt.example" in text
    _, text2 = render_message([{"title": "Тест", "impact": "low", "days_left": None, "reviewed": True}],
                              "https://lt.example", False)
    assert "проверено юристом" in text2 and "не проверено" not in text2


def test_security_headers(api):
    h = api.get("/api/healthz").headers
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" not in h      # тестовая конфигурация без Secure


def test_hsts_in_secure_mode():
    s = Settings(database_url="sqlite://", cookie_secure=True)
    api = Api(create_app(s, mailer=ConsoleMailer(), telegram=FakeSender()), ConsoleMailer(), FakeSender())
    assert "max-age" in api.get("/api/healthz").headers["strict-transport-security"]


def test_config_check_flags_demo_settings():
    from app.prod import config_problems
    bad = {m for lv, m in config_problems(Settings(database_url="sqlite://", demo_mode=True, cookie_secure=False)) if lv == "ERROR"}
    assert any("DEMO_MODE" in m for m in bad) and any("COOKIE_SECURE" in m for m in bad)
    assert any("SQLite" in m for m in bad)
    good = Settings(database_url="postgresql+psycopg2://u:p@db/lt", demo_mode=False, cookie_secure=True,
                    base_url="https://lt.ru", admin_token="x" * 48, smtp_host="smtp.lt.ru", mail_from="LT <no@lt.ru>",
                    support_email="help@lt.ru", app_env="production", auto_create_tables=False, bot_token="t",
                    telegram_webhook_secret="s")
    assert [m for lv, m in config_problems(good) if lv == "ERROR"] == []


def test_purge_demo_removes_demo_data(env):
    from sqlalchemy import func, select

    from app.models import Change, User
    from app.prod import purge_demo
    from seed.load import create_demo_account
    with env.app.state.session_factory() as db:
        create_demo_account(db)
        assert db.scalar(select(func.count()).select_from(User)) == 1
        res = purge_demo(db)
        assert res["users"] == 1 and res["changes"] > 5
        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert db.scalar(select(func.count()).select_from(Change)) == 0


def test_production_blocks_seed_commands():
    for cmd in ("seed", "demo"):
        r = subprocess.run([sys.executable, "-m", "app.cli", cmd], cwd=SERVER, capture_output=True, text=True,
                           env={**os.environ, "APP_ENV": "production", "DATABASE_URL": "sqlite://"})
        assert r.returncode != 0 and "purge-demo" in (r.stderr + r.stdout)


def test_unhandled_error_sends_alert(env, monkeypatch):
    from app import alerts
    alerts._last = 0.0
    s = Settings(database_url="sqlite://", cookie_secure=False, alert_chat_id="123", bot_token="tok")
    tg = FakeSender()
    app = create_app(s, mailer=ConsoleMailer(), telegram=tg, today=lambda: TODAY)

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("boom")
    from fastapi.testclient import TestClient
    r = TestClient(app, raise_server_exceptions=False).get("/api/boom")
    assert r.status_code == 500 and r.json()["error"]["code"] == "server_error"
    assert tg.sent and tg.sent[-1][0] == "123"


# ---------- Alembic ----------

def test_alembic_matches_models(tmp_path):
    """Чистая база поднимается миграциями, и схема совпадает с моделями (иначе нужна новая миграция)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    db = tmp_path / "m.db"
    env_vars = {**os.environ, "DATABASE_URL": f"sqlite:///{db}"}
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=SERVER, env=env_vars,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    engine = make_engine(f"sqlite:///{db}")
    assert set(inspect(engine).get_table_names()) >= set(Base.metadata.tables) | {"alembic_version"}
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"модели расходятся с миграциями, создайте новую миграцию: {diff}"
