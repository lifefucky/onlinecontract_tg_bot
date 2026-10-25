# База данных: SQLAlchemy, Pydantic, Alembic

Единая точка описания персистентного слоя. Код — в [`db/`](../db/), миграции — в [`alembic/`](../alembic/).

## Стек

| Компонент | Роль |
|-----------|------|
| **SQLAlchemy 2.x** (async) | ORM, `AsyncSession`, модели на [`DeclarativeBase`](../db/base.py) |
| **aiosqlite** | Асинхронный драйвер SQLite |
| **Pydantic v2** | Схемы чатов и пользователей в [`db/schemas/`](../db/schemas/); ORM в обработчики Telegram не отдаётся |
| **Alembic** | Версионирование схемы; async-окружение — [`alembic/env.py`](../alembic/env.py) |

Зависимости перечислены в [`requirements.txt`](../requirements.txt).

## Конфигурация URL

Переменная окружения **`DATABASE_URL`** задаётся в процессе (как и **`TOKEN`**); шаблон — [`example.env`](../example.env). Значение по умолчанию и вспомогательные проверки — [`misc/db_url.py`](../misc/db_url.py).

| Режим | Типичный URL | Примечание |
|-------|----------------|------------|
| In-memory | `sqlite+aiosqlite:///:memory:` | Один общий пул (**StaticPool** + `check_same_thread=False`), иначе у каждого соединения была бы своя пустая БД |
| Файл | `sqlite+aiosqlite:///./data/app.db` | Каталог `data/` создаётся при старте при необходимости; данные переживают перезапуск процесса |

## Инициализация схемы

[`db/session.py`](../db/session.py), функция **`init_db_schema()`**:

- для **in-memory** — `Base.metadata.create_all` на движке приложения. Признак in-memory смотрится по URL после decode: строка движка может содержать `%3Amemory%3A` вместо `:memory:`;
- для **файлового** (и прочих не in-memory URL) — **`alembic upgrade head`** в отдельном потоке (`asyncio.to_thread`), чтобы не блокировать event loop.

Глобальные **`get_engine()`** / **`get_session_maker()`** кэшируют движок и фабрику сессий; для тестов есть **`bind_engine()`** и **`reset_engine()`**.

## Схема таблиц

| Таблица | Назначение |
|---------|------------|
| **`telegram_chats`** | Чат Telegram: внутренний `id`, `telegram_chat_id` (unique), `chat_type`, `title`, метки времени |
| **`telegram_users`** | Человек, нажавший `/start`, либо «надгробие» запрета. `telegram_user_id` (unique), имя, username, язык, `is_banned`, `bot_blocked`, `profile_cleared`, `started_at` (пусто, если id только запретили и бота он не запускал), `last_seen_at`, `banned_at`, `ban_reason` |
| **`bot_message_tracks`** | Учёт `telegram_message_id` по чату и **scope** (`procedures`, `positions`, `welcome`) для удаления старых сообщений при смене категории, новых «Позициях», `/start` и сбросе диалога; уникальность `(chat_id, telegram_message_id)` |
| **`api_cache_entries`** | Кэш JSON ответов `get_procedures` по ключу категории (`cache_key`); опционально `expires_at` |
| **`shown_procedures`** | Уже отправленные пересечения: `telegram_chat_id`, `procedure_id`, `content_hash` (состав на момент отправки), `bucket` (`fit` — «подходит», `borderline` — «пограничный»), `posted_at`. Повтор в тот же чат не отправляется, если для пары `(telegram_chat_id, procedure_id)` есть любая строка. В схеме уникальность по-прежнему `(telegram_chat_id, procedure_id, content_hash)` |
| **`match_offers`** | Суждение, которое ушло на карточку владельца: `procedure_id`, `content_hash`, `level`, `lines_json` (строки в категориях), `closeness`, `in_scope_count`, `line_count`. Кнопка «Подходит» / «Не подходит» находит строку по началу хэша |
| **`match_labels`** | Ответ владельца и будущее золото для порога: процедура, уровень отправленного суждения, те же строки, `closeness`, решение (`fit` или `reject`), `telegram_user_id`, `answered_at`. Одна строка на `(procedure_id, telegram_user_id, content_hash)`; повторный ответ её обновляет. Порог по этим строкам сам не подстраивается |
| **`app_kv_store`** | Ключ–значение JSON. Флаг **`dialog_reset_all_v1`** (`true`) — однократный сброс диалогов во всех известных чатах при первом старте этой версии. Ключ **`match_active_procedure_ids`** — JSON-массив id активных процедур, уже взятых в проверку пересечения. Ключ **`match_poll_state`** — отпечаток файла прайса и версия правил; при смене любого из них активный список классифицируется заново. Ключ **`match_price_stale_warned_on`** — дата последнего предупреждения, что прайс старше `PRICE_MAX_AGE_DAYS`. Ключ **`match_catalog_embed_warned_on`** — дата последнего предупреждения, что эмбеддинги всего прайса ещё не посчитаны. Ключ **`db_backup_state`** — время последней попытки снимка и хэш содержимого базы без этой строки и без полей `updated_at`; пока хэш тот же, файл владельцам не отправляется |

ORM-модели: [`db/models/`](../db/models/). Перечисление scope: [`db/enums.py`](../db/enums.py) (`MessageTrackScope`).

## Репозитории и Pydantic

- Репозитории — тонкий слой над `AsyncSession`: [`db/repositories/chats.py`](../db/repositories/chats.py), [`users.py`](../db/repositories/users.py), [`message_tracks.py`](../db/repositories/message_tracks.py), [`api_cache.py`](../db/repositories/api_cache.py), [`kv.py`](../db/repositories/kv.py), [`shown.py`](../db/repositories/shown.py), [`labels.py`](../db/repositories/labels.py).
- Pydantic-модели — [`db/schemas/`](../db/schemas/): чаты (`ChatUpsert`, `ChatRow`) и пользователи (`UserStart`, `UserRow`, `UserStats`, `UserPage`). Кэш, треки сообщений, kv и `shown_procedures` этими схемами не пользуются.

## Миграции Alembic

- Конфиг: [`alembic.ini`](../alembic.ini); ревизии: [`alembic/versions/`](../alembic/versions/).
- URL в `env.py` подставляется из **`get_database_url()`**, путь к проекту добавляется в `sys.path`, чтобы импортировать пакет `db` без запуска бота.
- Ручной прогон из корня репозитория (после задания `DATABASE_URL` под файловый SQLite):

```bash
alembic upgrade head
```

Импорт **`misc.db_url`** не тянет **`TOKEN`** (см. ленивый `TgKeys` в [`misc/__init__.py`](../misc/__init__.py)), поэтому Alembic можно вызывать без переменной `TOKEN`.

## Связь с ботом

В [`bot.py`](../bot.py): middleware с одной **`AsyncSession`** на update и commit при успехе; внутри него middleware доступа. Кэш списков процедур, журнал пользователей и трекинг id сообщений идут через репозитории. Полная очистка кэша API на **`/start`** — см. `api_cache.clear_all`. Бан, удаление профиля и `/reset` кэш не трогают. Поведение UI (удаление сообщений, pin приветствия, однократный сброс диалогов) согласовано с записями в `bot_message_tracks` и флагом в `app_kv_store`. Файл базы уходит владельцам из [`backups.py`](../backups.py), когда с прошлой попытки прошёл интервал и содержимое изменилось.

## Тесты

Автотесты репозиториев: [`tests/test_db_repositories.py`](../tests/test_db_repositories.py), журнал и однократный сброс диалогов — [`tests/test_users.py`](../tests/test_users.py) (изолированный in-memory движок в фикстуре). Общие правила pytest — [`testing.md`](testing.md).

## Сопровождение документации

При изменении схемы, способа миграций или контрактов репозиториев обновляйте этот файл; в [`architecture.md`](architecture.md) достаточно краткой отсылки на персистентный слой без повторения деталей.
