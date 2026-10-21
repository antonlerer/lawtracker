from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.mailer import ConsoleMailer
from app.main import create_app
from app.telegram import FakeSender
from seed.load import load_demo_changes

TODAY = date(2026, 10, 20)
ADMIN = {"X-Admin-Token": "admin-secret"}
PASSWORD = "Passw0rd!"

DEMO_PROFILE = {
    "basics": {"name": "Нордвуд Логистика", "inn": "7707083893", "form": "ООО", "size": "small"},
    "industry": {"industry": "Транспорт и логистика", "activities": ["Грузоперевозки", "Складская логистика"],
                 "topics": ["pd", "tax", "labor"]},
    "regions": {"regions": ["Санкт-Петербург и ЛО", "Северо-Запад"]},
    "structure": {"branches": "yes", "foreign_share": True, "entities": 2},
    "staff": {"headcount": "16–100", "remote": True},
    "pd": {"pd_customers": True, "pd_employees": True, "pd_external": True},
    "digital": {"digital": ["CRM", "Облачные хранилища", "E-mail и SMS-рассылки"]},
    "contacts": {"contact_name": "Анна Ковалёва", "contact_email": "a.kovaleva@nordwood.ru",
                 "contact_role": "Руководитель юридического отдела"},
}


class Api:
    """Тонкая обёртка над TestClient: добавляет заголовок против CSRF и разбирает ответы."""

    def __init__(self, app, mailer, tg):
        self.client = TestClient(app, base_url="http://testserver")
        self.app, self.mailer, self.tg = app, mailer, tg

    def request(self, method, url, **kw):
        headers = {"X-Requested-With": "test", **kw.pop("headers", {})}
        return self.client.request(method, url, headers=headers, **kw)

    def get(self, url, **kw): return self.request("GET", url, **kw)
    def post(self, url, json=None, **kw): return self.request("POST", url, json=json, **kw)
    def put(self, url, json=None, **kw): return self.request("PUT", url, json=json, **kw)
    def patch(self, url, json=None, **kw): return self.request("PATCH", url, json=json, **kw)
    def delete(self, url, json=None, **kw): return self.request("DELETE", url, json=json, **kw)

    def clone(self):
        return Api(self.app, self.mailer, self.tg)

    def last_code(self, to: str) -> str:
        for m in reversed(self.mailer.outbox):
            if m["to"] == to and "Код подтверждения" in m["subject"]:
                return re.search(r"\b(\d{6})\b", m["body"]).group(1)
        raise AssertionError("код не найден")

    def signup(self, email="owner@example.org", name="Анна Ковалёва", verify=True):
        r = self.post("/api/auth/register", {"name": name, "email": email, "password": PASSWORD, "consent": True})
        assert r.status_code == 201, r.text
        if verify:
            assert self.post("/api/auth/verify-email", {"code": self.last_code(email)}).status_code == 200
        return r.json()

    def make_company(self, plan="pro", profile=True):
        assert self.post("/api/company", {"form": "ООО", "name": "Нордвуд Логистика"}).status_code == 201
        cid = self.get("/api/company").json()["id"]
        if plan != "free":
            assert self.put(f"/api/admin/companies/{cid}/plan", {"plan": plan}, headers=ADMIN).status_code == 200
        if profile:
            for step, data in DEMO_PROFILE.items():
                r = self.put(f"/api/company/profile/{step}", {"data": data})
                assert r.status_code == 200, (step, r.text)
        return cid


@pytest.fixture
def env():
    settings = Settings(database_url="sqlite://", cookie_secure=False, admin_token="admin-secret",
                        telegram_webhook_secret="hook-secret", base_url="https://lt.example")
    mailer, tg = ConsoleMailer(), FakeSender()
    app = create_app(settings, mailer=mailer, telegram=tg, today=lambda: TODAY)
    with app.state.session_factory() as db:
        load_demo_changes(db, TODAY)
    return SimpleNamespace(app=app, mailer=mailer, tg=tg, settings=settings, api=Api(app, mailer, tg))


@pytest.fixture
def api(env):
    return env.api


@pytest.fixture
def owner(api):
    """Вошедший владелец с демо-компанией на тарифе «Профессиональный»."""
    api.signup()
    api.make_company("pro")
    return api
