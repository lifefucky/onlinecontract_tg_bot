"""Read the price list and attach a product-kind category to every position."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

import xlrd

from matching import config
from matching.attributes import normalize
from matching.categories import OTHER, category_of

_BOILER = (
    "не является публичной",
    "телефон",
    "e-mail",
    "email",
    "оферт",
    "прайс-лист",
    "прайс лист",
    "группа компаний",
    "опт-снаб",
    "уточняй",
    "скидки не распространяются",
    "данный прайс",
)

_HEADER_EXACT = {
    "наименование",
    "наименование продукции",
    "наименование товара",
    "название",
    "номенклатура",
    "описание",
    "товар",
    "товары (работы, услуги)",
    "штрихкод",
    "штрих-код",
    "артикул",
    "цена",
    "фотография товара",
}

_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?!\d)")

# ``app_kv_store``: accepted upload. The stored file is ``price-list.xls``, so the
# age warning reads the day from ``original_name`` (or ``uploaded_at``).
PRICE_UPLOAD_KEY = "price_upload"
# ``app_kv_store``: one unconfirmed upload. Cleared by «Заменить» and «Отмена».
PRICE_PENDING_KEY = "price_pending"
CANONICAL_PRICE_NAME = "price-list.xls"


@dataclass
class Sku:
    name: str
    name_norm: str
    sheet: str
    ean: str | None
    price: float | None
    category: str


def _configured_price_path() -> Path | None:
    """``PRICE_LIST_PATH`` when set: absolute, or relative to the repository root."""
    raw = os.environ.get("PRICE_LIST_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config.REPO_ROOT / path
    return path


def find_price_xls() -> str:
    """Resolve the shared price list.

    ``PRICE_LIST_PATH`` (absolute or relative to the repository) wins, and a
    missing file still raises. Otherwise ``data/price-list.xls`` when it is a
    file. Legacy ``docs/research`` notebooks are no longer consulted at runtime.
    """
    configured = _configured_price_path()
    if configured is not None:
        if not configured.is_file():
            raise FileNotFoundError(
                f"PRICE_LIST_PATH is set but the file is missing: {configured}"
            )
        return str(configured)
    canonical = config.DATA_DIR / CANONICAL_PRICE_NAME
    if canonical.is_file():
        return str(canonical)
    raise FileNotFoundError(
        "price list xls not found; set PRICE_LIST_PATH or upload it via /price"
    )


def price_list_destination() -> Path:
    """File ``/price`` replaces. It does not have to exist yet.

    A set ``PRICE_LIST_PATH`` is that file. Otherwise ``data/price-list.xls``.
    The rollback copy is always ``data/price-list.prev.xls``, not a second copy
    of the canonical name.
    """
    return _configured_price_path() or (config.DATA_DIR / CANONICAL_PRICE_NAME)


def pending_price_path() -> Path:
    """Single unconfirmed upload. Never chosen by :func:`find_price_xls`."""
    return config.DATA_DIR / "price-list.pending.xls"


def previous_price_path() -> Path:
    """Rollback copy of the destination, taken before a confirmed replace."""
    return config.DATA_DIR / "price-list.prev.xls"


def _same_price_path(left: str | Path, right: str | Path) -> bool:
    a = Path(left)
    b = Path(right)
    if a == b:
        return True
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def replace_price_file(pending: Path, destination: Path) -> None:
    """Copy ``destination`` to the rollback file when it exists, then move ``pending`` onto it.

    The move is ``os.replace``. Across devices the pending file is copied next
    to the destination and that copy is renamed into place, so the live file
    is not truncated if the copy fails.
    """
    if not pending.is_file():
        raise FileNotFoundError(pending)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and not _same_price_path(destination, previous_price_path()):
        previous = previous_price_path()
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(destination, previous)
    _move_into_place(pending, destination)


def _move_into_place(src: Path, dest: Path) -> None:
    try:
        os.replace(src, dest)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    temporary = dest.with_name(f".{dest.name}.{os.getpid()}.incoming")
    try:
        shutil.copyfile(src, temporary)
        os.replace(temporary, dest)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink(missing_ok=True)
    try:
        src.unlink()
    except OSError:
        logging.warning("price pending remove failed after replace")


def price_fingerprint(path: str | Path) -> str:
    """Content hash. A new file, even with the same name, changes the snapshot."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _day_from_name(name: str, today: date) -> date | None:
    """Day written as ``26.04`` or ``26.04.2026``. A date with no year rolls back if it is still ahead."""
    match = _DATE_RE.search(name)
    if not match:
        return None
    day_n = int(match.group(1))
    month_n = int(match.group(2))
    year_n = int(match.group(3)) if match.group(3) else today.year
    try:
        found = date(year_n, month_n, day_n)
    except ValueError:
        return None
    if match.group(3) is None and found > today:
        try:
            found = date(year_n - 1, month_n, day_n)
        except ValueError:
            return None
    return found


def price_file_day(path: str | Path, *, today: date | None = None) -> date:
    """Date from the filename (``26.04`` or ``26.04.2026``), otherwise the file mtime."""
    today = today or date.today()
    found = _day_from_name(Path(path).name, today)
    if found is not None:
        return found
    stamp = Path(path).stat().st_mtime
    return datetime.fromtimestamp(stamp).date()


def _day_from_timestamp(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def price_upload_day(path: str | Path, upload: object | None, *, today: date | None = None) -> date:
    """Day for the stale warning.

    When ``upload`` is the ``price_upload`` record and its ``path`` is this file,
    the day comes from ``original_name``. A name with no date uses ``uploaded_at``.
    A missing record, or a path that does not match, keeps :func:`price_file_day`.
    """
    today = today or date.today()
    if isinstance(upload, dict):
        stored = upload.get("path")
        if isinstance(stored, str) and stored and _same_price_path(path, stored):
            original = upload.get("original_name")
            if isinstance(original, str):
                found = _day_from_name(original, today)
                if found is not None:
                    return found
            uploaded = _day_from_timestamp(upload.get("uploaded_at"))
            if uploaded is not None:
                return uploaded
    return price_file_day(path, today=today)


def _shown_price_name(path: str | Path, upload: object | None) -> str:
    name = Path(path).name
    if isinstance(upload, dict):
        stored = upload.get("path")
        original = upload.get("original_name")
        if (
            isinstance(stored, str)
            and isinstance(original, str)
            and original.strip()
            and _same_price_path(path, stored)
        ):
            name = original.strip()
    return escape(name)


def price_count_dropped_sharply(previous: int, new: int) -> bool:
    """True when the live catalog is non-empty and the new file has strictly fewer than half its positions."""
    return previous > 0 and new * 2 < previous


def stale_price_warning(
    path: str | Path,
    *,
    today: date | None = None,
    max_age_days: int,
    upload: object | None = None,
) -> str | None:
    """Russian note for owners, or None when the file is still fresh enough.

    ``upload`` is the ``price_upload`` record. When it names this file, the day
    and the shown filename come from the original upload, not from ``price-list.xls``.
    """
    today = today or date.today()
    day = price_upload_day(path, upload, today=today)
    age = (today - day).days
    if age <= max_age_days:
        return None
    return (
        f"Прайс старше {max_age_days} дней: файл от {day.strftime('%d.%m.%Y')} "
        f"({_shown_price_name(path, upload)}). Сопоставление продолжается."
    )


def _as_ean(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if 1e12 <= value < 1e14:
            digits = str(int(round(value)))
            if len(digits) == 13:
                return digits
        return None
    if isinstance(value, str):
        digits = re.sub(r"\D", "", value.strip())
        if len(digits) == 13:
            return digits
    return None


def _string_price(raw: str) -> float | None:
    lowered = raw.lower()
    if re.search(r"[а-яa-z]{3,}", lowered) and not re.search(r"руб|ндс|пара|шт", lowered):
        return None
    token = lowered.replace(" ", "").replace(",", ".")
    token = re.sub(r"(руб\.?|пара|шт\.?|сндс|ндс|кг|гр)", "", token)
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        number = float(token)
        if 0 < number <= 1_000_000:
            return number
    return None


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if _as_ean(value):
            return None
        number = float(value)
        if 0 <= number <= 1_000_000:
            return number
        return None
    if isinstance(value, str):
        return _string_price(value)
    return None


def _product_like(raw: str) -> bool:
    norm = normalize(raw)
    if len(norm) < 8 or len(norm) > 240:
        return False
    if any(key in norm for key in _BOILER):
        return False
    if len(re.findall(r"[а-яa-z]", norm)) < 5:
        return False
    if norm in _HEADER_EXACT or norm.startswith("наименование "):
        return False
    return True


def _clean_name(raw: str) -> str:
    text = raw.replace("\xa0", " ").replace("\n", " ")
    text = re.sub(r"i{3,}\s*новинка\s*i{3,}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _keep_names(names: list[str]) -> list[str]:
    """Drop a brand or category cell when the row also has a priced name with a digit.

    «ЮНИЛЕВЕР» next to «ДОМЕСТОС 1л» is the manufacturer column. Two real names
    that both contain a digit (the ЙОРК sheet) are both kept.
    """
    unique = list(dict.fromkeys(names))
    if len(unique) <= 1:
        return unique
    with_digit = [name for name in unique if re.search(r"\d", name)]
    if not with_digit:
        return unique
    return with_digit


def _hint(names: list[str], kept: list[str]) -> str | None:
    kept_set = set(kept)
    for name in names:
        if name in kept_set:
            continue
        found = category_of(name)
        if found:
            return found
    return None


def skus_from_rows(rows: list[tuple]) -> list[Sku]:
    """Build positions from ``(sheet, names, numbers[, ean])`` in sheet order.

    A row with names and no price is a section header. It sets the current kind
    only when the header text matches the lexicon. The sheet title is ignored.
    """
    found: dict[str, Sku] = {}
    header: str | None = None
    current_sheet: str | None = None
    for row in rows:
        sheet = str(row[0])
        names = [name for name in row[1] if name]
        numbers = [number for number in row[2] if number > 0]
        ean = row[3] if len(row) > 3 else None
        if sheet != current_sheet:
            current_sheet = sheet
            header = None
        if not names:
            continue
        if not numbers:
            found_cat = category_of(" ".join(names))
            if found_cat:
                header = found_cat
            continue
        kept = _keep_names(names)
        hint = _hint(names, kept)
        price = numbers[-1]
        row_ean = ean if len(kept) == 1 else None
        for name in kept:
            norm = normalize(name)
            key = f"ean:{row_ean}" if row_ean else f"name:{norm}"
            category = category_of(name) or hint or header or OTHER
            current = found.get(key)
            if current is None or len(name) < len(current.name):
                found[key] = Sku(
                    name=name,
                    name_norm=norm,
                    sheet=sheet,
                    ean=row_ean,
                    price=price,
                    category=category,
                )
    return list(found.values())


def _row_cells(sheet: xlrd.sheet.Sheet, row_idx: int) -> tuple[list[str], list[float], str | None]:
    names: list[str] = []
    numbers: list[float] = []
    ean: str | None = None
    for col_idx in range(sheet.ncols):
        value = sheet.cell_value(row_idx, col_idx)
        if isinstance(value, str):
            stripped = value.strip()
            if _product_like(stripped):
                names.append(_clean_name(stripped))
                continue
        found_ean = _as_ean(value)
        if found_ean:
            if ean is None:
                ean = found_ean
            continue
        number = _as_number(value)
        if number is not None and number > 0:
            numbers.append(number)
    return names, numbers, ean


def load_catalog(path: str | None = None) -> list[Sku]:
    book = xlrd.open_workbook(path or find_price_xls())
    rows: list[tuple] = []
    for sheet in book.sheets():
        for row_idx in range(sheet.nrows):
            names, numbers, ean = _row_cells(sheet, row_idx)
            if not names:
                continue
            rows.append((sheet.name, names, numbers, ean))
    return skus_from_rows(rows)
