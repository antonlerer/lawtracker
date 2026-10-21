"""Правила применимости изменения к компании.

Правило — JSON-дерево над полями профиля (а не теги):
    {"field": "pd_employees", "op": "is_true"}
    {"field": "industry", "op": "eq", "value": "Медицина"}
    {"field": "digital", "op": "contains", "value": "CRM"}
    {"all": [...]}, {"any": [...]}, {"not": {...}}

Изменение применимо, если выполнены все `requires` и сработала хотя бы одна из `reasons`.
Текст сработавшей причины показывается клиенту («почему это показано вам»).
"""
from __future__ import annotations

from typing import Any

from .catalog import HEADCOUNTS, SIZES

# Поля профиля, по которым разрешено строить правила, и допустимые операции.
FIELD_KINDS: dict[str, str] = {
    "form": "str", "size": "str", "industry": "str", "headcount": "str", "branches": "str", "contact_role": "str",
    "entities": "int",
    "foreign_share": "bool", "remote": "bool", "foreign_staff": "bool", "pd_customers": "bool",
    "pd_employees": "bool", "pd_external": "bool", "pd_cross": "bool", "ved_export": "bool", "ved_import": "bool",
    "activities": "list", "regions": "list", "licenses": "list", "digital": "list", "topics": "list",
}
OPS_BY_KIND = {
    "bool": {"is_true", "is_false"},
    "str": {"eq", "ne", "in", "not_empty"},
    "int": {"eq", "ne", "gt", "gte", "lt", "lte"},
    "list": {"contains", "contains_any", "not_empty"},
}
MAX_DEPTH = 6
MAX_NODES = 60


class RuleError(ValueError):
    pass


def validate_rule(node: Any, _depth: int = 0, _count: list[int] | None = None) -> None:
    """Проверяет структуру правила. Бросает RuleError, ничего не выполняет."""
    count = _count if _count is not None else [0]
    count[0] += 1
    if count[0] > MAX_NODES:
        raise RuleError("слишком большое правило")
    if _depth > MAX_DEPTH:
        raise RuleError("слишком глубокая вложенность")
    if not isinstance(node, dict):
        raise RuleError("правило должно быть объектом")
    keys = set(node)
    if keys in ({"all"}, {"any"}):
        items = node[next(iter(keys))]
        if not isinstance(items, list) or not items:
            raise RuleError("all/any: нужен непустой список")
        for item in items:
            validate_rule(item, _depth + 1, count)
        return
    if keys == {"not"}:
        validate_rule(node["not"], _depth + 1, count)
        return
    if not keys <= {"field", "op", "value"} or "field" not in node or "op" not in node:
        raise RuleError(f"неизвестная форма правила: {sorted(keys)}")
    field, op = node["field"], node["op"]
    kind = FIELD_KINDS.get(field)
    if kind is None:
        raise RuleError(f"поле «{field}» нельзя использовать в правилах")
    if op not in OPS_BY_KIND[kind]:
        raise RuleError(f"операция «{op}» не подходит для поля «{field}» ({kind})")
    needs_value = op not in {"is_true", "is_false", "not_empty"}
    if needs_value != ("value" in node):
        raise RuleError(f"операция «{op}»: значение {'обязательно' if needs_value else 'не нужно'}")
    if needs_value:
        v = node["value"]
        if op in {"in", "contains_any"} and not (isinstance(v, list) and v):
            raise RuleError(f"операция «{op}»: нужен непустой список значений")
        if kind == "int" and (isinstance(v, bool) or not isinstance(v, int)):
            raise RuleError("для числового поля нужно целое значение")
        if field == "size" and op in {"eq", "ne"} and v not in SIZES:
            raise RuleError(f"неизвестный размер: {v}")
        if field == "headcount" and op in {"eq", "ne"} and v not in HEADCOUNTS:
            raise RuleError(f"неизвестная численность: {v}")


def evaluate(node: dict, profile: dict) -> bool:
    """Вычисляет правило. Отсутствующее поле даёт False (профиль мог быть заполнен не полностью)."""
    if "all" in node:
        return all(evaluate(n, profile) for n in node["all"])
    if "any" in node:
        return any(evaluate(n, profile) for n in node["any"])
    if "not" in node:
        return not evaluate(node["not"], profile)
    field, op = node["field"], node["op"]
    val = profile.get(field)
    if op == "is_true":
        return val is True
    if op == "is_false":
        return val is False
    if op == "not_empty":
        return bool(val)
    arg = node.get("value")
    if op == "eq":
        return val is not None and val == arg
    if op == "ne":
        return val is not None and val != arg
    if op == "in":
        return val in arg
    if op in {"gt", "gte", "lt", "lte"}:
        if val is None:
            return False
        return {"gt": val > arg, "gte": val >= arg, "lt": val < arg, "lte": val <= arg}[op]
    if op == "contains":
        return isinstance(val, list) and arg in val
    if op == "contains_any":
        return isinstance(val, list) and any(a in val for a in arg)
    raise RuleError(op)  # недостижимо после validate_rule


class _SafeDict(dict):
    def __missing__(self, key):  # неизвестные плейсхолдеры остаются как есть
        return "{" + key + "}"


def render_reason(text: str, profile: dict) -> str:
    """Подставляет значения профиля в текст причины: «Численность: {headcount}»."""
    data = _SafeDict({k: v for k, v in profile.items() if isinstance(v, (str, int))})
    data["size_label"] = SIZES.get(profile.get("size") or "", "")
    return text.format_map(data)


def match(requires: list[dict], reasons: list[dict], profile: dict) -> list[str]:
    """Возвращает тексты сработавших причин; пустой список — изменение компании не касается."""
    if any(not evaluate(r, profile) for r in requires):
        return []
    return [render_reason(r["text"], profile) for r in reasons if evaluate(r["when"], profile)]
