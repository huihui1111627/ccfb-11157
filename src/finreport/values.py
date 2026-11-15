"""Parsing of printed amounts, units and page/text heuristics."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

UNIT_SCALE = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
    "亿元": Decimal(100000000),
}

UNIT_RE = re.compile(
    r"(?:金额)?单位\s*[:：]?\s*(?:人民币)?\s*(百万元|千元|万元|亿元|元)"
)
UNIT_ONLY_RE = re.compile(
    r"^\s*[（(]?\s*(?:金额)?单位\s*[:：]?\s*(?:人民币)?\s*"
    r"(百万元|千元|万元|亿元|元)\s*[)）]?\s*$"
)
# a unit label without the "单位：" prefix, e.g. a footer or a stray caption
UNIT_BARE_RE = re.compile(r"(?:人民币|人币|民币|人民)?\s*(百万元|千元|万元|亿元)")

YEAR_RE = re.compile(r"(20\d{2})\s*年")
DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")

# accounts whose printed unit is not the statement's currency unit
PER_SHARE_KEYS = {"basic_eps", "diluted_eps"}


# OCR scrambles the characters around the unit ("人币千民元"); when the
# "单位" marker is present, look for a magnitude character followed closely
# by 元 rather than requiring the exact compound
_UNIT_FUZZY = re.compile(r"(百万|千|万|亿)[^\u4e00-\u9fff\d]{0,2}[\u4e00-\u9fff]{0,2}元")
_FUZZY_SCALE = {"百万": "百万元", "千": "千元", "万": "万元", "亿": "亿元"}


def detect_unit(texts: list[str]) -> tuple[str, int] | None:
    """Return ``(unit_label, scale)`` from strings near the title."""
    # explicit "单位：万元" wins over a bare mention
    for text in texts:
        m = UNIT_RE.search(text)
        if m:
            label = m.group(1)
            return label, int(UNIT_SCALE[label])
    for text in texts:
        m = UNIT_BARE_RE.search(text)
        if m:
            label = m.group(1)
            return label, int(UNIT_SCALE[label])
    # a unit label can be split across two OCR lines ("...人民币万" / "元"),
    # so adjacent lines are also tried joined
    for a, b in zip(texts, texts[1:]):
        joined = a + b
        for pat in (UNIT_RE, UNIT_BARE_RE, _UNIT_FUZZY):
            m = pat.search(joined)
            if m:
                label = m.group(1)
                return _FUZZY_SCALE.get(label, label), int(UNIT_SCALE[
                    _FUZZY_SCALE.get(label, label)])
    for text in texts:
        if "单位" not in text:
            continue
        # the unit sits at the tail of the "单位：" clause; take the whole
        # string after the last marker and accept a fuzzy magnitude
        idx = text.rindex("单位")
        tail = text[idx:]
        m = _UNIT_FUZZY.search(tail)
        if m:
            label = _FUZZY_SCALE[m.group(1)]
            return label, int(UNIT_SCALE[label])
    return None


_DIGIT_MAP = str.maketrans({
    "O": "0", "o": "0", "〇": "0", "l": "1", "I": "1", "|": "1",
    "S": "5", "B": "8", "Z": "2", "，": ",", "．": ".", "。": ".",
    "—": "-", "–": "-", "−": "-", "－": "-", "　": "",
})


def parse_amount(text: str) -> tuple[Decimal | None, bool]:
    """Parse a printed amount.

    Returns ``(value, is_blank)``. ``is_blank`` is True when the cell means
    zero (a dash or an empty cell), distinguishing it from an unparseable
    cell, which returns ``(None, False)``.
    """
    if text is None:
        return None, True
    s = str(text).strip().translate(_DIGIT_MAP)
    s = s.replace(" ", "")
    if s == "":
        return None, True
    if s in {"-", "--", "/", "不适用", "N/A", "n/a"}:
        return Decimal(0), True
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    elif s.startswith("（") and s.endswith("）"):
        neg = True
        s = s[1:-1]
    if s.startswith("-") and len(s) > 1:
        neg = True
        s = s[1:]
    if s.endswith("-") and len(s) > 1:
        neg = True
        s = s[:-1]
    # decimal-comma confusion from OCR: "1,234,56" -> "1,234.56"
    if "," in s and "." not in s:
        tail = s.rsplit(",", 1)[1]
        if len(tail) in (1, 2) and s.count(",") >= 2:
            s = s.rsplit(",", 1)[0] + "." + tail
    s = s.replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", s):
        return None, False
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None, False
    return (-value if neg else value), False


def looks_numeric(text: str) -> bool:
    value, blank = parse_amount(text)
    if blank:
        return str(text).strip() in {"-", "--", "—", "–", "−"}
    return value is not None


def round_to_printed(yuan: Decimal, scale: int, places: int = 2) -> Decimal:
    """Round a yuan amount to what the given unit can express."""
    step = Decimal(scale)
    q = (yuan / step).quantize(Decimal(1).scaleb(-places))
    return q * step


def quantize_for_print(yuan: Decimal, scale: int, places: int = 2) -> Decimal:
    return (yuan / Decimal(scale)).quantize(Decimal(1).scaleb(-places))
