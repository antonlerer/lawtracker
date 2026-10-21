"""Справочники сервиса. Единый источник для API, валидации профиля и правил применимости."""
from __future__ import annotations

TOPICS: dict[str, str] = {
    "pd": "Персональные данные",
    "tax": "Налоги и отчётность",
    "labor": "Трудовое право",
    "ecom": "Интернет-торговля",
    "ads": "Маркетинг и реклама",
    "it": "IT и технологии",
    "lic": "Лицензирование",
    "log": "Логистика",
    "ved": "Внешнеэкономическая деятельность",
}
INDUSTRIES = [
    "Транспорт и логистика", "Розничная торговля", "Интернет-торговля", "IT и технологии", "Производство",
    "Строительство", "Медицина", "Образование", "Услуги и консалтинг", "Другое",
]
ACTIVITIES = [
    "Грузоперевозки", "Складская логистика", "Интернет-магазин", "Оптовая торговля", "Разработка ПО",
    "Маркетинг и реклама", "Производство продукции", "Строительство", "Медицинские услуги", "Консалтинг",
    "Общественное питание",
]
REGIONS = [
    "Москва и МО", "Санкт-Петербург и ЛО", "Центральный ФО", "Северо-Запад", "Юг", "Приволжье", "Урал",
    "Сибирь", "Дальний Восток", "Вся Россия",
]
LICENSES = [
    "Медицинская деятельность", "Образовательная деятельность", "Перевозка пассажиров", "Алкогольная продукция",
    "Связь", "Частная охрана",
]
DIGITAL = [
    "Интернет-магазин", "Маркетплейсы", "CRM", "Онлайн-оплата", "Мобильное приложение", "ИИ-сервисы",
    "Облачные хранилища", "E-mail и SMS-рассылки",
]
FORMS = ["ООО", "ИП", "АО", "Самозанятый", "Другое"]
SIZES = {"micro": "микро", "small": "малое", "medium": "среднее", "large": "крупное"}
SIZE_NAMES = {"micro": "Микро", "small": "Малое", "medium": "Среднее", "large": "Крупное"}
HEADCOUNTS = ["1–15", "16–100", "101–250", "более 250"]
BRANCHES = ["", "yes", "no"]

ROLES = {"owner": "Владелец", "admin": "Администратор", "member": "Участник", "viewer": "Просмотр"}
ROLE_DESC = {
    "owner": "Полный доступ, управление тарифом и удаление компании",
    "admin": "Профиль, уведомления, команда",
    "member": "Дайджесты, задачи, сохранённое",
    "viewer": "Только просмотр дайджестов",
}
ROLE_RANK = {"viewer": 1, "member": 2, "admin": 3, "owner": 4}

IMPACT_RANK = {"high": 3, "med": 2, "low": 1}
IMPACT_NAMES = {"high": "Высокий", "med": "Средний", "low": "Низкий"}
CONFIDENCE = ("high", "mid", "low")

# Шаги профиля: какие поля заполняются на каком шаге. opt=True — шаг можно пропустить.
STEPS: list[dict] = [
    {"id": "basics", "title": "Основные сведения", "opt": False, "fields": ["name", "inn", "form", "size"],
     "affects": "Налоги и отчётность, Трудовое право"},
    {"id": "industry", "title": "Отрасль и направления", "opt": False,
     "fields": ["industry", "activities", "desc", "topics"], "affects": "Все тематики"},
    {"id": "regions", "title": "Регионы работы", "opt": False, "fields": ["regions"], "affects": "Региональные нормы"},
    {"id": "structure", "title": "Организационная структура", "opt": False,
     "fields": ["branches", "foreign_share", "entities"], "affects": "Налоги и отчётность"},
    {"id": "staff", "title": "Сотрудники и кадровые процессы", "opt": False,
     "fields": ["headcount", "remote", "foreign_staff"], "affects": "Трудовое право"},
    {"id": "licenses", "title": "Лицензии и разрешения", "opt": True, "fields": ["licenses"], "affects": "Лицензирование"},
    {"id": "pd", "title": "Работа с персональными данными", "opt": False,
     "fields": ["pd_customers", "pd_employees", "pd_external", "pd_cross"], "affects": "Персональные данные"},
    {"id": "ved", "title": "Внешнеэкономическая деятельность", "opt": True, "fields": ["ved_export", "ved_import"],
     "affects": "ВЭД"},
    {"id": "digital", "title": "Цифровые сервисы и технологии", "opt": True, "fields": ["digital"],
     "affects": "IT, Персональные данные, Реклама"},
    {"id": "contacts", "title": "Контакты и получатели уведомлений", "opt": False,
     "fields": ["contact_name", "contact_email", "contact_role", "recipients"], "affects": "Уведомления"},
]
STEP_BY_ID = {s["id"]: s for s in STEPS}

# Тарифы. Цены предварительные (как в демо): платёжный модуль не подключён.
PLANS: dict[str, dict] = {
    "free": {"id": "free", "name": "Бесплатный старт", "short": "Старт", "price_month": 0, "price_year": 0,
             "users": 1, "areas": 1},
    "basic": {"id": "basic", "name": "Базовый", "short": "Базовый", "price_month": 690, "price_year": 6600,
              "users": 1, "areas": 3},
    "pro": {"id": "pro", "name": "Профессиональный", "short": "Проф.", "price_month": 1690, "price_year": 16200,
            "users": 5, "areas": 10},
    "corp": {"id": "corp", "name": "Корпоративный", "short": "Корп.", "price_month": None, "price_year": None,
             "price_from": 4900, "users": None, "areas": None},
}
PLAN_ORDER = ["free", "basic", "pro", "corp"]


def plan_at_least(plan: str, minimum: str) -> bool:
    return PLAN_ORDER.index(plan) >= PLAN_ORDER.index(minimum)


def entitlements(plan: str, enforce: bool = True) -> dict:
    """Что даёт тариф (см. матрицу тарифов в демо). При enforce=False открыто всё."""
    if not enforce:
        return {"personal": True, "reasons": True, "actions": True, "telegram": True, "notif_settings": True,
                "roles": True, "archive_days": None, "users": None, "areas": None}
    p = PLANS[plan]
    return {
        "personal": plan_at_least(plan, "basic"),
        "reasons": plan_at_least(plan, "basic"),
        "actions": plan_at_least(plan, "pro"),
        "telegram": plan_at_least(plan, "basic"),
        "notif_settings": plan_at_least(plan, "basic"),
        "roles": plan_at_least(plan, "pro"),
        "archive_days": None if plan_at_least(plan, "pro") else 90,
        "users": p["users"],
        "areas": p["areas"],
    }
