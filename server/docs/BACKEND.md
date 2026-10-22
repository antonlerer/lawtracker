# LawTracker: документация бэкенда для разработчика

Версия документа: 2026-10-22. Код: каталог `server/`. Точные схемы запросов и ответов — в OpenAPI (`/api/docs`, `/api/openapi.json`);
этот документ объясняет устройство, правила и места, где легко ошибиться.

## 1. Что это

Сервис персонального мониторинга изменений законодательства РФ для малого и среднего бизнеса. Компания заполняет **профиль**
(типизированные поля, не теги), сервер сравнивает профиль с **правилами применимости** каждого изменения и показывает
только то, что касается компании, с объяснением «почему». Уведомления идут в кабинет, на почту и в Telegram.

Стек: Python 3.11, FastAPI 0.115, SQLAlchemy 2.0, Pydantic 2, httpx, pypdf. БД: SQLite (разработка, тесты) или PostgreSQL
(`DATABASE_URL=postgresql+psycopg2://...`). Фронтенд — один файл `../index.html` (SPA), его же отдаёт сервер по `/`.

## 2. Структура каталога

```
server/
  app/
    main.py            create_app(): сборка приложения, CSRF-middleware, раздача index.html
    config.py          Settings (dataclass, читает переменные окружения)
    db.py              движок, фабрика сессий, PRAGMA foreign_keys для SQLite
    models.py          таблицы SQLAlchemy
    catalog.py         справочники, шаги профиля, тарифы, entitlements()
    profile.py         CompanyProfile (Pydantic), validate_step(), completeness()
    rules.py           DSL правил применимости: validate_rule / evaluate / match
    changes.py         выдача изменений: ViewCtx, serialize, фильтры, overview
    notify.py          настройки уведомлений и рассылка (dispatch)
    telegram.py        привязка по коду, webhook, отправка (Sender)
    mailer.py          ConsoleMailer / SmtpMailer
    security.py        scrypt, токены, коды, проверка ИНН
    deps.py            зависимости FastAPI: сессия, Ctx, роли, троттлинг
    errors.py          ApiError и обработчики ошибок
    cli.py             команды seed / demo / ingest / notify / analyze / evaluate
    routers/           auth, account, company, changes, notifications, misc
  ingest/              загрузка и ИИ-разбор актов (см. раздел 9)
  seed/                демо-данные (changes.json, load.py)
  tests/               137 тестов pytest
  e2e/live_site.js     сквозной сценарий в браузере (Playwright)
  eval/                эталонная выборка для замера качества ИИ-разбора
```

## 3. Запуск и конфигурация

```bash
cd server && python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=sqlite:///./lawtracker.db COOKIE_SECURE=false DEMO_MODE=true
python -m app.cli seed && python -m app.cli demo
uvicorn app.main:create_app --factory --port 8000
pytest
```

Таблицы создаются через `Base.metadata.create_all` при старте. **Миграций (Alembic) нет** — это обязательная доработка
перед продакшеном: любая смена модели на существующей БД требует ручной миграции.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./lawtracker.db` | строка подключения |
| `COOKIE_SECURE` | `true` | cookie только по HTTPS; для `http://localhost` ставят `false` |
| `SESSION_DAYS` | 14 | срок жизни сессии |
| `BASE_URL` | `http://localhost:8000` | адрес для ссылок в письмах |
| `TZ_NAME` | `Europe/Moscow` | часовой пояс расписания рассылки |
| `ADMIN_TOKEN` | пусто | токен служебных методов `/api/admin/*`; пусто — методы отключены (404) |
| `ENFORCE_PLAN_LIMITS` | `true` | включает ограничения тарифов |
| `DEMO_MODE` | `false` | код подтверждения возвращается в ответе (`dev_code`), доступен `PUT /api/demo/plan`. **В продакшене выключить** |
| `SITE_FILE` | `../index.html` | какой файл отдавать по `/` |
| `SMTP_HOST/PORT/USER/PASSWORD`, `MAIL_FROM` | пусто | без `SMTP_HOST` письма пишутся в лог (ConsoleMailer) |
| `BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET` | пусто | Telegram; без токена сообщения не отправляются |
| `GIGACHAT_AUTH_KEY`, `GIGACHAT_SCOPE`, `GIGACHAT_MODEL`, `GIGACHAT_VERIFY_SSL` | пусто / `GIGACHAT_API_PERS` / `GigaChat-2` / `true` | ИИ-разбор через GigaChat |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | пусто | любой OpenAI-совместимый API (vLLM, Ollama, шлюз) |
| `ANALYSIS_PASSES` | 2 | число независимых проходов модели |

Ключи и токены передавайте только через переменные окружения или секреты хостинга; в репозиторий и в чат их не кладут.

## 4. Модель данных (`app/models.py`)

| Таблица | Назначение и важные поля |
|---|---|
| `users` | e-mail (уникальный), хеш пароля scrypt, `email_verified`, хеши кода подтверждения и токена сброса, `consent_at` |
| `auth_sessions` | серверные сессии: хранится **SHA-256 токена**, не сам токен; `expires_at`, `last_seen`, ip, user-agent |
| `login_attempts` | журнал попыток для троттлинга (`key`, `kind`) |
| `companies` | `plan` (free/basic/pro/corp), `profile` (JSON), `steps_saved`, `steps_skipped` |
| `members` | связь пользователь–компания: `role` (owner/admin/member/viewer), `status` (active/pending), `invite_code` |
| `changes` | изменение законодательства (см. ниже) |
| `change_states` | на пользователя: `read`, `saved` |
| `action_states` | на **компанию**: состояние действия (`todo/doing/done`) по `(change_id, idx)` |
| `notification_prefs` | настройки уведомлений пользователя (JSON, валидируется `NotifSettings`) |
| `telegram_links` | статус привязки, одноразовый код `LT-XXXX`, `chat_id` |
| `notification_logs` | что и когда отправлено (для дедупликации и расписания) |
| `support_tickets`, `plan_requests`, `waitlist` | обращения, заявки на смену тарифа, лист ожидания |

Пользователь состоит максимум в одной компании (поиск активного `Member` по `user_id`).

### Таблица `changes`

Ключевые поля: `id` (строка; для загруженных актов `pravo-<eoNumber>`), `source_key` (номер публикации на портале, уникален),
`topic` (ключ из `catalog.TOPICS`), `impact` (`high|med|low`), `published_at`, `effective_at` (nullable), `is_draft`,
`status` (`published|hidden`), `confidence` (`high|mid|low`), `reviewed` (проверил ли юрист), тексты `title/summary/what/who/comment/quote`,
`doc`, `source_url`, JSON-поля `requires`, `reasons`, `regions`, `actions`, `related`.

- `actions`: `[{"title", "due": "ГГГГ-ММ-ДД"|null, "responsible"}]`; состояние хранится отдельно в `action_states` по индексу.
  **Индекс действия — часть ключа:** при перезаписи изменения порядок действий менять нельзя без пересмотра `action_states`.
- `regions`: пусто = федеральное изменение.

## 5. Профиль компании (`profile.py`, `catalog.STEPS`)

Профиль заполняется по 10 шагам: `basics, industry, regions, structure, staff, licenses, pd, ved, digital, contacts`.
Шаги `licenses`, `ved`, `digital` необязательные (можно пропустить). Каждый шаг перечисляет свои поля в `catalog.STEPS`.

Поля (`CompanyProfile`): `name, inn, form, size, industry, activities[], desc, topics[], regions[], branches, foreign_share, entities,
headcount, remote, foreign_staff, licenses[], pd_customers, pd_employees, pd_external, pd_cross, ved_export, ved_import, digital[],
contact_name, contact_email, contact_role, recipients[]`. Допустимые значения берутся из справочников `catalog.py`; ИНН проверяется
по контрольной сумме (`security.inn_valid`).

- `PUT /api/company/profile/{step}` проверяет **всю модель**, затем принимает только поля этого шага (чужие поля → 422), и требует
  обязательные поля шага (`profile.REQUIRED`).
- Прогресс: `completeness()` даёт процент, следующий шаг и состояние каждого шага.
- Пока не сохранён ни один шаг (`steps_saved` пуст), персонализации нет: `changes.evaluate` возвращает «не касается».

## 6. Правила применимости (`rules.py`)

Правило — JSON-дерево над полями профиля. Никакого `eval`: только разрешённые поля и операции.

```json
{"all": [
  {"field": "industry", "op": "eq", "value": "Интернет-торговля"},
  {"any": [{"field": "pd_customers", "op": "is_true"}, {"field": "digital", "op": "contains", "value": "CRM"}]}
]}
```

Типы полей (`FIELD_KINDS`) и допустимые операции (`OPS_BY_KIND`):

| Тип | Операции |
|---|---|
| `bool` | `is_true`, `is_false` |
| `str` | `eq`, `ne`, `in`, `not_empty` |
| `int` | `eq`, `ne`, `gt`, `gte`, `lt`, `lte` |
| `list` | `contains`, `contains_any`, `not_empty` |

Узлы: `{"all": [...]}`, `{"any": [...]}`, `{"not": {...}}`. Ограничения: глубина ≤ 6, узлов ≤ 60.

**Семантика применимости** (`rules.match`): изменение касается компании, если выполнены **все** правила из `requires` и сработала
**хотя бы одна** причина из `reasons`. Каждая причина — `{"when": <правило>, "text": "..."}`; тексты сработавших причин
показываются клиенту. В тексте можно использовать плейсхолдеры профиля: `{headcount}`, `{size_label}` и любые строковые/числовые поля.
Неизвестный плейсхолдер остаётся как есть.

Важные особенности:
- Отсутствующее поле даёт `False` для любых операций, включая `ne` и `is_false` (неполный профиль не даёт ложных срабатываний).
  `is_false` срабатывает только на явном `False`, а не на «не заполнено».
- Дополнительно `changes.evaluate` фильтрует по регионам: если у изменения заданы `regions`, а у компании регионы указаны и среди них нет
  «Вся Россия» и нет пересечения — изменение не касается компании.
- Любое правило, пришедшее извне (админка, ИИ), сначала проходит `validate_rule`; `evaluate` предполагает уже проверенное правило.

## 7. Лента изменений (`changes.py`)

Контекст запроса — `ViewCtx` (пользователь, компания, профиль, тариф, права тарифа, «сегодня», прочитанное, сохранённое, состояния действий).
«Сегодня» берётся из `app.state.today` (подменяется в тестах) либо `date.today()`.

- Видимость: только `status="published"`; на тарифах `free/basic` — не старше 90 дней (`archive_days`).
- `serialize()` формирует карточку: `personal`, `reasons` (пусто, если тариф не даёт объяснений), `days_left`, `status`
  (`draft|upcoming|in_force`), `attention`, `open_actions` (null, если тариф не даёт действий).
- «Требует внимания» (`needs_attention`): изменение персональное, не черновик, есть незавершённые действия и либо `impact=high`,
  либо до вступления в силу 0–30 дней.
- `GET /api/changes`: фильтры `q, period (all/7/30/90), topic, impact, status (all/new/read/saved/attn), sort (date/impact/eff),
  personal, page, per_page (≤50), detail`. Поиск `q` выполняется **в Python** (`casefold`), потому что `lower()/LIKE` в SQLite не работают
  с кириллицей; для каталога в десятки тысяч записей нужен полнотекстовый индекс.
- Действия: `PUT /api/changes/{id}/actions/{idx}` с телом `{"state": "todo|doing|done"}`; состояние общее для компании. Нужен тариф `pro`.

## 8. Тарифы и роли

`catalog.entitlements(plan, enforce)` — единственное место, где определено, что даёт тариф:

| Возможность | free | basic | pro | corp |
|---|---|---|---|---|
| Подбор по профилю и причины | нет | да | да | да |
| Telegram и настройки уведомлений | нет | да | да | да |
| Действия, сроки, ответственные | нет | нет | да | да |
| Роли и доступы | нет | нет | да | да |
| Архив | 90 дн. | 90 дн. | без огр. | без огр. |
| Пользователей / сфер | 1 / 1 | 1 / 3 | 5 / 10 | без огр. |

При `ENFORCE_PLAN_LIMITS=false` открыто всё. Платежей нет: смена тарифа — заявка (`POST /api/billing/request`) и ручное применение
через `PUT /api/admin/companies/{id}/plan` (проверяет лимиты, иначе 409 `limits_exceeded`). В `DEMO_MODE` владелец может менять тариф сам.

Роли: `viewer(1) < member(2) < admin(3) < owner(4)`. Проверка — зависимость `deps.need("<роль>")`; ошибки 409 `company_required`
(нет компании) и 403 `forbidden`. Последнего владельца удалить или понизить нельзя; изоляция компаний проверена тестами.

## 9. HTTP API

Формат ошибок единый:

```json
{"error": {"code": "validation_error", "message": "Проверьте введённые данные", "fields": {"inn": "Неверный ИНН"}}}
```

Коды: 401 `unauthorized`, 403 `forbidden`/`csrf`/`email_not_verified`, 404 `not_found`, 409 (`company_required`,
`already_in_company`, `limits_exceeded`), 422 `validation_error`, 429 `too_many_requests`, 503 `llm_not_configured`.

**Аутентификация:** cookie `lt_session` (HttpOnly, SameSite=Lax, Secure по настройке). Токен в БД хранится только в виде SHA-256.
**CSRF:** изменяющий запрос (POST/PUT/PATCH/DELETE на `/api/*`) должен быть `application/json` либо нести `X-Requested-With`;
исключения — webhook Telegram и запросы с `X-Admin-Token`. Иначе 403 `csrf`.
**Троттлинг:** вход, код подтверждения и сброс пароля ограничены по ключу (e-mail/ip) через `deps.throttle`.

| Группа | Методы |
|---|---|
| Аутентификация `/api/auth` | `POST register`, `login`, `logout`, `verify-email`, `resend-code`, `forgot`, `reset`; `GET me` |
| Аккаунт `/api/account` | `PATCH ""` (имя, телефон), `POST password`, `GET sessions`, `DELETE sessions/{id}`, `DELETE ""` (удаление аккаунта) |
| Компания `/api` | `POST company`, `POST company/join`, `GET company`, `PUT company/profile/{step}`, `POST company/profile/{step}/skip`, `DELETE company/profile` |
| Команда | `GET team`, `POST team/invites`, `POST team/{id}/resend`, `PATCH team/{id}`, `DELETE team/{id}`, `POST team/leave` |
| Тариф | `GET billing`, `POST billing/request` |
| Изменения | `GET overview`, `GET changes`, `GET changes/{id}`, `POST changes/{id}/read`, `POST changes/{id}/save`, `PUT changes/{id}/actions/{idx}`, `GET saved` |
| Уведомления | `GET/PUT notifications`, `GET telegram`, `POST telegram/connect`, `DELETE telegram`, `POST telegram/test`, `POST telegram/webhook/{secret}` |
| Прочее | `GET healthz`, `GET meta` (справочники и «сегодня»), `GET plans`, `POST/GET support`, `POST contact`, `POST plan/waitlist`, `PUT demo/plan` (только DEMO_MODE) |
| Служебные (`X-Admin-Token`) | `PUT admin/changes/{id}` (создать/обновить изменение), `POST admin/changes/{id}/analyze`, `PUT admin/companies/{id}/plan`, `GET admin/support` |

Регистрация: пароль ≥ 8 символов, буква и цифра; подтверждение e-mail 6-значным кодом (хранится хеш, ограничено число попыток);
в `DEMO_MODE` код возвращается в ответе как `dev_code`. Сессия создаётся при входе; смена пароля и сброс закрывают прочие сессии
(проверено тестами).

`PUT /api/admin/changes/{id}` проверяет правила (`validate_rule`), структуру причин, формат сроков действий (ISO-даты) и регионы.

## 10. Уведомления (`notify.py`, `telegram.py`)

Настройки пользователя (`NotifSettings`): каналы `cabinet/email/tg`, частота почты `now|daily|weekly`, тематики, минимальная значимость,
дни недели (1–7), время `ЧЧ:ММ`, пауза (до даты).

`notify.dispatch()` — один проход рассылки (запускается как `python -m app.cli notify --loop 300` или из cron). Для каждого активного
участника с заполненным профилем:
1. пропуск, если пауза;
2. каналы: почта (адрес подтверждён) и Telegram (тариф, привязка `connected`);
3. кандидаты — опубликованные не-черновики, персональные, не прочитанные и ещё не отправленные в этот канал, подходящие по тематикам и
   порогу значимости; сортировка по значимости и сроку;
4. если «пора» по расписанию — отправляется весь дайджест, иначе только `impact=high` (срочное);
5. успешная отправка пишется в `notification_logs` — так не бывает повторов.

Дайджест на почту идёт на `contact_email` профиля; владельцу — ещё и на `recipients`. Время проверяется в часовом поясе `TZ_NAME`.

Telegram: клиент получает код `LT-XXXX` (срок действия ограничен), отправляет его боту; webhook `/api/telegram/webhook/{secret}`
сопоставляет код и сохраняет `chat_id`. Невалидный JSON в webhook → 400. Секрет сравнивается с `TELEGRAM_WEBHOOK_SECRET`.
Отправка идёт через интерфейс `Sender` (`BotApiSender` или `FakeSender` в тестах); аналогично `Mailer`.

## 11. Загрузка и ИИ-разбор актов (`ingest/`)

Конвейер `ingest.run.run_ingest()` (CLI: `python -m app.cli ingest`):

1. `pravo.fetch_rss` / `parse_rss` — список номеров публикаций с publication.pravo.gov.ru; `fetch_document` — метаданные акта.
2. Уже известные `source_key` пропускаются (идемпотентность).
3. `text.fetch_text` — PDF акта → `pdf_to_text` (pypdf) → `clean_text`. Сканы без текстового слоя дают `None`.
4. Если настроена модель — `Analyzer.analyze()`; если нет модели, текста или ответа — запасной путь `enrich.enrich()` (тематика по началам слов,
   значимость по виду акта, правила из шаблона тематики, `confidence=low`). Нерелевантные акты (награждения, назначения) отбрасываются.
5. Запись `Change(id="pravo-<eo>", reviewed=False, ...)`. Статистика: `seen, new, added, skipped, errors, analyzed, fallback`.

### Принцип ИИ-разбора: модель предлагает — код проверяет

`ingest/prompts.py` содержит системный промпт (версия `PROMPT_VERSION`), пользовательский промпт и промпт починки формата.
`ingest/analyze.py` разбирает ответ модели строго:

- модель возвращает JSON; при невалидном JSON делается **одна** попытка починки;
- ключевая цитата и цитаты-основания причин и действий должны **дословно** (с допуском на пробелы/кавычки/тире) встречаться в тексте акта,
  иначе отбрасываются; действие без подтверждённой цитаты не попадает в карточку;
- даты считает `resolve_date`, а не модель (календарные даты, «по истечении N дней/месяцев после опубликования», «со дня опубликования»;
  рабочие дни не считаются); модель лишь цитирует фразу о вступлении в силу, и фраза проверяется по тексту;
- числа в пояснениях сверяются с текстом (`ungrounded_numbers`), чужие числа → `confidence=low`;
- правила и причины валидируются; при отказе — шаблон тематики и пометка в `notes`;
- `ANALYSIS_PASSES` проходов: консенсус по `relevant/topic/impact/сроку`, расхождение → `confidence=low`;
- длинный текст сокращается до бюджета 24000 знаков с сохранением фраз о вступлении в силу (`select_excerpt`), уверенность не выше `mid`;
- результат всегда с `reviewed=False`; `AnalysisResult(relevant, fields, notes, report, answers)`, где `report` — метрики проверки
  (сколько цитат подтверждено, сколько действий отброшено и т. д.).

Клиенты моделей: `ingest/llm.py` — протокол `LLM.chat(system, user, *, temperature, max_tokens) -> str | None`; реализации
`GigaChatLLM` (OAuth + chat, TLS-проверка включена) и `OpenAICompatibleLLM`; `make_llm(settings)` возвращает `None`, если модель не настроена.

Оценка качества: `python -m app.cli evaluate --gold eval/gold_example.jsonl` считает точность по релевантности, тематике, значимости, сроку,
долю проверенных цитат, точность и полноту применимости по профилям, точность по уровням уверенности. `gold_example.jsonl` — три синтетических
примера; для реального замера нужны 50–100 актов, размеченных юристом.

Служебный метод `POST /api/admin/changes/{id}/analyze {"text": "...", "apply": false}` показывает результат разбора; `apply=true` записывает поля
в изменение (кроме `source_key, related, status, agency`) и сбрасывает `reviewed`.

## 12. Безопасность (что сделано)

- Пароли: scrypt (N=2^14, r=8, p=1), сравнение за константное время; токены сессий, кода подтверждения и сброса хранятся хешами.
- Cookie HttpOnly + SameSite=Lax (+ Secure в продакшене), CSRF-защита middleware, заголовки `nosniff`, `Referrer-Policy`, `Cache-Control: no-store`.
- Троттлинг входа, кодов и сброса; одинаковые ответы для существующего и несуществующего e-mail там, где это важно.
- Строгие Pydantic-схемы (`extra="forbid"`), валидация правил до выполнения, никакого `eval`.
- Изоляция данных компаний: все запросы идут через `Ctx` с `company_id` из членства, а не из параметров запроса.

## 13. Тесты

`pytest` (137 тестов, SQLite в памяти, `tests/conftest.py` даёт `env`, `owner`, `Api`; «сегодня» зафиксировано константой `TODAY`).
Покрытие: правила и безопасность, регистрация и компании, ленты и фильтры, действия, уведомления и расписание, Telegram (подставной клиент),
команда и роли, тарифы, загрузка актов, ИИ-разбор (подставная модель: проверка цитат, сроков, чисел, проходов, формата, оценки).
Сквозной тест в браузере: `BASE_URL=http://localhost:8000 node e2e/live_site.js` (26 шагов; нужен сервер в `DEMO_MODE` и `TELEGRAM_WEBHOOK_SECRET=hooksecret`).

## 14. Что не сделано и что проверять перед продакшеном

Не проверялось в среде разработки: реальные pravo.gov.ru, GigaChat и другие модели (качество промпта на живых актах не измерено),
извлечение кириллицы из PDF портала, SMTP, Bot API Telegram, PostgreSQL (ожидается из-за SQLAlchemy, но тесты идут на SQLite).

Обязательный список доработок:
1. Миграции (Alembic) вместо `create_all`.
2. Docker/развёртывание, обратный прокси с HTTPS, лимиты запросов на уровне прокси, резервные копии, мониторинг и логи.
3. Размещение БД в РФ и согласие на обработку персональных данных (152-ФЗ).
4. Настоящая оплата (сейчас заявки обрабатываются вручную).
5. Замер качества ИИ-разбора на 50–100 реальных актах и юридическая проверка карточек (`reviewed`) перед показом клиентам.
6. Не реализовано: смена e-mail, переключатель «показывать профиль команде», скрытие кнопок по ролям на фронтенде (сервер роли проверяет).
7. Старый путь `enrich(raw, llm)` (только по названию) оставлен рядом с новым разбором как запасной; при желании его можно упростить.
8. Полнотекстовый поиск при росте каталога (сейчас поиск в Python).
