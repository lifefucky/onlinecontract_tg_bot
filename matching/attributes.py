"""Lexical false friends. Material, brand, and pack size are not match gates.

``false_friend`` is the only line parser the matcher still uses. The other
fields stay so the dictionary can be checked on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_THEME_RE = re.compile(r"моющ\w*\s+средств|бытов\w+\s+хими|синтетическ\w*\s+моющ")
_COTTON_RE = re.compile(r"х\s*/\s*б|хб\b|хлопчат|хлопок")
_WOOL_RE = re.compile(r"полушерст|п\s*/\s*шерст|п\s*/\s*ш\b")
_LATIN_RE = re.compile(r"[a-z]{4,}")

_LATIN_STOP = {
    "pack",
    "size",
    "blue",
    "black",
    "white",
    "household",
    "gloves",
    "glove",
    "latex",
    "nitrile",
    "vinyl",
    "risk",
    "high",
    "professional",
    "comfort",
    "home",
    "premium",
    "color",
    "colour",
}

_KILLERS: list[tuple[str, re.Pattern[str]]] = [
    (
        "reagent",
        re.compile(
            r"\bчда\b|\bхч\b|реактив|гидроксид|"
            r"(?:соляная|серная|азотная|уксусная)\s+кислот|"
            r"кислот[аы]\s+(?:соляная|серная|азотная)"
        ),
    ),
    (
        "service",
        re.compile(r"клининг|услуг\w*(?:\s+\w+){0,4}\s+убор|уборк\w*(?:\s+\w+){0,3}\s+помещ"),
    ),
    (
        "appliance",
        re.compile(
            r"микроволнов|холодильник|стиральн\w+\s+машин|бытовая техника|"
            r"электрочайник|пылесос|посудомоеч|морозильн\w*\s+камер"
        ),
    ),
    (
        "conduit",
        re.compile(r"гофр\w*|пвх\s+труб|труб\w*\s+пвх|кабель-?канал|короб\s+пвх|пвх\s+короб"),
    ),
    (
        "sign",
        re.compile(r"табличк|вывеск|информационн\w+\s+(?:таб|знак|указ)"),
    ),
]

_HEAD_FILLER = {"и", "в", "с", "для", "шт", "штука", "пара", "пар", "уп", "упак", "упаковка"}


@dataclass
class Parsed:
    product_type: str | None = None
    theme: bool = False
    head_only: bool = False
    material: str | None = None
    coating: str | None = None
    polymer: str | None = None
    degree: str | None = None
    soft_dot: bool = False
    soft_winter: bool = False
    soft_disp: bool = False
    says_coating: bool = False
    brand_latin: list[str] = field(default_factory=list)
    killer: str | None = None


def normalize(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u202f", " ")
    text = text.replace("ё", "е").replace("Ё", "е")
    text = text.lower()
    text = text.replace("«", " ").replace("»", " ").replace('"', " ")
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def cleanup_embed(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u202f", " ")
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def false_friend(norm: str) -> str | None:
    for name, pattern in _KILLERS:
        if pattern.search(norm):
            return name
    return None


def _product_type(norm: str) -> str | None:
    if re.search(r"бахил", norm):
        return "бахилы"
    if re.search(r"рукавиц", norm):
        return "рукавицы"
    if re.search(r"перчат", norm):
        return "перчатки"
    if re.search(r"салфет", norm):
        return "салфетки"
    if re.search(r"(?<![а-я])мыл[оа](?![а-я])", norm):
        return "мыло"
    if re.search(r"(?<![а-я])паст[аыуе](?![а-я])", norm):
        return "паста"
    if re.search(r"(?<![а-я])крем(?![а-я])", norm):
        return "крем"
    return None


def _head_only(norm: str, product_type: str | None) -> bool:
    if product_type not in {"перчатки", "мыло", "паста", "крем", "салфетки"}:
        return False
    stripped = norm
    stripped = re.sub(r"перчат\w*", " ", stripped)
    stripped = re.sub(r"мыл[оа]", " ", stripped)
    stripped = re.sub(r"паст[аыуе]", " ", stripped)
    stripped = re.sub(r"(?<![а-я])крем(?![а-я])", " ", stripped)
    stripped = re.sub(r"салфет\w*", " ", stripped)
    stripped = re.sub(r"[^а-яa-z0-9/]+", " ", stripped)
    tokens = [tok for tok in stripped.split() if tok not in _HEAD_FILLER]
    return not tokens


def parse_line(text: str) -> Parsed:
    norm = normalize(text)
    parsed = Parsed(
        product_type=_product_type(norm),
        theme=bool(_THEME_RE.search(norm)),
        killer=false_friend(norm),
    )
    parsed.head_only = _head_only(norm, parsed.product_type)
    if _WOOL_RE.search(norm):
        parsed.material = "полушерсть"
    elif _COTTON_RE.search(norm):
        parsed.material = "х/б"
    elif re.search(r"нейлон", norm):
        parsed.material = "нейлон"

    pvc = bool(re.search(r"пвх", norm))
    latex = bool(re.search(r"латекс", norm))
    nitrile = bool(re.search(r"нитрил", norm))
    vinyl = bool(re.search(r"винил|витрил", norm))
    tpe = bool(re.search(r"тпэ|\btpe\b", norm))
    obliv = bool(re.search(r"облив", norm))
    parsed.says_coating = bool(re.search(r"покрыт", norm)) or obliv
    parsed.soft_dot = bool(re.search(r"точеч|точк", norm))
    parsed.soft_winter = bool(re.search(r"утепл|зимн", norm))
    parsed.soft_disp = bool(re.search(r"одноразов", norm))
    if re.search(r"двойн", norm):
        parsed.degree = "двойной"
    elif re.search(r"одинарн", norm):
        parsed.degree = "одинарный"

    if parsed.product_type in {"перчатки", "рукавицы"}:
        if parsed.material == "х/б" and (latex or obliv) and not nitrile:
            parsed.coating = "латекс"
        elif pvc and not nitrile and not vinyl and not tpe:
            parsed.coating = "ПВХ"
        if nitrile:
            parsed.polymer = "нитрил"
        elif vinyl:
            parsed.polymer = "винил"
        elif tpe:
            parsed.polymer = "тпэ"
        elif latex and parsed.material != "х/б":
            parsed.polymer = "латекс"
        elif obliv and parsed.material != "х/б" and pvc:
            parsed.coating = "ПВХ"

    parsed.brand_latin = [tok for tok in _LATIN_RE.findall(norm) if tok not in _LATIN_STOP]
    return parsed


def killer_sources() -> tuple[str, ...]:
    """Pattern text, in order. Part of the rules version."""
    return tuple(pattern.pattern for _name, pattern in _KILLERS)
