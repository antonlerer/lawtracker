import pytest

from app import rules
from app.security import hash_password, inn_valid, password_problem, verify_password

PROFILE = {"industry": "Медицина", "digital": ["CRM"], "pd_employees": True, "entities": 2, "size": "small",
           "headcount": "16–100", "activities": []}


def test_rule_basic_ops():
    assert rules.evaluate({"field": "pd_employees", "op": "is_true"}, PROFILE)
    assert not rules.evaluate({"field": "pd_customers", "op": "is_true"}, PROFILE)      # поля нет — не выполняется
    assert rules.evaluate({"field": "industry", "op": "eq", "value": "Медицина"}, PROFILE)
    assert rules.evaluate({"field": "digital", "op": "contains", "value": "CRM"}, PROFILE)
    assert rules.evaluate({"field": "entities", "op": "gt", "value": 1}, PROFILE)
    assert rules.evaluate({"field": "size", "op": "in", "value": ["micro", "small"]}, PROFILE)
    assert not rules.evaluate({"field": "activities", "op": "not_empty"}, PROFILE)
    assert rules.evaluate({"not": {"field": "remote", "op": "is_true"}}, PROFILE)


def test_missing_field_is_not_equal_either():
    # «ne» по незаполненному полю не должно срабатывать: неизвестное ≠ «не равно»
    assert not rules.evaluate({"field": "form", "op": "ne", "value": "ИП"}, PROFILE)


def test_combinators():
    node = {"all": [{"field": "pd_employees", "op": "is_true"},
                    {"any": [{"field": "industry", "op": "eq", "value": "X"},
                             {"field": "digital", "op": "contains_any", "value": ["CRM", "ERP"]}]}]}
    assert rules.evaluate(node, PROFILE)


@pytest.mark.parametrize("bad", [
    {"field": "password", "op": "eq", "value": "x"},            # поле вне белого списка
    {"field": "industry", "op": "gt", "value": 1},               # операция не подходит к типу
    {"field": "remote", "op": "is_true", "value": True},         # лишнее значение
    {"field": "industry", "op": "eq"},                           # нет значения
    {"all": []},
    {"all": [{"field": "remote", "op": "is_true"}], "any": []},
    {"field": "size", "op": "eq", "value": "huge"},
    {"field": "entities", "op": "gt", "value": "1"},
    "text",
    {"field": "remote", "op": "is_true", "extra": 1},
])
def test_invalid_rules_rejected(bad):
    with pytest.raises(rules.RuleError):
        rules.validate_rule(bad)


def test_depth_and_size_limits():
    node = {"field": "remote", "op": "is_true"}
    for _ in range(10):
        node = {"not": node}
    with pytest.raises(rules.RuleError):
        rules.validate_rule(node)
    with pytest.raises(rules.RuleError):
        rules.validate_rule({"any": [{"field": "remote", "op": "is_true"}] * 100})


def test_match_requires_and_reasons():
    reasons = [{"when": {"field": "pd_employees", "op": "is_true"}, "text": "Есть данные сотрудников"},
               {"when": {"field": "headcount", "op": "not_empty"}, "text": "Численность: {headcount}"}]
    assert rules.match([], reasons, PROFILE) == ["Есть данные сотрудников", "Численность: 16–100"]
    # requires блокирует, даже если причины сработали
    assert rules.match([{"field": "industry", "op": "eq", "value": "Другое"}], reasons, PROFILE) == []
    assert rules.render_reason("Размер: {size_label}, {unknown}", PROFILE) == "Размер: малое, {unknown}"


def test_password_policy_and_hash():
    assert password_problem("short1")
    assert password_problem("onlyletters")
    assert password_problem("12345678")
    assert password_problem("Passw0rd!") is None
    h = hash_password("Passw0rd!")
    assert verify_password("Passw0rd!", h) and not verify_password("Passw0rd?", h)
    assert h != hash_password("Passw0rd!")                      # разная соль
    assert not verify_password("x", "garbage") and not verify_password("x", None)


def test_inn_checksum():
    assert inn_valid("7707083893") and inn_valid("500100732259")
    assert not inn_valid("7707083894") and not inn_valid("123") and not inn_valid("abcdefghij")
