# Telegram-бот OnlineContract

## 1. Краткое описание проекта

Telegram-бот на **aiogram 3.x** показывает пользователю открытые торговые процедуры и конкурсные листы с площадки [onlinecontract.ru](https://onlinecontract.ru) через публичный API `api.onlc.ru`. Пользователь выбирает категорию, получает карточки процедур с пагинацией и по кнопке может открыть позиции (конкурсный лист) по выбранной процедуре.

## 2. Стек технологий

| Область | Технологии |
|--------|------------|
| Язык | Python **3.11** |
| Telegram | **aiogram** 3.x, HTML parse mode |
| HTTP к API площадки | **httpx** (вызовы из asyncio через `asyncio.to_thread`) |
| БД | **SQLite**, **SQLAlchemy** 2.x (async), **aiosqlite**, **Pydantic** 2.x |
| Миграции | **Alembic** (схема поднимается при старте через `init_db_schema`) |

Зависимости, включая pytest: [`requirements.txt`](requirements.txt).

## 3. Архитектура решения

**Вкратце:** `bot.py` запускает `tgbot.app`: `Dispatcher`, middleware с одной сессией БД на update, обработчики команд и callback. Ответы API кэшируются в SQLite; тексты карточек формируются в `tgbot/ui/`. Клиент к `api.onlc.ru` — класс `OnlineContract` в `onlinecontract/client.py`. Команды владельца — `tgbot/admin.py`, запрет доступа — `tgbot/middleware.py`, сброс диалога — `tgbot/dialogs.py`, снимок базы — `jobs/backups.py`, рассылка пересечений с прайсом — `jobs/intersection_feed.py`.

Подробнее: [`docs/architecture.md`](docs/architecture.md), схема БД: [`docs/database.md`](docs/database.md).

### Схема потока данных

```mermaid
flowchart LR
  user[User] --> tg[Telegram]
  tg --> bot[bot.py]
  bot --> db[(SQLite)]
  bot --> oc[OnlineContract]
  oc --> api["api.onlc.ru"]
  bot --> fmt[tgbot/ui]
  fmt --> tg
```

## 4. Инструкция по запуску

1. Клонируйте репозиторий и перейдите в корень проекта.

2. Создайте виртуальное окружение (по желанию) и установите зависимости:

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Задайте токен бота от [@BotFather](https://t.me/BotFather). Удобно скопировать [`example.env`](example.env) в `.env` и заполнить `TOKEN`, либо экспортировать переменные:

   ```bash
   export TOKEN="ваш_токен"
   ```

4. В [`.env`](.env) задайте файловую базу, свой числовой Telegram id и период отправки файла базы (шаблон — [`example.env`](example.env)). Корневой `.env` читается при старте:

   ```bash
   DATABASE_URL=sqlite+aiosqlite:///./data/app.db
   ADMIN_IDS=123456789
   DB_BACKUP_INTERVAL_HOURS=24
   DB_BACKUP_ON_START=0
   MATCH_POLL_INTERVAL_MINUTES=15
   ```

   Без `DATABASE_URL` база в памяти: журнал не переживёт перезапуск, и файл базы не отправляется. Id можно узнать у [@userinfobot](https://t.me/userinfobot). Раз в указанное число часов владельцу приходит снимок SQLite, если с прошлой копии база изменилась. Срок хранится в базе, поэтому перезапуск сам по себе файл не присылает. `DB_BACKUP_ON_START=1` отправляет копию при старте, когда интервал уже вышел. Раз в `MATCH_POLL_INTERVAL_MINUTES` минут бот ищет новые конкурсные листы. Пересечение с прайсом уходит тем, кто нажал `/start` и не закрыт, и id из `ADMIN_IDS`, которых ещё нет в журнале.

5. Запустите бота из корня репозитория:

   ```bash
   python bot.py
   ```

## 5. Инструкция по тестированию

### Автотесты (pytest)

Установите зависимости и запустите тесты:

```bash
pip install -r requirements.txt
pytest
```

Каталог тестов: [`tests/`](tests/). Для импорта `bot` нужен `TOKEN` в окружении; в [`tests/conftest.py`](tests/conftest.py) для pytest задаётся тестовый placeholder. Настройки: [`pytest.ini`](pytest.ini).

Покрытие по файлам см. [`docs/testing.md`](docs/testing.md).

### Ручная проверка в Telegram

1. Запустите бота с реальным `TOKEN` (`python bot.py`).
2. В чате: `/start` — кнопки категорий; выбор категории — карточки; при большом списке — «Показать ещё»; кнопка позиций — конкурсный лист (при длинном тексте возможно несколько сообщений).
3. При недоступности API бот должен отвечать сообщением об ошибке, а не падать.
4. Владелец (`ADMIN_IDS`, личный чат): `/stats`, `/users`, `/user <id>`, `/ban`, `/unban`, `/reset <id>`, `/delete_user`. После `/ban` или подтверждённого `/delete_user` повторный `/start` отвечает «доступ закрыт». Пересечения с прайсом приходят сами, отдельной команды нет.

---

Дополнительно: конфигурация и секреты — [`docs/misc.md`](docs/misc.md); план работ — [`plans/README.md`](plans/README.md).
