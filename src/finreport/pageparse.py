"""Turns a PDF page into a grid of labelled numeric rows.

Two front-ends feed this module:

* text PDFs  -> PyMuPDF words with bounding boxes (``words_from_pdf``)
* scanned    -> Tesseract TSV words with bounding boxes (``words_from_ocr``)

Both produce the same ``Word`` list, which ``collect_rows`` groups into table
rows by vertical position, splits the label from the numeric columns by
horizontal position, and attaches a bounding box to every amount so the UI can
jump from a figure back to its position on the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .values import looks_numeric, parse_amount


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 100.0


@dataclass
class Cell:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 100.0


@dataclass
class RowCandidate:
    y0: float
    y1: float
    label: str
    label_box: tuple[float, float, float, float]
    numbers: list[Cell]
    page: int
    raw_text: str = ""


@dataclass
class TableRegion:
    page_start: int
    page_end: int
    rows: list[RowCandidate] = field(default_factory=list)
    header_text: str = ""
    unit: str | None = None
    unit_line: str | None = None


# ---------------------------------------------------------------------------
# words from sources
# ---------------------------------------------------------------------------

def words_from_pdf(page) -> list[Word]:
    out = []
    for w in page.get_text("words", sort=False):
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        if not text.strip():
            continue
        out.append(Word(text, x0, y0, x1, y1, 100.0))
    return out


def words_from_ocr(page, dpi: int = 300, psm: int = 4) -> list[Word]:
    import io

    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    # psm 4 ("single column of text of variable sizes") keeps each table row
    # on its own OCR line; psm 6 merges rows and drops figures on dense grids.
    # Title scanning uses psm 3, which segments the page without forcing it
    # into one column and is more reliable for centred headings.
    data = pytesseract.image_to_data(
        img, lang="chi_sim+eng", config=f"--psm {psm}",
        output_type=pytesseract.Output.DICT,
    )
    sx = page.rect.width / img.width
    sy = page.rect.height / img.height
    out = []
    for i, text in enumerate(data["text"]):
        t = (text or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0:
            conf = 0.0
        out.append(Word(
            t,
            data["left"][i] * sx, data["top"][i] * sy,
            (data["left"][i] + data["width"][i]) * sx,
            (data["top"][i] + data["height"][i]) * sy,
            conf,
        ))
    return out


# ---------------------------------------------------------------------------
# row assembly
# ---------------------------------------------------------------------------

_CJK = re.compile(r"[一-鿿]")


def _group_lines(words: list[Word], tol: float = 3.0) -> list[list[Word]]:
    ws = sorted(words, key=lambda w: (round(w.y0, 1), w.x0))
    lines: list[list[Word]] = []
    for w in ws:
        placed = False
        for line in lines:
            cy = sum(x.y0 for x in line) / len(line)
            if abs(w.y0 - cy) <= tol:
                line.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w.x0)
    lines.sort(key=lambda ln: sum(w.y0 for w in ln) / len(ln))
    return lines


def merge_words(words: list[Word], gap: float) -> list[Word]:
    """Merge adjacent words separated by less than ``gap`` points."""
    if not words:
        return []
    merged = [words[0]]
    for w in words[1:]:
        prev = merged[-1]
        same_text = bool(_CJK.search(prev.text) or _CJK.search(w.text))
        if (w.x0 - prev.x1) < gap and same_text:
            merged[-1] = Word(prev.text + w.text, prev.x0, min(prev.y0, w.y0),
                              max(prev.x1, w.x1), max(prev.y1, w.y1),
                              min(prev.conf, w.conf))
        else:
            merged.append(w)
    return merged


def _clean_label(label: str) -> str:
    s = re.sub(r"\s+", "", label)
    s = s.replace("..", "").replace("…", "")
    return s.strip()


def is_currency_like(text: str) -> bool:
    """True for things shaped like a printed amount, not a note reference.

    Note references are short integers ("4", "（3）", "五、5"); amounts carry
    thousands separators, decimals, or are long enough to be unambiguous.
    """
    t = text.strip()
    if not t:
        return False
    # printed amounts never contain CJK characters; this keeps company names
    # and titles from being mistaken for figures when they share a line
    if re.search(r"[一-鿿]", t):
        return False
    value, blank = parse_amount(t)
    if blank:
        return t in {"-", "--", "—", "–", "−"}
    if value is None:
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", t):
        return False           # a year, not an amount
    digits = re.sub(r"[^\d]", "", t)
    return bool(re.search(r"[.,]", t)) or len(digits) >= 4


def _column_centers(lines: list[list[Word]]) -> list[float]:
    """Right-edge centres of the amount columns.

    Amount columns are the vertical lines on which several currency-shaped
    words end.  The note column also holds digits, but they are short
    references ("2", "（1）") and never currency-shaped, so it is excluded.
    """
    nums = [w for line in lines for w in line if is_currency_like(w.text)]
    if not nums:
        return []
    clusters: list[list[Word]] = []
    for w in sorted(nums, key=lambda w: w.x1):
        placed = False
        for cl in clusters:
            if abs(w.x1 - sum(x.x1 for x in cl) / len(cl)) <= 7:
                cl.append(w)
                placed = True
                break
        if not placed:
            clusters.append([w])
    solid = [cl for cl in clusters if len(cl) >= 3] or \
            [max(clusters, key=len)]
    return sorted(sum(w.x1 for w in cl) / len(cl) for cl in solid)


def _estimate_numeric_start(lines: list[list[Word]]) -> float:
    centers = _column_centers(lines)
    if not centers:
        return 10_000.0
    return centers[0] - 60.0


_NOTE_SUFFIX = re.compile(
    r"(?:附注|注释|注)?[一二三四五六七八九十]{0,3}[、.]?[（(]?\d{1,3}[)）]?$"
)


def strip_note_marker(label: str) -> str:
    """Drop a trailing note reference such as （30）, 注12, 五、5."""
    s = label.strip()
    if not s:
        return s
    m = _NOTE_SUFFIX.search(s)
    if not m or m.start() == 0:
        return s
    tail = s[m.start():]
    if not re.search(r"\d", tail):
        return s
    # "一年内到期的非流动资产" ends in 年; nothing here should be stripped
    if s[m.start():].startswith("年"):
        return s
    return s[:m.start()].rstrip("、.：: ")


# a printed amount: thousands-grouped integer, optionally with decimals
_AMOUNT_INT = re.compile(r"\(?-?\d{1,3}(?:,\d{3})+")


def _tokenize_amounts(text: str) -> list[str]:
    """Greedy left-to-right parse of concatenated printed amounts.

    A plain regex would let the tail of one amount be read as the head of the
    next ("...000.00327,792..." -> "327,792"), so each amount is consumed in
    full (integer part, optional decimal) before scanning resumes.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        m = _AMOUNT_INT.match(text, i)
        if not m:
            i += 1
            continue
        end = m.end()
        if end < n and text[end] == ".":
            j = end + 1
            while j < n and text[j].isdigit():
                j += 1
            frac = j - (end + 1)
            # in a concatenated run the fractional digits run straight into
            # the next amount ("...000.00327,792..."), so keep two decimals
            if frac > 4 or (j < n and text[j] == ","):
                end = end + 1 + min(frac, 2)
            else:
                end = j
        if end < n and text[end] == ")" and text.startswith("(", i):
            end += 1
        out.append(text[m.start():end])
        i = end
    return out


def split_amount_runs(word: Word) -> list[Word]:
    """Split a token that concatenates several printed amounts.

    Four-column tables can emit "585,323,000.00327,792,000.00" as one word when
    columns sit close together; each amount is recovered and its box is
    interpolated across the original word so highlighting stays aligned.
    """
    text = word.text
    # a lone amount with a currency sign or a note quote fused to it
    fused = bool(re.search(r"[“”\"]\d[\d,]*\d", text))
    if text.count(",") < 2 and not fused:
        return [word]
    parts = _tokenize_amounts(text)
    if len(parts) < 2 and not (fused and parts):
        return [word]
    # labels and amounts can be fused into one token ("非流动资产合计1,534,253")
    # — keep the leading caption with the first amount's box
    leading = text[:text.find(parts[0])] if parts[0] in text else ""
    covered = sum(len(p) for p in parts) + len(leading)
    if covered < len(text) * 0.6:
        return [word]
    width = word.x1 - word.x0
    out, cursor = [], word.x0
    if leading:
        w = width * len(leading) / covered
        out.append(Word(leading, cursor, word.y0, cursor + w, word.y1,
                        word.conf))
        cursor += w
    for part in parts:
        w = width * len(part) / covered
        out.append(Word(part, cursor, word.y0, cursor + w, word.y1, word.conf))
        cursor += w
    return out



def belongs_to_column(word: Word, centers: list[float],
                      tol: float) -> bool:
    """True when a word is right-aligned to one of the amount columns."""
    return any(abs(c - word.x1) <= tol for c in centers)


def collect_rows(words: list[Word], page_no: int, num_x0: float | None,
                 num_x1: float | None, *, ocr: bool = False
                 ) -> list[RowCandidate]:
    """Group words into rows, split label vs numeric columns.

    ``num_x0`` / ``num_x1`` bound the numeric area; when unknown they are
    estimated from the distribution of numeric-looking right-aligned words.
    """
    lines = _group_lines(words)
    centers = _column_centers(lines)
    if num_x0 is None:
        num_x0 = _estimate_numeric_start(lines)
    spacing = min((b - a for a, b in zip(centers, centers[1:])),
                  default=120.0)
    snap = max(8.0, spacing * 0.45)
    label_zone = centers[0] - spacing * 0.75 if centers else num_x0
    rows: list[RowCandidate] = []
    pending_label: list[Word] = []
    for line in lines:
        line = [part for w in line for part in split_amount_runs(w)]
        line.sort(key=lambda w: w.x0)
        label_parts: list[Word] = []
        nums: list[Word] = []
        for w in line:
            if is_currency_like(w.text):
                nums.append(w)
            elif centers and belongs_to_column(w, centers, snap) \
                    and (not ocr or _looks_amountish(w, line, label_zone)):
                nums.append(w)
            else:
                label_parts.append(w)
        # an OCR row may split a label across lines, leaving "他应收款" on one
        # line and "其" on the next; such a bare fragment is joined on
        if not nums and label_parts and _is_fragment(label_parts, label_zone):
            pending_label = label_parts + pending_label
            continue
        if pending_label:
            label_parts = pending_label + label_parts
            pending_label = []
        if not label_parts and not nums:
            continue
        label_parts.sort(key=lambda w: w.x0)
        if label_parts:
            merged = merge_words(label_parts, gap=6.0)
            label = strip_note_marker("".join(w.text for w in merged))
            lb = (label_parts[0].x0, min(w.y0 for w in label_parts),
                  label_parts[-1].x1, max(w.y1 for w in label_parts))
        else:
            label = ""
            lb = (0.0, line[0].y0, 0.0, line[0].y1)
        nums.sort(key=lambda w: w.x0)
        cells = [Cell(w.text, w.x0, w.y0, w.x1, w.y1, w.conf) for w in nums]
        rows.append(RowCandidate(
            y0=min(w.y0 for w in line), y1=max(w.y1 for w in line),
            label=_clean_label(label), label_box=lb, numbers=cells,
            page=page_no,
            raw_text=" ".join(w.text for w in line),
        ))
    if ocr:
        rows = _merge_split_rows(rows)
    return [r for r in rows if r.label or r.numbers]


def _merge_split_rows(rows: list[RowCandidate]) -> list[RowCandidate]:
    """Recombine OCR fragments of one visual table row.

    Tesseract sometimes renders the label and its amounts on two lines a few
    points apart, or lifts a note number onto its own line.  Rows closer than
    half a table row are one visual row, so their labels and cells are joined.
    """
    if len(rows) < 2:
        return rows
    pitches = sorted(b.y0 - a.y0 for a, b in zip(rows, rows[1:])
                     if b.y0 > a.y0)
    pitch = pitches[len(pitches) // 2] if pitches else 0.0
    out: list[RowCandidate] = [rows[0]]
    for row in rows[1:]:
        prev = out[-1]
        gap = row.y0 - prev.y0
        # fragments of one visual row sit far closer than the row pitch
        if pitch and 0 < gap <= pitch * 0.45 and \
                (bool(prev.numbers) != bool(row.numbers)
                 or not prev.numbers and not row.numbers):
            label = prev.label + row.label
            box = (min(prev.label_box[0], row.label_box[0]),
                   min(prev.label_box[1], row.label_box[1]),
                   max(prev.label_box[2], row.label_box[2]),
                   max(prev.label_box[3], row.label_box[3]))
            merged = RowCandidate(
                y0=min(prev.y0, row.y0), y1=max(prev.y1, row.y1),
                label=label, label_box=box,
                numbers=prev.numbers + row.numbers,
                page=row.page,
                raw_text=(prev.raw_text + " " + row.raw_text).strip(),
            )
            out[-1] = merged
        else:
            out.append(row)
    for r in out:
        r.numbers.sort(key=lambda c: c.x0)
        _demote_stray_cells(r)
    return out


def _demote_stray_cells(row: RowCandidate) -> None:
    """Move note references that were merged into the amount cells back out."""
    money = [c for c in row.numbers if is_currency_like(c.text)]
    if not money:
        return
    leftmost = min(c.x0 for c in money)
    keep, moved = [], []
    for c in row.numbers:
        if is_currency_like(c.text) or c.x1 >= leftmost - 10:
            keep.append(c)
        else:
            moved.append(c)
    if moved:
        row.numbers = keep
        prefix = "".join(c.text for c in moved)
        row.label = row.label + prefix if row.label else prefix


def _looks_amountish(word: Word, line: list[Word], label_zone: float) -> bool:
    """A word aligned to an amount column that is plausibly a figure.

    Short runs of digits ("218"), blanks ("-") and OCR digits with a stray
    trailing glyph ("55,741.10fi,") qualify.  Words are also rejected when they
    sit left of the label zone, which is where row captions live.
    """
    t = word.text.strip()
    if not t or len(t) > 24:
        return False
    digits = sum(ch.isdigit() for ch in t)
    if digits == 0:
        return t in {"-", "--", "—", "–", "−"}
    letters = sum(ch.isalpha() for ch in t)
    if letters > 2:
        return False
    return word.x0 >= label_zone or digits >= 3


def _is_fragment(words: list[Word], label_zone: float) -> bool:
    """A row holding only a short label fragment outside the label zone."""
    if not words:
        return False
    text = "".join(w.text for w in words)
    if len(text) > 3 or re.search(r"\d", text):
        return False
    return all(w.x1 <= label_zone for w in words) or len(words) <= 2


def text_lines(page) -> list[tuple[float, str]]:
    """Return ``(y, text)`` lines from the text layer."""
    d = page.get_text("dict")
    out = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            txt = "".join(span["text"] for span in line.get("spans", []))
            if txt.strip():
                out.append((line["bbox"][1], txt.strip()))
    out.sort()
    return out


def header_numbers(row: RowCandidate) -> int:
    return sum(1 for c in row.numbers if parse_amount(c.text)[0] is not None)


def numeric_density(rows: list[RowCandidate]) -> float:
    if not rows:
        return 0.0
    with_nums = sum(1 for r in rows if header_numbers(r) >= 1)
    return with_nums / len(rows)
