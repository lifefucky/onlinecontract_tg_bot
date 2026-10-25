# Пакет `onlinecontract`

Доступ к **публичному REST API** площадки onlinecontract.ru (`api.onlc.ru`): список процедур и позиции по процедуре.

## Файлы

| Файл | Роль |
|------|------|
| [`__init__.py`](../onlinecontract/__init__.py) | Маркер пакета Python. |
| [`client.py`](../onlinecontract/client.py) | Константы URL, класс **`OnlineContract`**, синхронные HTTP-запросы через **`httpx`** (`api_get`). Тот же клиент вызывает [`matching/tenders.py`](../matching/tenders.py). |

## Реализация `OnlineContract`

- **Процедуры**: `GET` [`PROCEDURES_URL`](https://api.onlc.ru/purchases/v1/public/procedures) с query-параметрами (`limit`, `filters[categories]`, `include=owner`, сортировка и т.д.). Ответ нормализуется в список словарей с полями из **`procedures_kc`**, строковое поле **`owner`** (имя из вложенного объекта), **`link`** на карточку на `onlinecontract.ru`.
- **Позиции**: `GET` по шаблону **`POSITIONS_URL`** — `/procedures/{id}/positions`. Если по одной процедуре условия в строках совпадают, общий блок выносится в **`common_message`**; если вариантов несколько — часть полей дублируется в каждой позиции.
- **Ошибки**: при не-200, невалидном JSON или отсутствии/некорректном **`data`** методы возвращают **`None`** (вызывающий код в боте показывает сообщение пользователю). Пустой успешный список процедур — **`[]`**.
- **Поля строк** читаются через **`.get`**, чтобы не падать на неполных ответах API.

Запросы из [`tgbot/user.py`](../tgbot/user.py) вызываются внутри **`asyncio.to_thread`**, чтобы не блокировать цикл asyncio.
