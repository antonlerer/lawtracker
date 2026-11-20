import json

import httpx
import pytest
from sqlalchemy import select

from app.models import Change
from ingest.enrich import classify_by_rules, enrich, impact_by_type
from ingest.gigachat import GigaChat
from ingest.pravo import RawAct, parse_document, parse_rss
from ingest.run import run_ingest
from seed.load import load_demo_changes
from tests.conftest import ADMIN, TODAY

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>t1</title><description>Номер опубликования: 0001202610150001</description></item>
<item><title>t2</title><description>Номер опубликования: 0001202610150002</description></item>
<item><title>dup</title><description>Номер опубликования: 0001202610150001</description></item>
<item><title>no number</title><description>без номера</description></item></channel></rss>"""

DOCS = {
    "0001202610150001": {"name": "О внесении изменений в Федеральный закон «О персональных данных»",
                         "documentType": {"name": "Федеральный закон"}, "documentDate": "2026-10-15T00:00:00",
                         "number": "301-ФЗ"},
    "0001202610150002": {"name": "О награждении государственными наградами Российской Федерации",
                         "documentType": {"name": "Указ"}, "documentDate": "2026-10-15", "number": "5"},
}


def raw(title, type_="Федеральный закон"):
    return RawAct("1", type_, "2026-10-15", "1-ФЗ", title, "http://publication.pravo.gov.ru/document/1")


# ---------- разбор источника ----------

def test_parse_rss_and_document():
    assert parse_rss(RSS) == ["0001202610150001", "0001202610150002"]
    act = parse_document("1", DOCS["0001202610150001"])
    assert act.type == "Федеральный закон" and act.date == "2026-10-15" and act.document_url.endswith("/document/1")
    assert parse_document("1", {"name": "  "}) is None


# ---------- классификация без модели ----------

@pytest.mark.parametrize("title,topic", [
    ("О внесении изменений в Федеральный закон «О персональных данных»", "pd"),
    ("О внесении изменений в статью 217 Налогового кодекса Российской Федерации", "tax"),
    ("Об особенностях трудовой деятельности удалённых работников", "labor"),
    ("Об утверждении правил маркировки молочной продукции", "ads"),
    ("О лицензировании медицинской деятельности", "lic"),
])
def test_rule_classifier(title, topic):
    assert classify_by_rules(title) == topic


def test_classifier_has_no_substring_false_positives():
    # прежний словарь находил «ии» внутри «Федерации», «жд» в «Министерства...» и т. п.
    assert classify_by_rules("О награждении государственными наградами Российской Федерации") is None
    assert classify_by_rules("О назначении Иванова И.И. заместителем Министра финансов Российской Федерации") is None
    assert classify_by_rules("Об утверждении Положения о Федерации") is None


def test_enrich_skips_noise_and_marks_unreviewed():
    assert enrich(raw("О награждении государственными наградами Российской Федерации", "Указ")) is None
    assert enrich(raw("О ратификации Соглашения о сотрудничестве в области налогов")) is None
    f = enrich(raw("О внесении изменений в Федеральный закон «О персональных данных»"))
    assert f["topic"] == "pd" and f["impact"] == "high" and f["reviewed"] is False and f["confidence"] == "low"
    assert f["what"] == "" and f["who"] == "" and f["actions"] == []                 # содержание не выдумывается
    assert f["summary"] == f["title"] and f["source_key"] == "1"
    assert {r["text"] for r in f["reasons"]} >= {"Компания собирает персональные данные клиентов"}
    assert impact_by_type("Приказ") == "low" and impact_by_type("Постановление") == "med"


# ---------- модель (подставной клиент) ----------

class FakeLLM:
    def __init__(self, answer):
        self.answer = answer

    def complete_json(self, system, user):
        assert "Название:" in user and "digital" in system        # промпт содержит разрешённые поля
        return self.answer


GOOD = {"relevant": True, "topic": "ecom", "impact": "med", "summary": "Ограничения для маркетплейсов.",
        "reasons": [{"when": {"field": "digital", "op": "contains", "value": "Маркетплейсы"},
                     "text": "Продажи через маркетплейсы"}]}


def test_llm_result_used_when_valid():
    f = enrich(raw("Об особенностях торговли на маркетплейсах"), FakeLLM(GOOD))
    assert f["topic"] == "ecom" and f["confidence"] == "mid" and f["summary"] == "Ограничения для маркетплейсов."
    assert f["reasons"][0]["when"]["field"] == "digital" and f["reviewed"] is False


@pytest.mark.parametrize("answer", [
    None,
    {"relevant": True, "topic": "magic", "impact": "high", "reasons": []},
    {"relevant": True, "topic": "pd", "impact": "high", "reasons": []},
    {"relevant": True, "topic": "pd", "impact": "high",
     "reasons": [{"when": {"field": "__import__('os')", "op": "eq", "value": 1}, "text": "x"}]},     # недопустимое поле
    {"relevant": True, "topic": "pd", "impact": "huge", "reasons": [GOOD["reasons"][0]]},
])
def test_llm_garbage_falls_back_to_rules(answer):
    f = enrich(raw("О внесении изменений в Федеральный закон «О персональных данных»"), FakeLLM(answer))
    assert f["topic"] == "pd" and f["confidence"] == "low"


def test_llm_can_reject_irrelevant():
    assert enrich(raw("О внесении изменений в Налоговый кодекс"), FakeLLM({"relevant": False})) is None


def test_gigachat_client_parses_json_and_survives_errors():
    def handler(request):
        if "oauth" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"choices": [{"message": {"content": 'Вот ответ: {"relevant": false}'}}]})

    giga = GigaChat("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert giga.complete_json("s", "u") == {"relevant": False}
    broken = GigaChat("key", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert broken.complete_json("s", "u") is None


# ---------- проход загрузки ----------

def mock_portal():
    def handler(request):
        url = str(request.url)
        if url.endswith("/api/Rss"):
            return httpx.Response(200, text=RSS)
        eo = request.url.params.get("eoNumber")
        return httpx.Response(200, json=DOCS[eo]) if eo in DOCS else httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_run_ingest_adds_relevant_and_is_idempotent(env):
    with env.app.state.session_factory() as db:
        stats = run_ingest(db, env.settings, http=mock_portal())
        assert stats == {"seen": 2, "new": 2, "added": 1, "skipped": 1, "errors": 0, "analyzed": 0, "fallback": 1}
        ch = db.get(Change, "pravo-0001202610150001")
        assert ch.topic == "pd" and ch.published_at.isoformat() == "2026-10-15" and not ch.reviewed
        assert ch.source_url.endswith("/document/0001202610150001")
        again = run_ingest(db, env.settings, http=mock_portal())
        assert again["added"] == 0                                           # дубликатов нет
        assert db.scalar(select(Change).where(Change.id == "pravo-0001202610150002")) is None


def test_ingested_change_reaches_matching_clients(env, owner):
    with env.app.state.session_factory() as db:
        run_ingest(db, env.settings, http=mock_portal())
    item = owner.get("/api/changes/pravo-0001202610150001").json()
    assert item["personal"] and item["confidence"] == "low" and item["reviewed"] is False


# ---------- админские методы и сид ----------

BASE = {"topic": "tax", "impact": "med", "published_at": "2026-10-10", "title": "Тест"}


def test_admin_upsert_validation(owner):
    ok = {**BASE, "reasons": [{"when": {"field": "form", "op": "eq", "value": "ООО"}, "text": "Форма: ООО"}],
          "actions": [{"title": "Сделать", "due": "2026-12-01", "responsible": "Бухгалтер"}]}
    assert owner.put("/api/admin/changes/t-1", ok, headers=ADMIN).status_code == 200
    assert owner.put("/api/admin/changes/t-1", {**ok, "title": "Новое имя"}, headers=ADMIN).status_code == 200
    assert owner.get("/api/changes/t-1").json()["title"] == "Новое имя"
    for bad in [{"topic": "magic"}, {"regions": ["Марс"]}, {"actions": [{"title": ""}]},
                {"actions": [{"title": "x", "due": "завтра"}]},
                {"reasons": [{"when": {"field": "password", "op": "eq", "value": "1"}, "text": "x"}]},
                {"reasons": [{"when": {"field": "form", "op": "eq", "value": "ООО"}}]},
                {"requires": [{"all": []}]}, {"impact": "extreme"}, {"unknown": 1}]:
        r = owner.put("/api/admin/changes/t-2", {**BASE, **bad}, headers=ADMIN)
        assert r.status_code == 422, bad
    assert owner.put("/api/admin/changes/bad id!", BASE, headers=ADMIN).status_code == 422
    assert owner.put("/api/admin/changes/t-3", BASE).status_code == 404                  # без токена


def test_seed_is_idempotent_and_dates_are_relative(env):
    with env.app.state.session_factory() as db:
        assert load_demo_changes(db, TODAY) == 11
        assert load_demo_changes(db, TODAY) == 11
        assert len(db.scalars(select(Change)).all()) == 11
        c = db.get(Change, "consent")
        assert c.published_at.isoformat() == "2026-09-30" and c.effective_at.isoformat() == "2026-11-03"
        assert c.actions[0]["due"] == "2026-10-30"
        data = json.loads(open("seed/changes.json", encoding="utf-8").read())
        assert all(set(r) == {"when", "text"} for d in data for r in d["reasons"])


# ---------- устойчивость загрузки (повторы, сбои, ошибки модели) ----------

@pytest.fixture
def no_sleep(monkeypatch):
    import ingest.pravo as pravo
    monkeypatch.setattr(pravo, "BACKOFF", 0)


def test_http_retries_on_timeout_5xx_and_429(no_sleep):
    from ingest.pravo import get_with_retry
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("медленно", request=request)
        return httpx.Response(503) if len(calls) == 2 else (
            httpx.Response(429) if len(calls) == 3 else httpx.Response(200, text="ok"))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_with_retry(client, "https://x.test/", retries=4).text == "ok" and len(calls) == 4


def test_http_does_not_retry_404(no_sleep):
    from ingest.pravo import get_with_retry
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(404)))
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(client, "https://x.test/")
    assert len(calls) == 1


def test_http_gives_up_after_retries(no_sleep):
    from ingest.pravo import get_with_retry
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(client, "https://x.test/", retries=2)


def test_ingest_survives_broken_model_and_one_bad_act(env, no_sleep):
    class Boom:
        def complete_json(self, *a, **k):
            raise RuntimeError("модель недоступна")

    def handler(request):
        url = str(request.url)
        if url.endswith("/api/Rss"):
            return httpx.Response(200, text=RSS)
        eo = request.url.params.get("eoNumber")
        return httpx.Response(200, json=DOCS[eo]) if eo in DOCS else httpx.Response(404)
    with env.app.state.session_factory() as db:
        stats = run_ingest(db, env.settings, http=httpx.Client(transport=httpx.MockTransport(handler)), llm=Boom(),
                           text_fetcher=lambda c, e: "текст " * 40)
        assert stats["errors"] == 0 and stats["added"] == 1 and stats["fallback"] == 1   # модель упала — правила


def test_ingest_counts_documents_that_fail_to_load(env, no_sleep):
    def handler(request):
        if str(request.url).endswith("/api/Rss"):
            return httpx.Response(200, text=RSS)
        return httpx.Response(500)
    with env.app.state.session_factory() as db:
        stats = run_ingest(db, env.settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
        assert stats["errors"] == 2 and stats["added"] == 0


def test_ingest_rss_failure_raises_for_the_loop_to_log(env, no_sleep):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with env.app.state.session_factory() as db, pytest.raises(httpx.HTTPError):
        run_ingest(db, env.settings, http=client)


def test_gold_xlsx_template_roundtrip(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from ingest import gold_xlsx
    from ingest.evaluate import load_gold
    tpl = tmp_path / "t.xlsx"
    gold_xlsx.make_template(str(tpl))
    wb = openpyxl.load_workbook(tpl)
    acts = wb["Акты"]
    acts.delete_rows(2)
    acts.append(["a1", "О персональных данных", "Федеральный закон", "1-ФЗ", "2026-09-10", "Статья 1. " + "текст " * 30,
                 "да", "pd", "high", "2026-12-09", "нет", ""])
    wb["Применимость"].delete_rows(2, 5)
    wb["Применимость"].append(["a1", "P1", "да"])
    wb["Применимость"].append(["a1", "P2", "нет"])
    wb.save(tpl)
    out = tmp_path / "g.jsonl"
    assert gold_xlsx.convert(str(tpl), str(out)) == 1
    item = load_gold(out)[0]
    assert item["expected"] == {"relevant": True, "topic": "pd", "impact": "high", "effective_date": "2026-12-09",
                                "is_draft": False}
    assert item["profiles"][0]["profile"]["pd_customers"] is True and item["profiles"][1]["applies"] is False
    assert item["profiles"][0]["profile"]["digital"] == ["CRM", "Облачные хранилища"]


def test_gold_xlsx_reports_mistakes(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from ingest import gold_xlsx
    tpl = tmp_path / "t.xlsx"
    gold_xlsx.make_template(str(tpl))
    wb = openpyxl.load_workbook(tpl)
    wb["Акты"].delete_rows(2)
    wb["Акты"].append(["a1", "Акт", "Закон", "1", "2026-09-10", "коротко", "да", "нет-такой", "huge", "", "нет", ""])
    wb.save(tpl)
    with pytest.raises(SystemExit) as e:
        gold_xlsx.convert(str(tpl), str(tmp_path / "g.jsonl"))
    msg = str(e.value)
    assert "неизвестная тематика" in msg and "high, med или low" in msg and "не короче 80" in msg
