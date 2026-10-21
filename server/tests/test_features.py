from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Company, Member, TelegramLink, User, utcnow
from app.notify import dispatch
from tests.conftest import ADMIN, PASSWORD


def err(r):
    return r.json()["error"]


def invite_and_join(env, owner, email, role="member", name="Гость"):
    r = owner.post("/api/team/invites", {"email": email, "role": role})
    assert r.status_code == 201, r.text
    code = env.mailer.outbox[-1]["body"].split("Код приглашения: ")[1].split()[0]
    guest = owner.clone()
    guest.signup(email, name=name)
    assert guest.post("/api/company/join", {"code": code}).status_code == 200
    return guest


# ---------------- уведомления ----------------

def test_notification_settings_roundtrip_and_validation(owner):
    s = owner.get("/api/notifications").json()
    assert s["settings"]["email_freq"] == "daily" and s["email_target"] == "a.kovaleva@nordwood.ru" and s["editable"]
    new = {**s["settings"], "email_freq": "weekly", "topics": ["pd", "tax"], "min_impact": "high",
           "days": [3, 1, 3], "time": "08:30", "pause": True, "pause_until": "2026-11-01"}
    r = owner.put("/api/notifications", new)
    assert r.status_code == 200 and r.json()["settings"]["days"] == [1, 3]
    assert owner.get("/api/notifications").json()["settings"]["pause_until"] == "2026-11-01"
    for bad, field in [({"time": "25:00"}, "time"), ({"time": "9:00"}, "time"), ({"days": []}, "days"),
                       ({"days": [0, 8]}, "days"), ({"topics": ["magic"]}, "topics"), ({"min_impact": "x"}, "min_impact"),
                       ({"email_freq": "hourly"}, "email_freq"), ({"hack": 1}, "hack")]:
        r = owner.put("/api/notifications", {**s["settings"], **bad})
        assert r.status_code == 422 and field in err(r)["fields"], bad


def test_notification_settings_need_paid_plan(api):
    api.signup()
    api.make_company("free", profile=False)
    assert api.get("/api/notifications").json()["editable"] is False
    assert api.put("/api/notifications", {"email_freq": "now"}).status_code == 402


# ---------------- Telegram ----------------

def connect_telegram(env, api, chat_id=777, username="anna_k"):
    code = api.post("/api/telegram/connect").json()["code"]
    update = {"message": {"chat": {"id": chat_id, "type": "private"}, "from": {"username": username},
                          "text": f" {code.lower()} "}}
    r = api.client.post("/api/telegram/webhook/hook-secret", json=update)
    assert r.status_code == 200, r.text
    return code, r.json()["result"]


def test_telegram_connect_flow(env, owner):
    assert owner.get("/api/telegram").json()["status"] == "none"
    pending = owner.post("/api/telegram/connect").json()
    assert pending["status"] == "pending" and pending["code"].startswith("LT-") and pending["bot"] == "LawTrackerBot"
    assert owner.get("/api/telegram").json()["code"] == pending["code"]
    code, result = connect_telegram(env, owner)
    assert result == "connected"
    st = owner.get("/api/telegram").json()
    assert st["status"] == "connected" and st["username"] == "@anna_k" and st["code"] is None
    assert env.tg.sent[-1][0] == "777" and "подключён" in env.tg.sent[-1][1]
    assert owner.post("/api/telegram/test").status_code == 200 and "тестовое" in env.tg.sent[-1][1]
    # код одноразовый
    r = owner.client.post("/api/telegram/webhook/hook-secret", json={"message": {"chat": {"id": 999, "type": "private"}, "text": code}})
    assert r.json()["result"] == "bad_code"
    assert owner.delete("/api/telegram").json()["status"] == "disabled"
    assert owner.post("/api/telegram/test").status_code == 409


def test_telegram_webhook_security_and_garbage(env, owner):
    c = owner.client
    assert c.post("/api/telegram/webhook/wrong", json={}).status_code == 404
    bad = c.post("/api/telegram/webhook/hook-secret", content=b"not json", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400
    assert c.post("/api/telegram/webhook/hook-secret", json=[1, 2]).status_code == 400
    assert c.post("/api/telegram/webhook/hook-secret", json={"message": {"chat": {"id": 1, "type": "group"}, "text": "LT-AAAA"}}).json()["result"] == "ignored"
    assert c.post("/api/telegram/webhook/hook-secret", json={"message": {"chat": {"id": 1, "type": "private"}, "text": "привет"}}).json()["result"] == "unknown"
    assert c.post("/api/telegram/webhook/hook-secret", json={"message": {"chat": {"id": 1, "type": "private"}, "text": "/start"}}).json()["result"] == "start"


def test_telegram_expired_code_shows_error(env, owner):
    owner.post("/api/telegram/connect")
    with env.app.state.session_factory() as db:
        link = db.scalar(select(TelegramLink))
        link.code_expires = utcnow().replace(year=2020)
        db.commit()
    assert owner.get("/api/telegram").json()["status"] == "error"          # как состояние «Ошибка» в демо
    code = owner.post("/api/telegram/connect").json()["code"]                # новый код — снова pending
    assert owner.get("/api/telegram").json()["status"] == "pending" and code


def test_telegram_needs_basic_plan_and_one_account_per_chat(env, api):
    api.signup()
    api.make_company("free", profile=False)
    assert api.post("/api/telegram/connect").status_code == 402


def test_telegram_chat_moves_between_users(env):
    one, two = env.api.clone(), env.api.clone()
    one.signup("one@example.org"); one.make_company("pro", profile=False)
    two.signup("two@example.org"); two.make_company("pro", profile=False)
    connect_telegram(env, one, chat_id=555)
    connect_telegram(env, two, chat_id=555)
    assert one.get("/api/telegram").json()["status"] == "disabled"
    assert two.get("/api/telegram").json()["status"] == "connected"


# ---------------- команда ----------------

def test_team_invite_join_and_roles(env, owner):
    guest = invite_and_join(env, owner, "g@example.org", "member")
    team = owner.get("/api/team").json()
    assert [m["role"] for m in team["members"]] == ["owner", "member"] and team["limit"] == 5
    assert guest.get("/api/company").json()["role"] == "member"
    mid = next(m["id"] for m in team["members"] if m["role"] == "member")
    assert owner.patch(f"/api/team/{mid}", {"role": "admin"}).status_code == 200
    assert guest.get("/api/company").json()["role"] == "admin"
    assert owner.patch(f"/api/team/{mid}", {"role": "owner"}).status_code == 422
    owner_id = team["members"][0]["id"]
    assert owner.patch(f"/api/team/{owner_id}", {"role": "viewer"}).status_code == 403        # владельца менять нельзя
    assert owner.delete(f"/api/team/{mid}").status_code == 200
    assert guest.get("/api/company").status_code == 409                                         # вышел из компании


def test_invite_rules(env, owner):
    assert owner.post("/api/team/invites", {"email": "bad", "role": "member"}).status_code == 422
    assert owner.post("/api/team/invites", {"email": "x@example.org", "role": "owner"}).status_code == 422
    assert owner.post("/api/team/invites", {"email": "x@example.org"}).status_code == 201
    assert owner.post("/api/team/invites", {"email": "X@example.org"}).status_code == 409       # повтор
    members = owner.get("/api/team").json()["members"]
    pending = next(m for m in members if m["status"] == "pending")
    old_code = env.mailer.outbox[-1]["body"].split("Код приглашения: ")[1].split()[0]
    assert owner.post(f"/api/team/{pending['id']}/resend").status_code == 200
    new_code = env.mailer.outbox[-1]["body"].split("Код приглашения: ")[1].split()[0]
    assert new_code != old_code
    late = owner.clone()
    late.signup("x@example.org")
    assert late.post("/api/company/join", {"code": old_code}).status_code == 404               # старый код недействителен
    other = owner.clone()
    other.signup("thief@example.org")
    r = other.post("/api/company/join", {"code": new_code})
    assert r.status_code == 403 and err(r)["code"] == "invite_wrong_email"                      # код чужого e-mail не годится
    assert late.post("/api/company/join", {"code": new_code}).status_code == 200


def test_plan_limits_users(env, api):
    api.signup()
    api.make_company("basic", profile=False)                    # на «Базовом» один пользователь
    r = api.post("/api/team/invites", {"email": "x@example.org"})
    assert r.status_code == 402 and err(r)["code"] == "plan_limit"


def test_member_and_admin_boundaries(env, owner):
    admin = invite_and_join(env, owner, "ad@example.org", "admin", "Админ")
    member = invite_and_join(env, owner, "me@example.org", "member", "Участник")
    assert member.post("/api/team/invites", {"email": "z@example.org"}).status_code == 403
    assert member.put("/api/company/profile/regions", {"data": {"regions": ["Юг"]}}).status_code == 403
    assert admin.put("/api/company/profile/regions", {"data": {"regions": ["Юг"]}}).status_code == 200
    assert admin.post("/api/team/invites", {"email": "z@example.org", "role": "admin"}).status_code == 403   # админов назначает владелец
    assert admin.post("/api/billing/request", {"plan": "corp"}).status_code == 403                           # тариф — только владелец
    team = owner.get("/api/team").json()["members"]
    admin_row = next(m for m in team if m["role"] == "admin")
    assert admin.delete(f"/api/team/{admin_row['id']}").status_code == 403              # администраторами управляет владелец


def test_cross_company_isolation(env, owner):
    other = owner.clone()
    other.signup("rival@example.org", name="Конкурент")
    other.post("/api/company", {"form": "ООО", "name": "Конкурент"})
    victim = owner.get("/api/team").json()["members"][0]["id"]
    assert other.delete(f"/api/team/{victim}").status_code == 404                               # чужой участник не виден
    assert other.patch(f"/api/team/{victim}", {"role": "viewer"}).status_code == 404
    assert other.put("/api/changes/consent/actions/0", {"state": "done"}).status_code == 402    # у «Старта» плана действий нет
    assert owner.get("/api/changes/consent").json()["actions"][0]["state"] == "todo"           # и чужие отметки не затронуты
    assert other.get("/api/company").json()["profile"]["name"] == "Конкурент"


def test_leave_company(env, owner):
    guest = invite_and_join(env, owner, "g@example.org")
    assert owner.post("/api/team/leave").status_code == 409
    assert guest.post("/api/team/leave").status_code == 200
    assert guest.get("/api/company").status_code == 409


# ---------------- тариф ----------------

def test_billing_and_admin_plan(env, api):
    api.signup()
    cid = api.make_company("free", profile=False)
    b = api.get("/api/billing").json()
    assert b["plan"]["id"] == "free" and b["payments_connected"] is False and b["usage"]["members"] == 1
    assert api.post("/api/billing/request", {"plan": "free"}).status_code == 422
    assert api.post("/api/billing/request", {"plan": "pro", "period": "y"}).status_code == 201
    assert api.get("/api/billing").json()["requests"][0]["status"] == "new"
    assert api.post("/api/billing/request", {"plan": "free"}).status_code == 422
    assert api.put(f"/api/admin/companies/{cid}/plan", {"plan": "pro"}).status_code == 404        # без токена метода «нет»
    assert api.put(f"/api/admin/companies/{cid}/plan", {"plan": "pro"}, headers={"X-Admin-Token": "bad"}).status_code == 404
    assert api.put(f"/api/admin/companies/{cid}/plan", {"plan": "pro"}, headers=ADMIN).status_code == 200
    assert api.get("/api/billing").json()["requests"][0]["status"] == "done"
    invite_and_join(env, api, "g@example.org")
    assert api.put(f"/api/admin/companies/{cid}/plan", {"plan": "basic"}, headers=ADMIN).status_code == 409   # 2 участника > 1


def test_plans_and_meta_public(env):
    anon = env.api.clone()
    plans = anon.get("/api/plans").json()
    assert [p["id"] for p in plans["plans"]] == ["free", "basic", "pro", "corp"] and plans["prices_final"] is False
    meta = anon.get("/api/meta").json()
    assert "Транспорт и логистика" in meta["industries"] and meta["steps"][0]["id"] == "basics" and "pd" in meta["topics"]
    assert anon.get("/api/healthz").json() == {"status": "ok"}


# ---------------- поддержка, аккаунт ----------------

def test_support_and_contact(owner, env):
    assert owner.post("/api/support", {"topic": "Другое", "message": "  "}).status_code == 422
    assert owner.post("/api/support", {"topic": "Неизвестная", "message": "x"}).status_code == 422
    assert owner.post("/api/support", {"topic": "Ошибка в материале", "message": "Опечатка"}).status_code == 201
    assert owner.get("/api/support").json()["tickets"][0]["message"] == "Опечатка"
    anon = env.api.clone()
    assert anon.post("/api/contact", {"name": "Иван", "email": "bad", "message": "x"}).status_code == 422
    for _ in range(5):
        assert anon.post("/api/contact", {"name": "Иван", "email": "i@example.org", "message": "Привет"}).status_code == 201
    assert anon.post("/api/contact", {"name": "Иван", "email": "i@example.org", "message": "Привет"}).status_code == 429
    tickets = owner.get("/api/admin/support", headers=ADMIN).json()["tickets"]
    assert len(tickets) >= 6
    assert owner.post("/api/plan/waitlist").status_code == 201 and owner.post("/api/plan/waitlist").status_code == 201


def test_account_profile_password_sessions(env, owner):
    assert owner.patch("/api/account", {"name": "  Анна   К. ", "phone": "+7 900 000-00-00"}).json()["user"]["name"] == "Анна К."
    second = owner.clone()
    assert second.post("/api/auth/login", {"email": "owner@example.org", "password": PASSWORD}).status_code == 200
    assert len(owner.get("/api/account/sessions").json()["sessions"]) == 2
    r = owner.post("/api/account/password", {"current": "wrong", "new": "NewPassw0rd"})
    assert r.status_code == 400 and "current" in err(r)["fields"]
    assert owner.post("/api/account/password", {"current": PASSWORD, "new": "weak"}).status_code == 422
    assert owner.post("/api/account/password", {"current": PASSWORD, "new": "NewPassw0rd"}).status_code == 200
    assert second.get("/api/auth/me").status_code == 401                     # остальные сессии завершены
    assert owner.get("/api/auth/me").status_code == 200
    third = owner.clone()
    third.post("/api/auth/login", {"email": "owner@example.org", "password": "NewPassw0rd"})
    sid = next(s["id"] for s in owner.get("/api/account/sessions").json()["sessions"] if not s["current"])
    assert owner.delete(f"/api/account/sessions/{sid}").status_code == 200
    assert third.get("/api/auth/me").status_code == 401
    cur = next(s["id"] for s in owner.get("/api/account/sessions").json()["sessions"] if s["current"])
    assert owner.delete(f"/api/account/sessions/{cur}").status_code == 404   # текущую завершают через выход


def test_delete_account(env, owner):
    guest = invite_and_join(env, owner, "g@example.org")
    assert owner.delete("/api/account", {"password": "wrong"}).status_code == 400
    assert owner.delete("/api/account", {"password": PASSWORD}).status_code == 409        # в компании есть другие участники
    assert guest.delete("/api/account", {"password": PASSWORD}).status_code == 200
    assert owner.delete("/api/account", {"password": PASSWORD}).status_code == 200        # один владелец — компания удаляется
    with env.app.state.session_factory() as db:
        assert db.scalar(select(User)) is None and db.scalar(select(Company)) is None and db.scalar(select(Member)) is None
    assert env.api.clone().post("/api/auth/login", {"email": "owner@example.org", "password": PASSWORD}).status_code == 401


# ---------------- рассылка ----------------

def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def run(env, now):
    with env.app.state.session_factory() as db:
        return dispatch(db, env.settings, env.mailer, env.tg, now)


def digests(env):
    return [m for m in env.mailer.outbox if m["subject"].startswith("Дайджест")]


def urgent(env):
    return [m for m in env.mailer.outbox if m["subject"].startswith("Срочное")]


def test_daily_digest_schedule_and_dedupe(env, owner):
    # 20.10.2026 — вторник; 09:00 по Москве = 06:00 UTC
    run(env, utc(2026, 10, 20, 5, 30))
    assert digests(env) == []                                                      # до 09:00 плановый дайджест не уходит
    first = urgent(env)                                                            # но срочное (высокое влияние) — вне расписания
    assert len(first) == 1 and "Согласие на обработку" in first[0]["body"] and "дистанционной" not in first[0]["body"]
    run(env, utc(2026, 10, 20, 6, 5))
    d = digests(env)
    assert d[-1]["subject"] == "Дайджест LawTracker" and d[-1]["to"] == "a.kovaleva@nordwood.ru"
    assert "Новые правила оформления дистанционной работы" in d[-1]["body"]
    count = len(env.mailer.outbox)
    run(env, utc(2026, 10, 20, 12, 0))
    assert len(env.mailer.outbox) == count                                          # повторно то же не отправляется


def test_digest_respects_weekdays_pause_and_topics(env, owner):
    prefs = owner.get("/api/notifications").json()["settings"]
    owner.put("/api/notifications", {**prefs, "days": [6, 7], "min_impact": "low"})
    run(env, utc(2026, 10, 20, 8))                                                  # вторник, не выбран
    assert digests(env) == []
    owner.put("/api/notifications", {**prefs, "days": [1, 2, 3, 4, 5], "pause": True, "pause_until": "2026-10-25"})
    n = len(env.mailer.outbox)
    stats = run(env, utc(2026, 10, 20, 8))
    assert len(env.mailer.outbox) == n and stats["skipped_paused"] == 1
    owner.put("/api/notifications", {**prefs, "pause": True, "pause_until": "2026-10-19"})  # пауза закончилась
    run(env, utc(2026, 10, 20, 8))
    assert len(env.mailer.outbox) > n


def test_digest_topic_and_impact_filters(env, owner):
    prefs = owner.get("/api/notifications").json()["settings"]
    owner.put("/api/notifications", {**prefs, "topics": ["tax"], "min_impact": "low", "email_freq": "now"})
    run(env, utc(2026, 10, 20, 9))
    body = digests(env)[-1]["body"]
    assert "Дополнительная форма отчётности" in body and "Согласие на обработку" not in body


def test_telegram_gets_digest_when_connected(env, owner):
    connect_telegram(env, owner)
    prefs = owner.get("/api/notifications").json()["settings"]
    owner.put("/api/notifications", {**prefs, "email": False})
    env.tg.sent.clear()
    stats = run(env, utc(2026, 10, 20, 7))
    assert stats["telegram"] == 1 and stats["email"] == 0
    chat, text = env.tg.sent[-1]
    assert chat == "777" and "Дайджест LawTracker" in text and "lt.example" in text
    owner.put("/api/notifications", {**prefs, "email": False, "tg": False})
    env.tg.sent.clear()
    run(env, utc(2026, 10, 21, 7))
    assert env.tg.sent == []                                                        # канал выключен


def test_unverified_email_gets_nothing(env, api):
    api.signup("u@example.org", verify=False)
    api.post("/api/company", {"form": "ООО", "name": "X"})                           # 403: почта не подтверждена
    assert run(env, utc(2026, 10, 20, 7)) == {"email": 0, "telegram": 0, "skipped_paused": 0}


def test_weekly_digest_gap(env, owner):
    prefs = owner.get("/api/notifications").json()["settings"]
    owner.put("/api/notifications", {**prefs, "email_freq": "weekly", "days": [2], "min_impact": "low"})
    run(env, utc(2026, 10, 20, 7))
    sent = digests(env)
    assert len(sent) == 1
    body = {"topic": "tax", "impact": "low", "published_at": "2026-10-19", "title": "Новое налоговое изменение",
            "reasons": [{"when": {"field": "form", "op": "not_empty"}, "text": "Налоги"}]}
    owner.put("/api/admin/changes/new-tax", body, headers=ADMIN)
    run(env, utc(2026, 10, 27, 7))
    sent2 = digests(env)
    assert len(sent2) == 2 and "Новое налоговое изменение" in sent2[-1]["body"]


def test_read_changes_are_not_sent(env, owner):
    for cid in ("pd-local", "consent", "remote-work", "foreign-report", "cargo", "hr-docs", "ads-mark", "small-tax"):
        owner.post(f"/api/changes/{cid}/read")
    prefs = owner.get("/api/notifications").json()["settings"]
    owner.put("/api/notifications", {**prefs, "email_freq": "now", "min_impact": "low"})
    run(env, utc(2026, 10, 20, 9))
    assert digests(env) == [] and urgent(env) == []


# ---------------- подключение сайта ----------------

def test_list_with_detail_includes_cards(owner):
    items = owner.get("/api/changes", params={"per_page": 50, "detail": True}).json()["items"]
    consent = next(i for i in items if i["id"] == "consent")
    assert consent["actions"][0]["title"] and consent["quote"] and {r["id"] for r in consent["related"]} == {"pd-local", "ads-mark"}
    plain = owner.get("/api/changes", params={"per_page": 50}).json()["items"][0]
    assert "actions" not in plain and "quote" not in plain


def test_meta_today_and_demo_flags(env):
    meta = env.api.clone().get("/api/meta").json()
    assert meta["today"] == "2026-10-20" and meta["demo_mode"] is False and "steps" in meta
    assert any("contact_email" in s["fields"] for s in meta["steps"])


def test_profile_required_fields_match_site(api):
    api.signup()
    api.make_company("pro", profile=False)
    # как на сайте: для «Контактов» обязательна только почта, для «Структуры» — ответ про филиалы
    assert api.put("/api/company/profile/contacts", {"data": {"contact_email": "a@b.ru"}}).status_code == 200
    r = api.put("/api/company/profile/structure", {"data": {"foreign_share": True}})
    assert r.status_code == 422 and "branches" in r.json()["error"]["fields"]


def test_demo_plan_switch_only_in_demo_mode(env, owner):
    assert owner.put("/api/demo/plan", {"plan": "basic"}).status_code == 404
    from dataclasses import replace

    from app.main import create_app
    from app.mailer import ConsoleMailer
    from app.telegram import FakeSender
    from tests.conftest import Api, TODAY
    settings = replace(env.settings, demo_mode=True)
    app = create_app(settings, mailer=ConsoleMailer(), telegram=FakeSender(), today=lambda: TODAY)
    from seed.load import load_demo_changes
    with app.state.session_factory() as db:
        load_demo_changes(db, TODAY)
    a = Api(app, app.state.mailer, app.state.telegram)
    r = a.post("/api/auth/register", {"name": "Анна", "email": "demo@example.org", "password": "Passw0rd!", "consent": True})
    code = r.json()["dev_code"]                                     # без почты код возвращается в ответе
    assert a.post("/api/auth/verify-email", {"code": code}).status_code == 200
    a.post("/api/company", {"form": "ООО", "name": "Демо"})
    assert a.put("/api/demo/plan", {"plan": "pro"}).json() == {"plan": "pro"}
    assert a.get("/api/billing").json()["plan"]["id"] == "pro"
    a.put("/api/company/profile/industry", {"data": {"industry": "Медицина", "topics": ["pd", "tax", "labor"]}})
    assert a.put("/api/demo/plan", {"plan": "free"}).status_code == 409        # выбрано 3 сферы, на «Старте» можно 1
    assert a.put("/api/demo/plan", {"plan": "basic"}).status_code == 200       # на «Базовом» лимит как раз 3
