from tests.conftest import ADMIN

PERSONAL = {"pd-local", "consent", "foreign-report", "cargo", "remote-work", "hr-docs", "ads-mark", "small-tax"}
ATTENTION = {"pd-local", "consent", "remote-work"}


def ids(r):
    return {i["id"] for i in r.json()["items"]}


def listing(api, **params):
    return api.get("/api/changes", params={"per_page": 50, **params}).json()


def test_personalisation_follows_profile_fields(owner):
    items = listing(owner, personal=True)["items"]
    assert {i["id"] for i in items} == PERSONAL
    by_id = {i["id"]: i for i in items}
    # причина — текст из правила, подставленные значения профиля
    assert "В профиле указано: компания обрабатывает персональные данные сотрудников" in by_id["pd-local"]["reasons"]
    assert "Указана численность сотрудников: 16–100" in by_id["hr-docs"]["reasons"]
    assert "Размер компании: малое предприятие" in by_id["small-tax"]["reasons"]
    assert "ved" not in by_id and "med-lic" not in by_id


def test_changing_profile_changes_matches(owner):
    owner.put("/api/company/profile/ved", {"data": {"ved_export": True}})
    assert "ved" in {i["id"] for i in listing(owner, personal=True)["items"]}
    owner.put("/api/company/profile/pd", {"data": {"pd_customers": False, "pd_employees": False, "pd_external": False}})
    items = {i["id"]: i for i in listing(owner, personal=True)["items"]}
    # флаги про данные сняты, но облачное хранилище в профиле ещё остаётся причиной для pd-local
    assert items["pd-local"]["reasons"] == ["Компания использует облачные хранилища"]
    owner.put("/api/company/profile/digital", {"data": {"digital": []}})
    got = {i["id"] for i in listing(owner, personal=True)["items"]}
    assert "pd-local" not in got and "consent" not in got


def test_region_scoped_change(owner, env):
    body = {"topic": "log", "impact": "med", "published_at": "2026-10-15", "title": "Региональная норма",
            "regions": ["Сибирь"], "reasons": [{"when": {"field": "industry", "op": "eq", "value": "Транспорт и логистика"},
                                                "text": "Отрасль: логистика"}]}
    assert owner.put("/api/admin/changes/reg-1", body, headers=ADMIN).status_code == 200
    assert "reg-1" not in {i["id"] for i in listing(owner, personal=True)["items"]}      # компания работает не в Сибири
    owner.put("/api/company/profile/regions", {"data": {"regions": ["Сибирь"]}})
    assert "reg-1" in {i["id"] for i in listing(owner, personal=True)["items"]}
    owner.put("/api/company/profile/regions", {"data": {"regions": ["Вся Россия"]}})
    assert "reg-1" in {i["id"] for i in listing(owner, personal=True)["items"]}


def test_empty_profile_means_no_personalisation(api):
    api.signup()
    api.make_company("pro", profile=False)
    data = listing(api, personal=True)
    assert data["total"] == 0
    ov = api.get("/api/overview").json()
    assert ov["profile"]["started"] is False and ov["stats"]["attention"] == 0


def test_free_plan_gets_general_digest_only(api):
    api.signup()
    api.make_company("free", profile=False)
    api.put("/api/company/profile/basics", {"data": {"name": "Н", "form": "ООО", "size": "small"}})
    assert listing(api, personal=True)["total"] == 0                # персонализация — с тарифа «Базовый»
    assert listing(api)["total"] > 0                                 # общий дайджест виден, но в пределах архива


def test_basic_plan_personal_but_no_action_plan(api):
    api.signup()
    api.make_company("basic")
    assert {i["id"] for i in listing(api, personal=True)["items"]} == PERSONAL
    d = api.get("/api/changes/pd-local").json()
    assert d["actions"] == [] and d["actions_locked"] is True
    assert api.put("/api/changes/pd-local/actions/0", {"state": "done"}).status_code == 402


def test_filters_sort_search_pagination(owner):
    assert listing(owner)["total"] == 11
    assert {i["id"] for i in listing(owner, topic="pd")["items"]} == {"pd-local", "consent"}
    assert {i["id"] for i in listing(owner, impact="high")["items"]} == {"pd-local", "consent", "med-lic"}
    assert {i["id"] for i in listing(owner, period="7")["items"]} == {"hr-docs", "small-tax", "ai-draft", "remote-work"}
    assert {i["id"] for i in listing(owner, q="согласие")["items"]} == {"consent"}
    assert {i["id"] for i in listing(owner, q="налог")["items"]} >= {"small-tax", "foreign-report"}  # поиск и по тематике
    by_date = [i["id"] for i in listing(owner)["items"]]
    assert by_date[0] == "ai-draft" and by_date[-1] == "med-lic"          # свежие сверху
    by_impact = listing(owner, sort="impact")["items"]
    assert [i["impact"] for i in by_impact] == sorted([i["impact"] for i in by_impact], key=lambda x: -{"high": 3, "med": 2, "low": 1}[x])
    by_eff = [i["effective_at"] for i in listing(owner, sort="eff")["items"]]
    assert by_eff[-1] is None and by_eff[:-1] == sorted(by_eff[:-1])      # без даты — в конце
    p1 = owner.get("/api/changes", params={"per_page": 5, "page": 1}).json()
    p3 = owner.get("/api/changes", params={"per_page": 5, "page": 9}).json()   # выход за пределы — последняя страница
    assert p1["pages"] == 3 and len(p1["items"]) == 5 and p3["page"] == 3 and len(p3["items"]) == 1
    assert owner.get("/api/changes", params={"topic": "magic"}).json()["total"] == 0
    assert owner.get("/api/changes", params={"impact": "extreme"}).status_code == 422
    assert owner.get("/api/changes", params={"per_page": 1000}).status_code == 422


def test_attention_matches_demo_rule(owner):
    assert {i["id"] for i in listing(owner, status="attn")["items"]} == ATTENTION
    ov = owner.get("/api/overview").json()
    assert ov["stats"]["attention"] == 3 and ov["stats"]["unread"] == 11
    # выполнили все действия по изменению — оно выходит из «требует внимания»
    n = len(owner.get("/api/changes/remote-work").json()["actions"])
    for i in range(n):
        assert owner.put(f"/api/changes/remote-work/actions/{i}", {"state": "done"}).status_code == 200
    assert {i["id"] for i in listing(owner, status="attn")["items"]} == ATTENTION - {"remote-work"}
    owner.put("/api/changes/remote-work/actions/0", {"state": "todo"})
    assert "remote-work" in {i["id"] for i in listing(owner, status="attn")["items"]}


def test_overview_blocks(owner):
    ov = owner.get("/api/overview").json()
    assert ov["greeting_name"] == "Анна" and ov["company_name"] == "Нордвуд Логистика"
    assert [c["id"] for c in ov["attention"]] == ["consent", "pd-local", "remote-work"]
    assert [c["id"] for c in ov["upcoming"]] == ["consent", "remote-work", "hr-docs"]     # ближайшие вступающие
    assert ov["stats"]["soon30"] == 2                                                      # consent (14), remote-work (30)
    assert ov["profile"]["percent"] == 80 and ov["profile"]["next_step"] == "licenses"
    assert all(c["id"] not in {a["id"] for a in ov["attention"]} for c in ov["fresh"])
    assert len(ov["fresh"]) <= 4 and ov["telegram"] == "none"


def test_read_and_saved_are_per_user(env, owner):
    assert owner.post("/api/changes/consent/read").json()["is_read"] is True
    assert owner.post("/api/changes/consent/save").json()["is_saved"] is True
    assert {i["id"] for i in listing(owner, status="read")["items"]} == {"consent"}
    assert {i["id"] for i in listing(owner, status="saved")["items"]} == {"consent"}
    assert owner.get("/api/overview").json()["stats"]["unread"] == 10
    assert owner.post("/api/changes/consent/read", {"read": False}).json()["is_read"] is False
    other = owner.clone()
    other.signup("b@example.org", name="Борис")
    assert listing(other, status="saved")["total"] == 0 and other.get("/api/saved").json()["total"] == 0
    assert owner.get("/api/saved").json()["total"] == 1
    assert owner.post("/api/changes/ghost/read").status_code == 404


def test_detail_card(owner):
    d = owner.get("/api/changes/consent").json()
    assert d["days_left"] == 14 and d["status"] == "upcoming" and d["attention"] is True
    assert [t["key"] for t in d["timeline"]] == ["published", "today", "effective"]
    assert d["timeline"][2]["done"] is False and d["timeline"][0]["date"] == "2026-09-30"
    assert [a["title"] for a in d["actions"]][0] == "Оформить согласие отдельно от оферты"
    assert d["actions"][0]["due"] == "2026-10-30" and d["actions"][0]["due_in_days"] == 10 and d["actions"][0]["state"] == "todo"
    assert {r["id"] for r in d["related"]} == {"pd-local", "ads-mark"}
    assert "Отрасль: Транспорт и логистика" in d["profile_basis"]
    assert d["confidence"] == "high" and d["reviewed"] is True and d["quote"].startswith("«")
    draft = owner.get("/api/changes/ai-draft").json()
    assert draft["is_draft"] and draft["status"] == "draft" and draft["effective_at"] is None
    assert [t["key"] for t in draft["timeline"]] == ["published", "today"]
    assert owner.get("/api/changes/nope").status_code == 404


def test_action_progress_is_shared_by_company(env, owner):
    owner.put("/api/changes/consent/actions/0", {"state": "done"})
    owner.post("/api/team/invites", {"email": "m@example.org", "role": "member"})
    code = env.mailer.outbox[-1]["body"].split("Код приглашения: ")[1].split()[0]
    member = owner.clone()
    member.signup("m@example.org", name="Мария")
    member.post("/api/company/join", {"code": code})
    assert member.get("/api/changes/consent").json()["actions"][0]["state"] == "done"      # общий прогресс компании
    assert member.put("/api/changes/consent/actions/1", {"state": "done"}).status_code == 200
    assert owner.put("/api/changes/consent/actions/9", {"state": "done"}).status_code == 404
    assert owner.put("/api/changes/consent/actions/0", {"state": "maybe"}).status_code == 422


def test_archive_limit_on_lower_plans(api, env):
    api.signup()
    api.make_company("basic")
    body = {"topic": "tax", "impact": "low", "published_at": "2026-05-01", "title": "Старое изменение"}
    api.put("/api/admin/changes/old-1", body, headers=ADMIN)
    assert api.get("/api/changes/old-1").status_code == 404             # архив старше 90 дней — с тарифа «Профессиональный»
    cid = api.get("/api/company").json()["id"]
    api.put(f"/api/admin/companies/{cid}/plan", {"plan": "pro"}, headers=ADMIN)
    assert api.get("/api/changes/old-1").status_code == 200


def test_hidden_changes_not_visible(owner):
    body = {"topic": "tax", "impact": "low", "published_at": "2026-10-10", "title": "Скрытое", "status": "hidden"}
    owner.put("/api/admin/changes/hid-1", body, headers=ADMIN)
    assert owner.get("/api/changes/hid-1").status_code == 404
    assert "hid-1" not in ids(owner.get("/api/changes", params={"per_page": 50}))


def test_requires_unauth(env):
    anon = env.api.clone()
    for url in ("/api/changes", "/api/overview", "/api/saved", "/api/changes/consent", "/api/notifications",
                "/api/telegram", "/api/team", "/api/billing", "/api/company"):
        assert anon.get(url).status_code == 401, url
