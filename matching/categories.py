"""Product-kind categories for the price list.

Section headers inside a sheet are used when they name a kind («Жидкое мыло»,
«Средства для мытья посуды»). Sheet titles such as «НЭФИС», «Henkel» and
«Артпласт» are suppliers and are not categories. Headers that do not name a
kind (a knit class, a brand line) leave the previous kind in force. A position
whose own name matches the lexicon uses that kind. Anything left over is
«прочее», so positions outside the lexicon still belong to the interest set.
The lexicon does not use embeddings, volume, weight, pack count, or brand.
"""

from __future__ import annotations

import re

from matching.attributes import normalize

OTHER = "прочее"

# First match wins. More specific kinds stay above broader ones.
_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("перчатки", (r"перчат", r"рукавиц")),
    ("пакеты и мешки", (r"мешк", r"пакет")),
    (
        "средства от насекомых",
        (
            r"комар",
            r"антимол",
            r"\bмоли\b",
            r"\bмоль\b",
            r"инсектиц",
            r"репеллен",
            r"дихлофос",
            r"mosquit",
            r"москит",
            r"от мух",
            r"таракан",
        ),
    ),
    ("бахилы", (r"бахил",)),
    ("подгузники", (r"подгуз", r"памперс", r"pampers")),
    ("средства для обуви", (r"обуви", r"salamander", r"саламандер")),
    ("антисептики", (r"антисепти", r"дезинфиц", r"хлорамин", r"антибактер")),
    ("салфетки", (r"салфет",)),
    ("уборочный инвентарь", (r"губк", r"щетк", r"швабр", r"тряпк", r"\bмоп\b", r"совок", r"веник")),
    ("средства для посуды", (r"для посуд", r"мытья посуд", r"мойки посуд", r"fairy", r"фейри", r"\bfinish\b")),
    ("мыло", (r"мыло", r"\bsoap\b")),
    (
        "средства для стирки",
        (
            r"стир",
            r"\bсмс\b",
            r"порошок",
            r"ariel",
            r"\btide\b",
            r"персил",
            r"\bлоск\b",
            r"\bмиф\b",
            r"sorti",
            r"биолан",
            r"\baos\b",
            r"dosia",
            r"лотус",
        ),
    ),
    (
        "чистящие средства",
        (
            r"белизн",
            r"отбел",
            r"чистящ",
            r"ч\s*/\s*с",
            r"domestos",
            r"доместос",
            r"\bcomet\b",
            r"комет",
            r"sanfor",
            r"санфор",
            r"\bbref\b",
            r"бреф",
            r"унитаз",
            r"писсуар",
            r"санитарн",
            r"для ванн",
            r"кафел",
            r"стекол",
            r"для окон",
            r"для пола",
            r"пропер",
            r"пемолюкс",
            r"пемоксоль",
            r"моющ\w*\s+средств",
            r"м\s*/\s*с",
            r"прогресс",
            r"универсальн",
        ),
    ),
    ("освежители воздуха", (r"освежител", r"\bglade\b", r"air\s*wick")),
    ("уход за полостью рта", (r"зубн", r"oral[\s_-]*b", r"полост\w+\s+рта")),
    ("уход за волосами", (r"шамп", r"волос", r"укладк", r"pantene", r"tresemme")),
    (
        "уход за кожей",
        (
            r"(?<![а-я])крем(?![а-я])",
            r"лосьон",
            r"дезодорант",
            r"антиперспирант",
            r"для душа",
            r"пена для ван",
            r"nivea",
            r"нивея",
            r"rexona",
            r"рексона",
            r"(?<![а-я])dove(?![а-я])",
        ),
    ),
    ("бумажная продукция", (r"туалетн", r"бумаг", r"полотенц")),
    ("посуда одноразовая", (r"стакан", r"тарел", r"контейнер", r"крышк", r"вилк", r"ложк", r"посуд", r"ланч")),
    ("колготки и носки", (r"колгот", r"носк", r"гольф", r"чулк")),
    ("фольга и пленка", (r"фольг", r"пленк", r"пергамент")),
    ("свечи", (r"свеч",)),
    ("маски", (r"(?<![а-я])маск",)),
    ("диспенсеры", (r"диспенсер",)),
)

_COMPILED = tuple((name, tuple(re.compile(pattern) for pattern in patterns)) for name, patterns in _CATEGORIES)

# Embedding anchors for goods the price list does not sell. Together with the
# dictionary in attributes.py they are the only line filter besides category.
NEGATIVE_SAMPLES = (
    "услуги клининга уборка помещений",
    "химические реактивы чда хч кислота",
    "бытовая техника холодильник микроволновка",
    "труба ПВХ гофрированная кабель-канал",
    "информационная табличка вывеска ПВХ",
)


def category_names() -> tuple[str, ...]:
    return tuple(name for name, _patterns in _CATEGORIES) + (OTHER,)


def category_signature() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Stable input for the rules version: names and source patterns, in order."""
    return _CATEGORIES + ((OTHER, ()),)


def category_of(text: str) -> str | None:
    """Lexicon kind for ``text``, or None when it names no product kind."""
    norm = normalize(text)
    if not norm:
        return None
    for name, patterns in _COMPILED:
        for pattern in patterns:
            if pattern.search(norm):
                return name
    return None
