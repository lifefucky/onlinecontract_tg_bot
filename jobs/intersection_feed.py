"""Poll for new competitive sheets and post price-list intersections to started users."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from datetime import date
from os import environ

from aiogram import Bot
from sqlalchemy.exc import IntegrityError

from db.repositories import kv, labels, shown, users
from db.session import get_session_maker
from jobs.embed_lock import catalog_embed_lock
from jobs.match_cards import BUCKET_FIT, BUCKET_TITLE, _card_keyboard, content_hash, format_match_card
from misc.admins import get_admin_ids
from misc.load_env import load_project_env
from tgbot.ui.messages import split_telegram_messages

logger = logging.getLogger(__name__)

SEEN_PROCEDURES_KEY = "match_active_procedure_ids"
MATCH_STATE_KEY = "match_poll_state"
PRICE_WARNED_KEY = "match_price_stale_warned_on"
PRICE_MISSING_NOTICE_KEY = "match_price_missing_notice"
CATALOG_EMBED_WARNED_KEY = "match_catalog_embed_warned_on"
_MISSING_PRICE_TEXT = "Не найден файл прайса. Эта проверка пропущена."
DEFAULT_MATCH_POLL_INTERVAL_MINUTES = 15.0
MIN_ACTIVE_LIST_RATIO = 0.5
SEND_GAP_SECONDS = 1.05


def match_poll_interval_seconds() -> float:
    load_project_env()
    raw = environ.get("MATCH_POLL_INTERVAL_MINUTES", "").strip()
    if not raw:
        return DEFAULT_MATCH_POLL_INTERVAL_MINUTES * 60
    try:
        minutes = float(raw.replace(",", "."))
    except ValueError:
        logging.warning("MATCH_POLL_INTERVAL_MINUTES=%r is not a number; using 15", raw)
        return DEFAULT_MATCH_POLL_INTERVAL_MINUTES * 60
    if minutes <= 0:
        logging.warning("MATCH_POLL_INTERVAL_MINUTES=%s is not positive; using 15", minutes)
        return DEFAULT_MATCH_POLL_INTERVAL_MINUTES * 60
    return minutes * 60


def resolve_baseline(stored: set[int] | None, cached: set[int] | None) -> tuple[set[int], bool]:
    """Return ids already reviewed, and whether that baseline must be saved before the next fetch.

    A stored snapshot wins over the procedure-list file, including an empty snapshot.
    That keeps a failed check from being skipped after the fetch rewrites the file.
    """
    if stored is not None:
        return stored, False
    if cached:
        return set(cached), True
    return set(), True


def parse_procedure_ids(payload: object) -> set[int]:
    if not isinstance(payload, list):
        raise ValueError("procedure id snapshot is not a list")
    ids: set[int] = set()
    for item in payload:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("procedure id snapshot contains a non-integer")
        ids.add(item)
    return ids


def procedure_ids(procedures: list[dict]) -> set[int]:
    ids: set[int] = set()
    for row in procedures:
        procedure_id = row.get("id")
        if isinstance(procedure_id, bool) or not isinstance(procedure_id, int):
            continue
        ids.add(procedure_id)
    return ids


def should_rematch_all(
    stored_fingerprint: str | None,
    stored_rules: str | None,
    fingerprint: str,
    rules: str,
    *,
    has_snapshot: bool,
) -> bool:
    """The whole active list is classified again when the price file or the rules changed.

    A missing snapshot is the first-run baseline, not a rules change.
    A snapshot without a stored fingerprint is the upgrade from the old id-only snapshot.
    """
    if not has_snapshot:
        return False
    if stored_fingerprint is None or stored_rules is None:
        return True
    return stored_fingerprint != fingerprint or stored_rules != rules


def select_procedures_to_match(
    procedures: list[dict],
    seen: set[int],
    *,
    rematch_all: bool,
    stale_ids: set[int],
) -> list[dict]:
    """New ids, sheets whose positions are due, or the whole list when rules or the price changed."""
    chosen: list[dict] = []
    taken: set[int] = set()
    for row in procedures:
        procedure_id = row.get("id")
        if isinstance(procedure_id, bool) or not isinstance(procedure_id, int):
            continue
        if procedure_id in taken:
            continue
        if not rematch_all and procedure_id in seen and procedure_id not in stale_ids:
            continue
        taken.add(procedure_id)
        chosen.append(row)
    return chosen


def select_new_procedures(procedures: list[dict], seen: set[int]) -> list[dict]:
    """Procedures whose id is not in the previous active snapshot, in list order."""
    fresh: list[dict] = []
    taken: set[int] = set()
    for row in procedures:
        procedure_id = row.get("id")
        if isinstance(procedure_id, bool) or not isinstance(procedure_id, int):
            continue
        if procedure_id in taken or procedure_id in seen:
            continue
        taken.add(procedure_id)
        fresh.append(row)
    return fresh


def active_list_trusted(previous: set[int], current: set[int]) -> bool:
    """Reject an empty or sharply shorter list so a truncated page cannot reset the snapshot."""
    if not current:
        return False
    if previous and len(current) < len(previous) * MIN_ACTIVE_LIST_RATIO:
        return False
    return True


def next_seen_ids(active_ids: set[int], failed_ids: set[int]) -> set[int]:
    """Ids to remember. Failures stay out so the next poll checks them again."""
    return active_ids - failed_ids


def select_unseen(records: list[dict], known: set[int]) -> list[dict]:
    """Keep intersections this chat has never been sent.

    ``known`` is procedure ids. A new line composition is still the same sheet.
    """
    pending: list[dict] = []
    taken: set[int] = set()
    for record in records:
        if record.get("bucket") not in BUCKET_TITLE:
            continue
        procedure_id = int(record["procedure_id"])
        if procedure_id in known or procedure_id in taken:
            continue
        taken.add(procedure_id)
        pending.append(record)
    return pending


def _id_text(procedure_ids: set[int]) -> str:
    return ", ".join(str(procedure_id) for procedure_id in sorted(procedure_ids))


async def notify_match_admins(bot: Bot, text: str) -> None:
    """Send one short failure note to every admin.

    ``text`` is Russian and must not include a traceback, token, or API key.
    A rejected notice is logged once per chat and is not retried.
    """
    try:
        admin_ids = sorted(get_admin_ids())
    except Exception:
        logger.exception("match poll admin notice skipped: ADMIN_IDS is unreadable")
        return
    if not admin_ids:
        logger.info("match poll admin notice skipped: ADMIN_IDS is empty")
        return
    notice = f"⚠️ {text}"
    logger.info("match poll admin notice: %s", text)
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, notice)
        except Exception:
            logger.exception("match poll admin notice failed chat=%s", admin_id)


def _missing_notice_rows(payload: object) -> list[dict[str, int]]:
    """``{chat_id, message_id}`` rows. One row per chat; junk entries are dropped."""
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, int]] = []
    seen: set[int] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        chat_id = item.get("chat_id")
        message_id = item.get("message_id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            continue
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            continue
        if chat_id in seen:
            continue
        seen.add(chat_id)
        rows.append({"chat_id": chat_id, "message_id": message_id})
    return rows


async def _load_missing_price_notices() -> list[dict[str, int]]:
    maker = get_session_maker()
    async with maker() as session:
        try:
            payload = await kv.get_json(session, PRICE_MISSING_NOTICE_KEY)
        except json.JSONDecodeError:
            await kv.delete_key(session, PRICE_MISSING_NOTICE_KEY)
            await session.commit()
            return []
    return _missing_notice_rows(payload)


async def _save_missing_price_notices(rows: list[dict[str, int]]) -> None:
    maker = get_session_maker()
    async with maker() as session:
        if rows:
            await kv.set_json(session, PRICE_MISSING_NOTICE_KEY, rows)
        else:
            await kv.delete_key(session, PRICE_MISSING_NOTICE_KEY)
        await session.commit()


async def _notify_missing_price(bot: Bot) -> None:
    """Post the missing-price note once per admin for this outage.

    A chat that already has a stored message is not notified again. A failed
    send is retried on a later poll. The messages are not tracked for ``/start``.
    """
    try:
        admin_ids = sorted(get_admin_ids())
    except Exception:
        logger.exception("match poll missing price notice skipped: ADMIN_IDS is unreadable")
        return
    try:
        stored = await _load_missing_price_notices()
    except Exception:
        logger.exception("match poll missing price notice was not read")
        return
    known = {row["chat_id"] for row in stored}
    fresh = list(stored)
    notice = f"⚠️ {_MISSING_PRICE_TEXT}"
    for admin_id in admin_ids:
        if admin_id in known:
            continue
        try:
            message = await bot.send_message(admin_id, notice)
        except Exception as exc:
            logger.warning(
                "missing price notice failed chat=%s (%s)",
                admin_id,
                type(exc).__name__,
            )
            continue
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            logger.warning("missing price notice has no message id chat=%s", admin_id)
            continue
        fresh.append({"chat_id": admin_id, "message_id": message_id})
        known.add(admin_id)
    if fresh == stored:
        return
    try:
        await _save_missing_price_notices(fresh)
    except Exception:
        logger.exception("match poll missing price notice was not saved")


async def _drop_missing_price_notice(bot: Bot) -> None:
    """Delete the missing-price notes once the file is back, then forget them.

    A failed delete is logged and does not keep the key. The poll continues,
    and a finished outage is not posted again.
    """
    try:
        stored = await _load_missing_price_notices()
    except Exception:
        logger.exception("match poll missing price notice was not read")
        return
    if not stored:
        return
    for row in stored:
        try:
            await bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
        except Exception as exc:
            logger.warning(
                "missing price notice delete failed chat=%s message=%s (%s)",
                row["chat_id"],
                row["message_id"],
                type(exc).__name__,
            )
    try:
        await _save_missing_price_notices([])
    except Exception:
        logger.exception("match poll missing price notice key was not cleared")


def _cached_procedure_ids() -> set[int] | None:
    from matching.tenders import cached_procedure_ids

    return cached_procedure_ids()


def _load_new_procedures(seen: set[int]) -> tuple[list[dict], set[int]]:
    from matching.config import POSITION_REFRESH_SECONDS
    from matching.tenders import fetch_procedures, stale_position_ids

    procedures = fetch_procedures(refresh=True, progress=False)
    current = procedure_ids(procedures)
    stale = set(stale_position_ids(sorted(current), max_age_seconds=POSITION_REFRESH_SECONDS))
    chosen = select_procedures_to_match(procedures, seen, rematch_all=False, stale_ids=stale)
    return chosen, current


def _load_all_procedures() -> tuple[list[dict], set[int]]:
    from matching.tenders import fetch_procedures

    procedures = fetch_procedures(refresh=True, progress=False)
    chosen = select_procedures_to_match(procedures, set(), rematch_all=True, stale_ids=set())
    return chosen, procedure_ids(procedures)


def _match_procedures(procedures: list[dict]) -> tuple[list[dict], list[dict], set[int]]:
    from matching.config import POSITION_REFRESH_SECONDS
    from matching.run import classify_procedures
    from matching.tenders import position_error_ids, stale_position_ids

    ids = [int(row["id"]) for row in procedures]
    refresh_ids = set(stale_position_ids(ids, max_age_seconds=POSITION_REFRESH_SECONDS))
    refresh_ids.update(position_error_ids(ids))
    fit, borderline, _decisions = classify_procedures(procedures, refresh_ids=refresh_ids)
    errors = position_error_ids(ids)
    if errors:
        fit = [record for record in fit if int(record["procedure_id"]) not in errors]
        borderline = [record for record in borderline if int(record["procedure_id"]) not in errors]
    return fit, borderline, errors


def _live_match_state() -> tuple[str, str]:
    from matching.catalog import find_price_xls, price_fingerprint
    from matching.config import rules_version

    path = find_price_xls()
    return price_fingerprint(path), rules_version()


def _stale_price_text(upload: object | None = None) -> str | None:
    from matching.catalog import find_price_xls, stale_price_warning
    from matching.config import PRICE_MAX_AGE_DAYS

    return stale_price_warning(
        find_price_xls(),
        max_age_days=PRICE_MAX_AGE_DAYS,
        upload=upload,
    )


async def load_seen_procedure_ids(session) -> set[int] | None:
    try:
        payload = await kv.get_json(session, SEEN_PROCEDURES_KEY)
    except json.JSONDecodeError as exc:
        raise ValueError("procedure id snapshot is not valid json") from exc
    if payload is None:
        return None
    return parse_procedure_ids(payload)


async def save_seen_procedure_ids(session, procedure_ids_seen: set[int]) -> None:
    await kv.set_json(session, SEEN_PROCEDURES_KEY, sorted(procedure_ids_seen))


async def _read_seen() -> set[int] | None:
    maker = get_session_maker()
    async with maker() as session:
        return await load_seen_procedure_ids(session)


async def _write_seen(procedure_ids_seen: set[int]) -> None:
    maker = get_session_maker()
    async with maker() as session:
        await save_seen_procedure_ids(session, procedure_ids_seen)
        await session.commit()


async def _read_match_state() -> tuple[str | None, str | None]:
    maker = get_session_maker()
    async with maker() as session:
        try:
            payload = await kv.get_json(session, MATCH_STATE_KEY)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(payload, dict):
        return None, None
    fingerprint = payload.get("price_fingerprint")
    rules = payload.get("rules_version")
    return (
        fingerprint if isinstance(fingerprint, str) and fingerprint else None,
        rules if isinstance(rules, str) and rules else None,
    )


async def _write_match_state(fingerprint: str, rules: str) -> None:
    maker = get_session_maker()
    async with maker() as session:
        await kv.set_json(
            session,
            MATCH_STATE_KEY,
            {"price_fingerprint": fingerprint, "rules_version": rules},
        )
        await session.commit()


async def _notify_once_today(bot: Bot, key: str, text: str) -> None:
    today = date.today().isoformat()
    maker = get_session_maker()
    async with maker() as session:
        try:
            last = await kv.get_json(session, key)
        except json.JSONDecodeError:
            last = None
        if last == today:
            return
        await kv.set_json(session, key, today)
        await session.commit()
    await notify_match_admins(bot, text)


async def _read_price_upload() -> object | None:
    from matching.catalog import PRICE_UPLOAD_KEY

    maker = get_session_maker()
    async with maker() as session:
        try:
            return await kv.get_json(session, PRICE_UPLOAD_KEY)
        except json.JSONDecodeError:
            return None


async def _warn_stale_price(bot: Bot) -> None:
    """Tell owners, at most once a day, when the price file is older than the configured age.

    A ``price_upload`` record that names the live file supplies the day from the
    original filename. A missing or mismatched record keeps the file's own day.
    """
    upload = None
    try:
        upload = await _read_price_upload()
    except Exception:
        logger.exception("price upload record was not read")
    try:
        warning = await asyncio.to_thread(_stale_price_text, upload)
    except FileNotFoundError:
        return
    if not warning:
        return
    await _notify_once_today(bot, PRICE_WARNED_KEY, warning)


def _records_for_chat(records: list[dict], *, for_admin: bool) -> list[dict]:
    if for_admin:
        return records
    return [record for record in records if record.get("bucket") == BUCKET_FIT]


async def _save_offers(records: list[dict]) -> None:
    maker = get_session_maker()
    async with maker() as session:
        for record in records:
            if record.get("bucket") not in BUCKET_TITLE:
                continue
            await labels.save_offer(session, record, content_hash=content_hash(record))
        await session.commit()


async def match_recipient_ids(admin_ids: Iterable[int] | None = None) -> list[int]:
    """Private chats for a new card: started users, plus admin ids that have no row."""
    if admin_ids is None:
        admin_ids = get_admin_ids()
    maker = get_session_maker()
    async with maker() as session:
        return await users.list_match_recipient_ids(session, extra_ids=admin_ids)


async def post_match_records(bot: Bot, records: list[dict]) -> tuple[int, set[int]]:
    """Send intersections. Return how many cards were delivered and which ids failed."""
    if not records:
        return 0, set()
    admin_ids = get_admin_ids()
    recipients = await match_recipient_ids(admin_ids)
    if not recipients:
        logger.info("match poll skipped posts: nobody to notify")
        return 0, {int(record["procedure_id"]) for record in records}
    await _save_offers(records)
    failed: set[int] = set()
    send_failed: set[int] = set()
    sent = 0
    maker = get_session_maker()
    for chat_id in recipients:
        async with maker() as session:
            known = await shown.known_keys(session, chat_id)
        for_admin = chat_id in admin_ids
        pending = select_unseen(_records_for_chat(records, for_admin=for_admin), known)
        if not pending:
            logger.info("match poll nothing new chat=%s", chat_id)
            continue
        if for_admin:
            fit_n = sum(1 for record in pending if record.get("bucket") == BUCKET_FIT)
            header = f"Пересечения с прайсом: подходит {fit_n}, пограничных {len(pending) - fit_n}."
            try:
                await bot.send_message(chat_id, header)
            except Exception:
                logger.exception("match poll header failed chat=%s", chat_id)
                pending_ids = {int(record["procedure_id"]) for record in pending}
                failed.update(pending_ids)
                send_failed.update(pending_ids)
                continue
            await asyncio.sleep(SEND_GAP_SECONDS)
        for record in pending:
            procedure_id = int(record["procedure_id"])
            digest = content_hash(record)
            chunks = split_telegram_messages(format_match_card(record, for_admin=for_admin))
            try:
                for index, chunk in enumerate(chunks):
                    markup = (
                        _card_keyboard(procedure_id, for_admin=for_admin, content_hash=digest)
                        if index == len(chunks) - 1
                        else None
                    )
                    await bot.send_message(chat_id, chunk, reply_markup=markup)
                    if index + 1 < len(chunks):
                        await asyncio.sleep(SEND_GAP_SECONDS)
                async with maker() as session:
                    try:
                        await shown.mark_shown(
                            session,
                            telegram_chat_id=chat_id,
                            procedure_id=procedure_id,
                            content_hash=digest,
                            bucket=str(record.get("bucket") or ""),
                        )
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
            except Exception:
                logger.exception(
                    "match poll send failed chat=%s procedure=%s",
                    chat_id,
                    procedure_id,
                )
                failed.add(procedure_id)
                send_failed.add(procedure_id)
                continue
            sent += 1
            logger.info(
                "match poll sent chat=%s procedure=%s bucket=%s",
                chat_id,
                procedure_id,
                record.get("bucket"),
            )
            await asyncio.sleep(SEND_GAP_SECONDS)
    if send_failed:
        await notify_match_admins(
            bot,
            f"Не удалось отправить пересечения. Процедуры: {_id_text(send_failed)}.",
        )
    return sent, failed


async def poll_new_matches(bot: Bot) -> int:
    """Classify new sheets, and the whole active list when the price or the rules changed."""
    if not await match_recipient_ids():
        logger.info("match poll skipped: nobody to notify")
        return 0
    try:
        stored = await _read_seen()
    except ValueError:
        logger.exception("match poll snapshot is unreadable; this tick is skipped")
        await notify_match_admins(
            bot,
            "Снимок активных процедур не читается. Эта проверка пропущена.",
        )
        return 0
    try:
        fingerprint, rules = await asyncio.to_thread(_live_match_state)
    except FileNotFoundError:
        await _notify_missing_price(bot)
        return 0
    except Exception:
        logger.exception("match poll price state failed")
        await notify_match_admins(bot, "Не удалось прочитать прайс. Эта проверка пропущена.")
        return 0
    await _drop_missing_price_notice(bot)
    stored_fp, stored_rules = await _read_match_state()
    rematch = should_rematch_all(
        stored_fp,
        stored_rules,
        fingerprint,
        rules,
        has_snapshot=stored is not None,
    )
    cached = await asyncio.to_thread(_cached_procedure_ids) if stored is None else None
    seen, persist = resolve_baseline(stored, cached)
    if persist:
        await _write_seen(seen)
        await _write_match_state(fingerprint, rules)
        rematch = False
        if seen:
            logger.info("match poll baseline %s procedures", len(seen))
        else:
            logger.info("match poll has no baseline; the current active list will be checked")
    try:
        if rematch:
            fresh, current = await asyncio.to_thread(_load_all_procedures)
        else:
            fresh, current = await asyncio.to_thread(_load_new_procedures, seen)
    except Exception as exc:
        logger.exception("match poll procedure list failed")
        if "truncated" in str(exc).lower():
            note = "Список конкурсных листов оборвался на неполной странице. Эта проверка пропущена."
        else:
            note = "Не удалось получить список конкурсных листов. Эта проверка пропущена."
        await notify_match_admins(bot, note)
        return 0
    if not active_list_trusted(seen, current):
        logger.warning(
            "match poll ignored active=%s previous=%s; the list looks truncated",
            len(current),
            len(seen),
        )
        await notify_match_admins(
            bot,
            "Список конкурсных листов пуст или выглядит обрезанным: "
            f"сейчас {len(current)}, в снимке было {len(seen)}. Снимок не изменён.",
        )
        return 0
    await _warn_stale_price(bot)
    if not fresh:
        await _write_seen(current)
        await _write_match_state(fingerprint, rules)
        logger.info("match poll no new procedures active=%s", len(current))
        return 0
    logger.info(
        "match poll classify=%s rematch=%s ids=%s",
        len(fresh),
        rematch,
        [int(row["id"]) for row in fresh],
    )
    try:
        # Same lock as the owner's «Посчитать» button: that call rewrites the embedding cache.
        async with catalog_embed_lock:
            fit, borderline, position_errors = await asyncio.to_thread(_match_procedures, fresh)
    except (Exception, SystemExit) as exc:
        logger.exception("match poll failed while matching")
        from matching.errors import CatalogEmbedRequired

        if isinstance(exc, CatalogEmbedRequired):
            await _notify_once_today(
                bot,
                CATALOG_EMBED_WARNED_KEY,
                "В кэше нет эмбеддингов всего прайса. Запрос к модели не отправлен. "
                "Один раз: MATCH_ALLOW_CATALOG_EMBED=1 python -m matching.run --embed-catalog",
            )
            return 0
        await notify_match_admins(
            bot,
            "Не удалось сопоставить листы с прайсом. "
            f"Процедуры: {_id_text({int(row['id']) for row in fresh})}.",
        )
        return 0
    if position_errors:
        logger.warning("match poll position fetch failed ids=%s", sorted(position_errors))
        await notify_match_admins(
            bot,
            f"Не удалось скачать позиции для процедур: {_id_text(set(position_errors))}.",
        )
    records = fit + borderline
    logger.info("match poll intersections fit=%s borderline=%s", len(fit), len(borderline))
    sent, post_failed = await post_match_records(bot, records)
    failed = position_errors | post_failed
    await _write_seen(next_seen_ids(current, failed))
    await _write_match_state(fingerprint, rules)
    logger.info("match poll sent=%s retry=%s", sent, len(failed))
    return sent


async def match_poll_loop(bot: Bot) -> None:
    """Check for new sheets soon after start, then every interval, until the task is cancelled."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    interval = match_poll_interval_seconds()
    logger.info("match poll every %s min", interval / 60)
    while True:
        started = time.monotonic()
        try:
            await poll_new_matches(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("match poll failed")
            await notify_match_admins(
                bot,
                "Проверка пересечений завершилась с ошибкой. Следующая попытка будет по расписанию.",
            )
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)
