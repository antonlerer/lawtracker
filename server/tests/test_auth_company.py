from datetime import timedelta

from sqlalchemy import select

from app.models import AuthSession, utcnow
from tests.conftest import DEMO_PROFILE, PASSWORD


def err(r):
    return r.json()["error"]


def test_register_validation(api):
    r = api.post("/api/auth/register", {"name": "A", "email": "bad", "password": "short", "consent": False})
    assert r.status_code == 422
    assert set(err(r)["fields"]) == {"email", "password", "consent"}


def test_register_login_logout_me(api):
    assert api.get("/api/auth/me").status_code == 401
    api.signup("a@example.org")
    me = api.get("/api/auth/me").json()
    assert me["user"]["email"] == "a@example.org" and me["user"]["email_verified"] and me["company"] is None
    assert api.post("/api/auth/logout").status_code == 200
    assert api.get("/api/auth/me").status_code == 401
    assert api.post("/api/auth/login", {"email": "A@Example.org ", "password": PASSWORD}).status_code == 200
    assert api.get("/api/auth/me").status_code == 200


def test_duplicate_email_and_wrong_password_same_message_shape(api):
    api.signup("a@example.org")
    other = api.clone()
    r = other.post("/api/auth/register", {"name": "B", "email": "a@example.org", "password": PASSWORD, "consent": True})
    assert r.status_code == 409 and err(r)["code"] == "email_taken"
    bad = other.post("/api/auth/login", {"email": "a@example.org", "password": "Wrong123!"})
    ghost = other.post("/api/auth/login", {"email": "nobody@example.org", "password": "Wrong123!"})
    assert bad.status_code == ghost.status_code == 401 and bad.json() == ghost.json()   # нельзя узнать, есть ли email


def test_login_throttle(api):
    api.signup("a@example.org")
    other = api.clone()
    for _ in range(8):
        assert other.post("/api/auth/login", {"email": "a@example.org", "password": "Wrong123!"}).status_code == 401
    r = other.post("/api/auth/login", {"email": "a@example.org", "password": PASSWORD})
    assert r.status_code == 429                                 # даже верный пароль после блокировки


def test_cookie_flags_and_session_stored_hashed(env):
    api = env.api
    r = api.post("/api/auth/register", {"name": "A", "email": "a@example.org", "password": PASSWORD, "consent": True})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    token = api.client.cookies.get("lt_session")
    with env.app.state.session_factory() as db:
        s = db.scalar(select(AuthSession))
        assert s.token_hash != token and len(s.token_hash) == 64     # в БД только хеш


def test_session_expiry(env):
    api = env.api
    api.signup("a@example.org")
    with env.app.state.session_factory() as db:
        s = db.scalar(select(AuthSession))
        s.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    assert api.get("/api/auth/me").status_code == 401


def test_csrf_guard_blocks_form_posts(env):
    api = env.api
    api.signup("a@example.org")
    r = api.client.post("/api/auth/logout", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403 and err(r)["code"] == "csrf"
    assert api.get("/api/auth/me").status_code == 200           # сессия цела


def test_email_verification(api):
    api.signup("a@example.org", verify=False)
    assert api.post("/api/company", {"form": "ООО", "name": "X"}).status_code == 403       # нужна подтверждённая почта
    assert api.post("/api/auth/verify-email", {"code": "000000"}).status_code == 400
    code = api.last_code("a@example.org")
    assert api.post("/api/auth/verify-email", {"code": code}).status_code == 200
    assert api.post("/api/company", {"form": "ООО", "name": "X"}).status_code == 201


def test_verification_code_attempt_limit(api):
    api.signup("a@example.org", verify=False)
    code = api.last_code("a@example.org")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        api.post("/api/auth/verify-email", {"code": wrong})
    r = api.post("/api/auth/verify-email", {"code": code})      # после 5 ошибок верный код тоже не принимается
    assert r.status_code == 400 and err(r)["code"] == "code_expired"
    api.post("/api/auth/resend-code")
    assert api.post("/api/auth/verify-email", {"code": api.last_code("a@example.org")}).status_code == 200


def test_password_reset_flow(env):
    api = env.api
    api.signup("a@example.org")
    api.post("/api/auth/logout")
    r1 = api.post("/api/auth/forgot", {"email": "a@example.org"})
    r2 = api.post("/api/auth/forgot", {"email": "ghost@example.org"})
    assert r1.json() == r2.json()                                # ответ одинаковый
    assert [m["to"] for m in env.mailer.outbox if "Сброс" in m["subject"]] == ["a@example.org"]
    token = env.mailer.outbox[-1]["body"].split("token=")[1].split()[0]
    assert api.post("/api/auth/reset", {"token": token, "password": "weak"}).status_code == 422
    assert api.post("/api/auth/reset", {"token": token, "password": "NewPassw0rd"}).status_code == 200
    assert api.post("/api/auth/reset", {"token": token, "password": "NewPassw0rd"}).status_code == 400   # одноразовый
    assert api.post("/api/auth/login", {"email": "a@example.org", "password": PASSWORD}).status_code == 401
    assert api.post("/api/auth/login", {"email": "a@example.org", "password": "NewPassw0rd"}).status_code == 200


def test_company_create_and_profile_steps(api):
    api.signup()
    assert api.get("/api/company").status_code == 409            # компании ещё нет
    r = api.post("/api/company", {"form": "ООО", "name": "  Нордвуд   Логистика "})
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "owner" and body["plan"] == "free" and body["profile"]["name"] == "Нордвуд Логистика"
    assert body["completeness"]["percent"] == 0 and not body["completeness"]["started"]
    assert api.post("/api/company", {"form": "ООО", "name": "Ещё"}).status_code == 409


def test_step_validation_errors(api):
    api.signup()
    api.make_company("pro", profile=False)
    cases = [
        ("basics", {"name": "X", "form": "ООО", "size": "small", "inn": "123"}, "inn"),
        ("basics", {"name": "X", "form": "Фирма", "size": "small"}, "form"),
        ("basics", {"name": "X", "form": "ООО", "size": "huge"}, "size"),
        ("basics", {"form": "ООО", "size": "small"}, "name"),                    # обязательное поле
        ("industry", {"industry": "Космос"}, "industry"),
        ("industry", {"industry": "Медицина", "activities": ["Полёты"]}, "activities"),
        ("industry", {"industry": "Медицина", "topics": ["magic"]}, "topics"),
        ("regions", {"regions": []}, "regions"),
        ("structure", {"entities": 0}, "entities"),
        ("staff", {"headcount": "много"}, "headcount"),
        ("contacts", {"contact_name": "А", "contact_email": "не-почта"}, "contact_email"),
        ("contacts", {"contact_name": "А", "contact_email": "a@b.ru", "recipients": ["bad"]}, "recipients"),
        ("pd", {"pd_customers": True, "unknown": 1}, "unknown"),
        ("basics", {"name": "X", "form": "ООО", "size": "small", "industry": "Медицина"}, "industry"),  # чужое поле шага
    ]
    for step, data, field in cases:
        r = api.put(f"/api/company/profile/{step}", {"data": data})
        assert r.status_code == 422, (step, data, r.text)
        assert field in err(r)["fields"], (step, data, r.text)
    assert api.put("/api/company/profile/nope", {"data": {}}).status_code == 404


def test_profile_completeness_and_skip(api):
    api.signup()
    api.make_company("pro", profile=False)
    api.put("/api/company/profile/basics", {"data": DEMO_PROFILE["basics"]})
    c = api.get("/api/company").json()
    assert c["completeness"]["percent"] == 10 and c["completeness"]["started"] and c["completeness"]["next_step"] == "industry"
    assert api.post("/api/company/profile/basics/skip").status_code == 422        # обязательный
    r = api.post("/api/company/profile/licenses/skip")
    assert r.status_code == 200 and "licenses" in r.json()["completeness"]["done"]
    for step, data in DEMO_PROFILE.items():
        api.put(f"/api/company/profile/{step}", {"data": data})
    api.post("/api/company/profile/ved/skip")
    assert api.get("/api/company").json()["completeness"]["percent"] == 100
    r = api.delete("/api/company/profile")                      # «Очистить профиль»
    assert r.json()["completeness"]["percent"] == 0 and r.json()["profile"]["name"] == "Нордвуд Логистика"


def test_profile_is_structured_not_tags(api):
    api.signup()
    api.make_company("pro")
    profile = api.get("/api/company").json()["profile"]
    assert profile["industry"] == "Транспорт и логистика" and profile["entities"] == 2
    assert profile["pd_employees"] is True and profile["headcount"] == "16–100"
    assert "tags" not in profile and "tags_json" not in profile


def test_free_plan_limits_areas(api):
    api.signup()
    api.make_company("free", profile=False)
    r = api.put("/api/company/profile/industry", {"data": {"industry": "Медицина", "topics": ["pd", "tax"]}})
    assert r.status_code == 402 and err(r)["code"] == "plan_limit"
    assert api.put("/api/company/profile/industry", {"data": {"industry": "Медицина", "topics": ["pd"]}}).status_code == 200


def test_viewer_cannot_edit_profile_but_can_read(env, owner):
    cid_owner = owner.get("/api/company").json()["id"]
    owner.post("/api/team/invites", {"email": "v@example.org", "role": "viewer"})
    code = env.mailer.outbox[-1]["body"].split("Код приглашения: ")[1].split()[0]
    viewer = owner.clone()
    viewer.signup("v@example.org", name="Виктор")
    assert viewer.post("/api/company/join", {"code": code}).status_code == 200
    assert viewer.get("/api/company").json()["id"] == cid_owner
    assert viewer.put("/api/company/profile/regions", {"data": {"regions": ["Юг"]}}).status_code == 403
    assert viewer.delete("/api/company/profile").status_code == 403
