"""Read the price list and attach a product-kind category to every position."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
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


@dataclass
class Sku:
    name: str
    name_norm: str
    sheet: str
    ean: str | None
    price: float | None
    category: str


def _pick_xls(directory: Path) -> Path | None:
    """Largest ``.xls`` in ``directory``. A name containing ``ОПТ`` wins over other files."""
    if not directory.is_dir():
        return None
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    files = [path for path in entries if path.is_file() and path.suffix.lower() == ".xls"]
    preferred = [path for path in files if "ОПТ" in path.name.upper()]
    chosen = preferred or files
    if not chosen:
        return None
    chosen.sort(key=lambda path: path.stat().st_size, reverse=True)
    return chosen[0]


def find_price_xls() -> str:
    """Resolve the price list.

    ``PRICE_LIST_PATH`` (absolute or relative to the repository) wins. Otherwise
    the largest ``.xls`` in ``data/``, preferring a name that contains ``ОПТ``.
    ``docs/research`` is only a legacy fallback for an existing checkout.
    """
    raw = os.environ.get("PRICE_LIST_PATH", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = config.REPO_ROOT / path
        if not path.is_file():
            raise FileNotFoundError(f"PRICE_LIST_PATH is set but the file is missing: {path}")
        return str(path)
    for directory in (config.DATA_DIR, config.RESEARCH_DIR):
        found = _pick_xls(directory)
        if found is not None:
            return str(found)
    raise FileNotFoundError(
        "price list xls not found; set PRICE_LIST_PATH or put the file in data/"
    )


def price_fingerprint(path: str | Path) -> str:
    """Content hash. A new file, even with the same name, changes the snapshot."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def price_file_day(path: str | Path, *, today: date | None = None) -> date:
    """Date from the filename (``26.04`` or ``26.04.2026``), otherwise the file mtime."""
    today = today or date.today()
    name = Path(path).name
    match = _DATE_RE.search(name)
    if match:
        day_n = int(match.group(1))
        month_n = int(match.group(2))
        year_n = int(match.group(3)) if match.group(3) else today.year
        try:
            found = date(year_n, month_n, day_n)
        except ValueError:
            found = None
        if found is not None:
            if match.group(3) is None and found > today:
                try:
                    found = date(year_n - 1, month_n, day_n)
                except ValueError:
                    found = None
            if found is not None:
                return found
    stamp = Path(path).stat().st_mtime
    return datetime.fromtimestamp(stamp).date()


def stale_price_warning(
    path: str | Path,
    *,
    today: date | None = None,
    max_age_days: int,
) -> str | None:
    """Russian note for owners, or None when the file is still fresh enough."""
    today = today or date.today()
    day = price_file_day(path, today=today)
    age = (today - day).days
    if age <= max_age_days:
        return None
    return (
        f"Прайс старше {max_age_days} дней: файл от {day.strftime('%d.%m.%Y')} "
        f"({Path(path).name}). Сопоставление продолжается."
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
