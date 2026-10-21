# LawTracker — серверная часть

Бэкенд для сайта LawTracker с тем же функционалом, что в демоверсии: регистрация и вход, профиль компании,
подбор изменений законодательства, дайджесты, карточка изменения с планом действий, уведомления, Telegram,
команда и роли, тарифы, поддержка, настройки аккаунта.

**Главное отличие от репозитория `lwtk`:** клиент заполняет **профиль компании** (типизированные поля), а не теги.
Применимость изменения считается правилами над полями профиля, и клиенту показывается причина
(«Указана численность сотрудников: 16–100»).

Стек тот же, что в `lwtk`: FastAPI + SQLAlchemy 2 + Pydantic 2, SQLite для разработки, PostgreSQL через `DATABASE_URL`.

## Запуск

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export DATABASE_URL=sqlite:///./lawtracker.db   # или postgresql+psycopg2://user:pass@host/db
export COOKIE_SECURE=false                       # только для http://localhost; в продакшене оставьте true
export ADMIN_TOKEN=$(openssl rand -hex 24)       # включает служебные методы /api/admin/*

python -m app.cli seed                           # 11 демо-изменений (даты считаются от сегодняшнего дня)
uvicorn app.main:create_app --factory --port 8000
# документация API: http://localhost:8000/api/docs

pytest                                           # 93 теста
```

Фоновые задачи (cron или отдельные процессы):

```bash
python -m app.cli ingest             # новые акты с pravo.gov.ru → разбор → БД
python -m app.cli notify --loop 300  # рассылка дайджестов по расписанию клиентов
```

Переменные окружения: см. `app/config.py` (`BASE_URL`, `TZ_NAME`, `SMTP_*`, `MAIL_FROM`, `BOT_TOKEN`,
`TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`, `GIGACHAT_AUTH_KEY`, `ENFORCE_PLAN_LIMITS`).
Без `SMTP_HOST` письма пишутся в лог, без `BOT_TOKEN` сообщения в Telegram не отправляются.

## Профиль компании вместо тегов

Профиль — модель `CompanyProfile` (`app/profile.py`) с конкретными полями и допустимыми значениями из справочников
(`app/catalog.py`, отдаются методом `GET /api/meta`). Заполняется по 10 шагам, как в демо:

| Шаг | Поля |
|---|---|
| `basics` | `name`, `inn` (проверяется контрольная сумма), `form`, `size` |
| `industry` | `industry`, `activities[]`, `desc`, `topics[]` (сферы мониторинга) |
| `regions` | `regions[]` |
| `structure` | `branches`, `foreign_share`, `entities` |
| `staff` | `headcount`, `remote`, `foreign_staff` |
| `licenses` (можно пропустить) | `licenses[]` |
| `pd` | `pd_customers`, `pd_employees`, `pd_external`, `pd_cross` |
| `ved` (можно пропустить) | `ved_export`, `ved_import` |
| `digital` (можно пропустить) | `digital[]` |
| `contacts` | `contact_name`, `contact_email`, `contact_role`, `recipients[]` |

Прогресс профиля (`completeness.percent`) считается по заполненным и пропущенным шагам.

## Как изменение подбирается компании

У изменения (`Change`) вместо тегов хранятся **условия применимости** — JSON-правила над полями профиля (`app/rules.py`):

```json
{"requires": [],
 "reasons": [
   {"when": {"field": "pd_employees", "op": "is_true"},
    "text": "В профиле указано: компания обрабатывает персональные данные сотрудников"},
   {"when": {"field": "headcount", "op": "not_empty"}, "text": "Указана численность сотрудников: {headcount}"}
 ],
 "regions": []}
```

* Изменение касается компании, если выполнены все `requires` и сработала хотя бы одна из `reasons`
  (как в демо). Тексты сработавших причин показываются клиенту.
* Правила: `{"field","op","value"}`, `all`, `any`, `not`. Операции зависят от типа поля
  (`is_true`, `eq`, `in`, `contains`, `contains_any`, `gt`, `not_empty`, …). Допустимы только поля профиля;
  вложенность и размер ограничены. Правило проверяется при загрузке и **никогда не исполняется как код**.
* Если у изменения указаны `regions`, а у компании регионы заданы (и нет «Вся Россия»), пересечение обязательно.
* Пока профиль не начат, изменения не персонализируются (видна общая лента).
* Требует внимания: персональное, не проект, высокое влияние или срок ≤ 30 дней, и есть невыполненные действия.

## Тарифы

`catalog.entitlements()` реализует матрицу тарифов из демо (отключается `ENFORCE_PLAN_LIMITS=false`):
персонализация, причины показа и Telegram — с «Базового»; план действий, роли и архив старше 90 дней — с
«Профессионального»; лимиты сфер и пользователей. Платёжный модуль **не подключён**: клиент отправляет заявку
(`POST /api/billing/request`), тариф переключает администратор (`PUT /api/admin/companies/{id}/plan`).

## API (кратко)

Все пути под `/api`, ответы JSON, ошибки `{"error": {"code", "message", "fields"}}`, авторизация — cookie `lt_session`.
Изменяющие запросы должны быть `application/json` или нести `X-Requested-With` (защита от CSRF).

| Раздел | Методы |
|---|---|
| Вход | `POST /auth/register`, `/auth/login`, `/auth/logout`, `GET /auth/me`, `POST /auth/verify-email`, `/auth/resend-code`, `/auth/forgot`, `/auth/reset` |
| Компания | `POST /company`, `POST /company/join`, `GET /company`, `PUT /company/profile/{step}`, `POST /company/profile/{step}/skip`, `DELETE /company/profile` |
| Ленты | `GET /overview`, `GET /changes` (`q, period, topic, impact, status, sort, personal, page, per_page`), `GET /changes/{id}`, `POST /changes/{id}/read`, `/save`, `PUT /changes/{id}/actions/{i}`, `GET /saved` |
| Уведомления | `GET/PUT /notifications`, `GET /telegram`, `POST /telegram/connect`, `POST /telegram/test`, `DELETE /telegram`, `POST /telegram/webhook/{secret}` |
| Команда | `GET /team`, `POST /team/invites`, `POST /team/{id}/resend`, `PATCH /team/{id}`, `DELETE /team/{id}`, `POST /team/leave` |
| Тариф | `GET /billing`, `POST /billing/request`, `GET /plans` |
| Аккаунт | `PATCH /account`, `POST /account/password`, `GET /account/sessions`, `DELETE /account/sessions/{id}`, `DELETE /account` |
| Прочее | `POST /support`, `GET /support`, `POST /contact`, `POST /plan/waitlist`, `GET /meta`, `GET /healthz` |
| Служебные (`X-Admin-Token`) | `PUT /admin/changes/{id}`, `PUT /admin/companies/{id}/plan`, `GET /admin/support` |

Telegram: зарегистрируйте webhook вашего бота на `https://<домен>/api/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>`.
Клиент получает код `LT-XXXX` (15 минут), отправляет его боту — аккаунт привязывается.

## Роли

| Роль | Может |
|---|---|
| Владелец | всё, включая тариф и удаление компании |
| Администратор | профиль, команда (кроме администраторов), уведомления |
| Участник | ленты, отметки, выполнение действий |
| Просмотр | только чтение |

## Загрузка актов

`ingest/pravo.py` читает RSS и API портала pravo.gov.ru (те же эндпоинты и поля, что в `PARSER.py` проекта),
`ingest/enrich.py` превращает акт в `Change`:

* **Без модели** определяются тематика (по началам слов, без подстрок), значимость (по виду акта) и правила применимости
  по шаблону тематики. Мусор (награждения, назначения, ратификации) отбрасывается.
* **С GigaChat** (`GIGACHAT_AUTH_KEY`) модель предлагает тематику, значимость, краткое содержание и правила; ответ строго
  проверяется (`rules.validate_rule`), при любой ошибке — откат на правила.
* По названию и виду акта нельзя честно написать «что изменилось», «кого касается» и план действий, поэтому они **не
  придумываются**: запись создаётся с `reviewed=false`, `confidence=low|mid`, юрист дополняет её методом
  `PUT /api/admin/changes/{id}`.

## Безопасность

Пароли — scrypt; сессии хранятся на сервере хешем (можно посмотреть и завершить); cookie `HttpOnly`, `SameSite=Lax`,
`Secure`; блокировка перебора пароля и злоупотреблений формами; ответы входа и «забыли пароль» не раскрывают наличие
e-mail; коды подтверждения и приглашения одноразовые и ограничены по времени; проверка прав по роли и по компании на каждый
запрос; служебные методы скрыты (404) без токена. Из демо сохранено поведение «e-mail уже зарегистрирован» при регистрации.

## Что проверено и что нет

93 автотеста (pytest, SQLite в памяти): правила, профиль, ленты и фильтры, внимание, действия, уведомления и расписание,
Telegram через подставной клиент, команда и роли, изоляция компаний, тарифы, аккаунт, разбор источника, загрузка.
Тесты ловят намеренные поломки (проверено подменой кода). Запуск `uvicorn` и вход по HTTP проверены вручную.

**Не проверено** (нет доступа в среде разработки): реальные `pravo.gov.ru`, GigaChat, SMTP, Bot API Telegram, PostgreSQL.
Работа с PostgreSQL ожидается из-за SQLAlchemy, но тесты идут на SQLite. Нужно перед продакшеном: миграции (Alembic —
сейчас таблицы создаются `create_all`), обратный прокси с HTTPS, резервные копии, мониторинг, согласие на обработку
данных и размещение БД в РФ (152-ФЗ), лимиты запросов на уровне прокси. Поиск по тексту выполняется в Python и рассчитан
на каталог в тысячи записей; для десятков тысяч нужен полнотекстовый индекс.
