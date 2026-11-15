"""Statement detection, page grouping and row/column extraction.

The extractor is column-position based rather than order based: numeric
columns are found by clustering the right edges of printed amounts, so a
blank cell, a merged header, or an OCR jitter cannot shift values into the
wrong column.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .accounts import BY_KEY, match_account
from .pageparse import (
    Cell, RowCandidate, collect_rows, is_currency_like, text_lines,
    words_from_ocr, words_from_pdf,
)
from .values import DATE_RE, UNIT_SCALE, YEAR_RE, detect_unit, parse_amount

D = Decimal

TITLE_PATTERNS = [
    ("BS", re.compile(r"资产负债表")),
    ("IS", re.compile(r"利润表|损益表")),
    ("CF", re.compile(r"现金流量表")),
]
# OCR occasionally drops the final 表; a heading that is only the truncated
# form still counts when it is the whole line
TRUNCATED_TITLES = [
    ("BS", re.compile(r"(?:合并|母公司)?资产负债")),
    ("IS", re.compile(r"(?:合并|母公司)?利润")),
    ("CF", re.compile(r"(?:合并|母公司)?现金流量")),
]
_DISTRACTOR = re.compile(
    r"所有者权益变动表|股东权益变动表|主要会计数据|财务指标|审计报告|财务报表附注"
)
# OCR can mangle a title beyond recognition ("F并现金量表").  These
# near-misses still identify the statement.
_TITLE_FUZZY = [
    ("CF", re.compile(r"现金.{0,3}量表|现金流量")),
    ("BS", re.compile(r"资产.{0,3}负债.{0,2}表")),
    ("IS", re.compile(r"利润.{0,3}表|损益表")),
]
_CONTINUED = re.compile(r"[（(]\s*(?:续|继|线)\s*[)）]|续\s*$|[(（]\s*\w{0,3}\s*[)）]\s*$")


def _merge_title_fragments(lines: list[tuple[float, str]]
                           ) -> list[tuple[float, str]]:
    """Join OCR fragments that belong to one heading line.

    Scans often split a title across two lines a few points apart
    ("合并资产负债" / "表").  Adjacent short lines that together contain a
    statement keyword are merged.
    """
    out: list[list] = []
    for y, text in lines:
        t = re.sub(r"\s+", "", text)
        if out and abs(y - out[-1][0]) <= 26 and len(t) <= 12:
            prev = out[-1]
            merged = prev[1] + t
            if any(pat.search(merged) for _, pat in TITLE_PATTERNS):
                prev[1] = merged
                continue
        out.append([y, t])
    return [(y, t) for y, t in out]
_SCOPE_PARENT = re.compile(r"母公司|本公司")
_SCOPE_CONSOL = re.compile(r"合并|集团")
_SCOPE_WORD = re.compile(r"合并|母公司")


@dataclass
class StatementMeta:
    code: str
    scope: str
    title: str
    continued: bool
    page: int
    y: float


def _resolve_scope(meta: StatementMeta, metas: list[StatementMeta]) -> None:
    """Infer the missing scope of a title from its neighbours.

    OCR can lose the "合并" prefix on the first page of a statement.  The
    consolidated table always comes before the parent one and both share a
    page order, so an unknown scope inherits the scope of the next same-code
    title, or the previous one when it is the last.
    """
    if meta.scope != "unknown":
        return
    idx = metas.index(meta)
    # consolidated always precedes parent, so an unknown scope on a first
    # page belongs to the consolidated table that the next title continues
    for t in metas[idx + 1:]:
        if t.code == meta.code and t.continued and t.scope != "unknown":
            meta.scope = t.scope
            return
    for t in metas[idx + 1:]:
        if t.code == meta.code and t.scope != "unknown":
            meta.scope = t.scope
            return
    for t in reversed(metas[:idx]):
        # a later same-code title that is already consolidated forces parent
        if t.code == meta.code and t.scope == "consolidated":
            meta.scope = "parent"
            return
        if t.code == meta.code and t.scope != "unknown":
            meta.scope = t.scope
            return


def statement_title(text: str) -> tuple[str | None, str, bool]:
    t = re.sub(r"\s+", "", text)
    if _DISTRACTOR.search(t) or len(t) > 40:
        return None, "unknown", False
    for table in (TITLE_PATTERNS, TRUNCATED_TITLES, _TITLE_FUZZY):
        for code, pat in table:
            if table is TITLE_PATTERNS:
                hit = pat.search(t)
            elif table is TRUNCATED_TITLES:
                hit = pat.fullmatch(t)
            else:
                # a fuzzy title must look like a heading: short, and printed
                # in the title area rather than in the table body
                hit = len(t) <= 14 and pat.search(t)
            if hit:
                scope = "unknown"
                if _SCOPE_PARENT.search(t):
                    scope = "parent"
                elif _SCOPE_CONSOL.search(t):
                    scope = "consolidated"
                return code, scope, bool(_CONTINUED.search(t))
    return None, "unknown", False


def title_scope_hint(lines: list[tuple[float, str]], y: float) -> str:
    """Scope implied by nearby title-block lines.

    Titles are not always printed as "合并资产负债表"; some reports print
    "资产负债表" with "合并" on the line below and the column captions just
    under it.  For a combined consolidated+parent table the first amount
    column is the consolidated one, so a nearby "合并" means consolidated.
    """
    for ly, text in lines:
        if abs(ly - y) <= 60:
            t = re.sub(r"\s+", "", text)
            if _SCOPE_PARENT.search(t) and "母公司" not in t[:0]:
                pass
    # a scope marker just below the title counts: combined tables print
    # "合并" as a sub-header under the year captions
    for ly, text in lines:
        if 0 <= ly - y <= 160:
            t = re.sub(r"\s+", "", text)
            if re.search(r"^母公司$|^母公司(?:报表)?$", t):
                return "parent"
            if re.search(r"^合并$|^合并(?:报表)?$", t):
                return "consolidated"
    for ly, text in lines:
        if abs(ly - y) <= 60:
            t = re.sub(r"\s+", "", text)
            if _SCOPE_PARENT.search(t):
                return "parent"
    return "unknown"


def scan_titles(document, ocr: bool = False) -> list[StatementMeta]:
    metas: list[StatementMeta] = []
    for pno in range(document.page_count):
        page = document[pno]
        candidates = _merge_title_fragments(_title_lines(page, ocr))
        for y, text in candidates:
            code, scope, cont = statement_title(text)
            if not code:
                continue
            if _CAPTION_HEAD.search(re.sub(r"\s+", "", text)):
                # "一、经营活动产生的现金流量" is a section caption
                continue
            if ocr and y > 420 and not _is_heading_line(text):
                continue
            if scope == "unknown":
                scope = title_scope_hint(candidates, y)
            # the same page can carry several OCR variants of one title; keep
            # the longest reading (the one that recognised the most)
            prev = next((m for m in metas
                         if m.page == pno + 1 and m.code == code
                         and abs(m.y - y) <= 12), None)
            if prev is not None:
                if len(text) > len(prev.title):
                    prev.title = text.strip()
                    prev.scope = scope
                    prev.continued = cont
                    prev.y = round(y, 1)
                continue
            if metas and metas[-1].page == pno + 1 and cont \
                    and metas[-1].code == code:
                continue  # "（续）" belongs to the previous title
            metas.append(StatementMeta(code, scope, text.strip(), cont,
                                       pno + 1, round(y, 1)))
    if ocr:
        _infer_missing_titles(document, metas)
        # a title recovered from the table body may duplicate one that OCR
        # read imperfectly on the same page; keep a single title per page
        _dedupe_page_titles(metas)
    # first resolve known non-continuation titles, then continuation pages
    # inherit from the title they continue
    for m in metas:
        if not m.continued:
            _resolve_scope(m, metas)
    if ocr:
        # scans frequently lose the "合并" prefix; the first table of each
        # statement is the consolidated one.  A page carrying two titles is a
        # combined table, so position is not informative there.
        for code in ("BS", "IS", "CF"):
            group = [m for m in metas if m.code == code and not m.continued]
            pages_seen: dict[int, int] = {}
            for m in group:
                pages_seen[m.page] = pages_seen.get(m.page, 0) + 1
            single = [m for m in group if pages_seen[m.page] == 1]
            if single and single[0].scope == "parent":
                single[0].scope = "consolidated"
    for i, m in enumerate(metas):
        if m.continued and m.scope == "unknown":
            for j in range(i - 1, -1, -1):
                if metas[j].code == m.code and not metas[j].continued:
                    m.scope = metas[j].scope
                    break
    # a lone same-code pair where both look consolidated: the second must be
    # the parent table (a report never prints consolidated twice in a row)
    for code in ("BS", "IS", "CF"):
        group = [m for m in metas if m.code == code and not m.continued]
        if (len(group) == 2 and group[0].scope == "consolidated"
                and group[1].scope == "consolidated"):
            group[1].scope = "parent"
    return metas


_CAPTION_HEAD = re.compile(
    r"^[一二三四五六七八九十\d]+[、.．]"
    r"|^(?:经营|投资|筹资)活动"
    r"|产生的现金流量$"
)


def _is_heading_line(text: str) -> bool:
    """True for a statement title, as opposed to a section caption.

    A caption like "一、经营活动产生的现金流量" is longer and starts with a
    numbering or an activity name; a title is a short bare statement name.
    """
    t = re.sub(r"\s+", "", text)
    if len(t) > 12 or re.match(r"^[一二三四五六七八九十\d]", t):
        return False
    return not re.search(r"产生的现金流量$", t)


def _dedupe_page_titles(metas: list[StatementMeta]) -> None:
    """Keep one title per page, preferring the one OCR actually read."""
    by_page: dict[int, list[StatementMeta]] = {}
    for m in metas:
        by_page.setdefault(m.page, []).append(m)
    keep: list[StatementMeta] = []
    for page, group in by_page.items():
        real = [m for m in group if m.title]
        pick = real[0] if real else group[0]
        keep.append(pick)
    metas[:] = sorted(keep, key=lambda m: (m.page, m.y))


def _infer_missing_titles(document, metas: list[StatementMeta]) -> None:
    """Recover a title that OCR destroyed entirely.

    A page whose table matches many standard accounts must be one of the
    three statements.  Which one is decided by the majority of its matched
    rows, and the page is registered as a title page so the normal grouping
    logic can continue.
    """
    from .pageparse import collect_rows, words_from_ocr

    titled_pages = {m.page for m in metas}
    for pno in range(1, document.page_count + 1):
        if pno in titled_pages:
            continue
        rows = collect_rows(words_from_ocr(document[pno - 1]), pno, None, None,
                            ocr=True)
        counts: dict[str, int] = {}
        for row in rows:
            if not row.numbers:
                continue
            for code in ("BS", "IS", "CF"):
                if match_account(row.label, code)[0]:
                    counts[code] = counts.get(code, 0) + 1
                    break
        if not counts:
            continue
        code, hits = max(counts.items(), key=lambda kv: kv[1])
        if hits < 8:
            continue
        # scope from the column captions ("（合并）" / "（母公司）"); if the
        # page offers no marker, leave it unknown for neighbour resolution
        scope = "unknown"
        lines = _title_lines(document[pno - 1], True)
        for _, text in lines:
            t = re.sub(r"\s+", "", text)
            if "母公司" in t:
                scope = "parent"
                break
            if "合并" in t:
                scope = "consolidated"
                break
        if scope == "unknown":
            # the tables of a statement appear consolidated-first, so the
            # first unrecovered same-code page is consolidated
            same = [m for m in metas if m.code == code]
            if not same:
                scope = "consolidated"
            elif same[-1].page == pno - 1:
                scope = "parent"
            else:
                scope = "consolidated"
        metas.append(StatementMeta(code, scope, "", False, pno, 0.0))
    metas.sort(key=lambda m: (m.page, m.y))


def _page_lines(page, ocr: bool) -> list[tuple[float, str]]:
    """Lines of a page, for titles and vicinity text."""
    if not ocr:
        return text_lines(page)
    words = words_from_ocr(page, psm=3)
    by_line: dict[int, list] = {}
    for w in words:
        by_line.setdefault(int(round(w.y0 / 6)), []).append(w)
    out = []
    for key in sorted(by_line):
        ws = sorted(by_line[key], key=lambda w: w.x0)
        out.append((ws[0].y0, "".join(w.text for w in ws)))
    return out


def _unit_lines(page) -> list[tuple[float, str]]:
    """OCR the title block more deeply, for the "单位：万元" line."""
    import io

    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=200)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    band = img.crop((0, 0, img.width, int(img.height * 0.55)))
    data = pytesseract.image_to_data(
        band, lang="chi_sim+eng", config="--psm 6",
        output_type=pytesseract.Output.DICT)
    items = []
    for i, text in enumerate(data["text"]):
        t = (text or "").strip()
        if not t:
            continue
        items.append((data["top"][i], data["left"][i], t))
    items.sort()
    out: list[list] = []
    for top, left, text in items:
        if out and abs(top - out[-1][0]) <= 8:
            out[-1][1].append((left, text))
        else:
            out.append([top, [(left, text)]])
    return [(top, "".join(t for _, t in sorted(parts))) for top, parts in out]


def _title_lines(page, ocr: bool) -> list[tuple[float, str]]:
    """Lines that may hold a statement title.

    Digital pages: the top of the text layer.  Scanned pages: an OCR pass of
    the top band only, which is both faster and more accurate than parsing the
    whole table on every page.
    """
    if not ocr:
        return text_lines(page)[:14]
    import io

    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=250)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    # the statement name and date sit in the upper third; cropping deeper
    # would pull in captions from the table body and neighbouring sections
    band = img.crop((0, 0, img.width, int(img.height * 0.30)))
    # psm 4 keeps each centred heading on its own line and still segments the
    # column headers below it; psm 6 merges the heading into one block and can
    # drop the title entirely on tightly spaced pages
    data = pytesseract.image_to_data(
        band, lang="chi_sim+eng", config="--psm 4",
        output_type=pytesseract.Output.DICT)
    sx = page.rect.width / band.width
    sy = page.rect.height / band.height
    items = []
    for i, text in enumerate(data["text"]):
        t = (text or "").strip()
        if not t:
            continue
        items.append((data["top"][i], data["left"][i],
                      data["width"][i], t))
    items.sort()
    # regroup OCR words into visual lines; block/paragraph ids are unreliable
    # in psm 4 where several centred headings share a line number
    out: list[list] = []
    for top, left, width, text in items:
        if out and abs(top - out[-1][0]) <= 8:
            out[-1][1].append((left, text))
        else:
            out.append([top, [(left, text)]])
    result = []
    for top, parts in out:
        parts.sort()
        result.append((top * sy, "".join(p[1] for p in parts)))
    return result


# ---------------------------------------------------------------------------
# page grouping
# ---------------------------------------------------------------------------

_STOP = re.compile(
    r"所有者权益变动表|审计报告|财务报表附注|主要会计数据|财务指标"
    r"|第[一二三四五六七八九十]+节"
)


def group_pages(document, meta: StatementMeta, titles: list[StatementMeta],
                ocr: bool = False) -> list[int]:
    """Pages belonging to the statement that starts at ``meta``."""
    same_page_next = [t for t in titles
                      if t.page == meta.page and t.y > meta.y + 8]
    # continuation titles of the same statement are not boundaries
    later = [t for t in titles if t.page > meta.page
             and not (t.continued and t.code == meta.code
                      and t.scope in (meta.scope, "unknown"))]
    if same_page_next:
        return [meta.page]
    limit = later[0].page - 1 if later else document.page_count
    pages = [meta.page]
    p = meta.page + 1
    while p <= limit:
        lines = _page_lines(document[p - 1], ocr)
        head = [re.sub(r"\s+", "", t) for _, t in lines[:8]]
        if any(_STOP.search(t) for t in head):
            break
        # a statement title for another statement ends the range
        other_title = False
        for t in head:
            code, _, _ = statement_title(t)
            if code and code != meta.code:
                other_title = True
        if other_title:
            break
        rows = collect_rows(
            words_from_ocr(document[p - 1]) if ocr
            else words_from_pdf(document[p - 1]),
            p, None, None, ocr=ocr)
        if sum(1 for r in rows if any(is_currency_like(c.text)
                                      for c in r.numbers)) < 1:
            break
        pages.append(p)
        p += 1
    return pages


# ---------------------------------------------------------------------------
# units and years
# ---------------------------------------------------------------------------

def detect_statement_unit(document, pages: list[int], ocr: bool = False
                          ) -> tuple[str, str | None]:
    texts: list[str] = []
    for p in pages[:3]:
        page = document[p - 1]
        if ocr:
            # scanned pages: the unit line sits below the date in the title
            # block, and the footer often repeats it.  OCR can break
            # "人民币万元" across lines, so spaces are stripped before match.
            unit_lines = [(y, t.replace(" ", "")) for y, t in _unit_lines(page)]
            texts.extend(t for _, t in unit_lines)
            # the unit label is often split across two OCR lines; join each
            # line with its successor so both halves are matched together
            texts.extend(a + b for (_, a), (_, b) in
                         zip(unit_lines, unit_lines[1:]))
            texts.extend(t.replace(" ", "") for _, t in
                         _page_lines(page, True)[-4:])
        else:
            lines = _page_lines(page, False)
            texts.extend(t for _, t in lines[:10])
            texts.extend(t for _, t in lines[-4:])
    found = detect_unit(texts)
    if found:
        return found[0], None
    return "元", "未识别到金额单位，按元处理"


def _report_year_from_name(document, meta: StatementMeta) -> str | None:
    """Year taken from the PDF's table of contents / first pages."""
    for pno in range(min(document.page_count, 3)):
        for _, text in _page_lines(document[pno], False)[:4]:
            m = YEAR_RE.search(text)
            if m:
                return m.group(1)
    return None


def resolve_years(header_text: str, title_area: str) -> dict[str, str]:
    years: dict[str, str] = {}
    cur = re.search(r"(20\d{2})\s*年", header_text)
    if cur:
        years["current"] = cur.group(1)
    if "current" not in years:
        m = DATE_RE.search(title_area) or YEAR_RE.search(title_area)
        if m:
            years["current"] = m.group(1)
    if "current" in years:
        prev = re.search(r"(20\d{2})", header_text.replace(years["current"], ""))
        years["prior"] = prev.group(1) if prev else str(int(years["current"]) - 1)
    return years


_CUR = re.compile(r"本年|本期|年末|期末")
_PRIOR = re.compile(r"上年|上期|年初|期初")


def column_role(text: str, report_year: str | None = None) -> str:
    """Classify a column caption as the current or prior period.

    Explicit wording ("本期"/"上期") wins; otherwise the caption's year is
    compared with the report's own year, so a 2023 report names 2023 本期 and
    2022 上期 correctly.
    """
    t = re.sub(r"\s+", "", text)
    cur = bool(_CUR.search(t))
    pri = bool(_PRIOR.search(t))
    if cur and not pri:
        return "current"
    if pri and not cur:
        return "prior"
    years_found = re.findall(r"(20\d{2})", t)
    if years_found and report_year:
        return "current" if report_year in years_found else "prior"
    return "unknown"


def column_scope(text: str) -> str:
    t = re.sub(r"\s+", "", text)
    if re.search(r"母公司|本公司", t):
        return "parent"
    if "合并" in t:
        return "consolidated"
    return "unknown"


def is_combined_header(col_headers: list[dict], ncols: int) -> bool:
    """True when the table carries consolidated and parent columns.

    Detected from explicit scope captions, or from an even column count where
    the year captions repeat (2024 | 2024 | 2023 | 2023).
    """
    scopes = {h["scope"] for h in col_headers}
    # both scopes named means consolidated and parent sit side by side
    if {"consolidated", "parent"} <= scopes:
        return True
    if ncols >= 4 and ncols % 2 == 0:
        roles = [h["role"] for h in col_headers]
        pairs = [tuple(roles[i:i + 2]) for i in range(0, ncols, 2)]
        return len(set(pairs)) == 1 or roles[0::2] == roles[1::2]
    return False


# ---------------------------------------------------------------------------
# column geometry
# ---------------------------------------------------------------------------

def cluster_columns(rows: list[RowCandidate], tol: float = 14.0
                    ) -> list[float]:
    """Right-edge positions of the numeric columns, left to right."""
    edges = sorted(c.x1 for r in rows for c in r.numbers
                   if is_currency_like(c.text))
    if not edges:
        return []
    clusters: list[list[float]] = []
    for x in edges:
        if clusters and x - clusters[-1][-1] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    # drop clusters that only appear once and are far from the main block
    clusters = [c for c in clusters if len(c) >= max(2, len(rows) // 10)] or clusters
    return [sum(c) / len(c) for c in clusters]


def assign_column(cell: Cell, centers: list[float], tol: float = 26.0
                  ) -> int | None:
    if not centers:
        return None
    best = min(range(len(centers)), key=lambda i: abs(centers[i] - cell.x1))
    return best if abs(centers[best] - cell.x1) <= tol else None


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass
class ExtractedRow:
    key: str
    canonical: str
    label: str
    method: str
    score: float
    page: int = 0
    y: float = 0.0
    values: dict[int, dict] = field(default_factory=dict)


@dataclass
class ExtractedStatement:
    code: str
    scope: str
    title: str
    pages: list[int]
    unit: str
    unit_scale: int
    columns: list[dict]
    years: dict[str, str]
    rows: list[ExtractedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    header_text: str = ""
    scanned: bool = False


def extract_statements(document, *, ocr: bool = False,
                       extra_aliases: dict[str, str] | None = None
                       ) -> list[ExtractedStatement]:
    titles = scan_titles(document, ocr)
    out: list[ExtractedStatement] = []
    for meta in titles:
        if meta.continued:
            continue
        out.extend(extract_one(document, meta, titles, ocr=ocr,
                               extra_aliases=extra_aliases))
    return out


def extract_one(document, meta: StatementMeta, titles: list[StatementMeta],
                *, ocr: bool = False,
                extra_aliases: dict[str, str] | None = None
                ) -> list[ExtractedStatement]:
    pages = group_pages(document, meta, titles, ocr)
    unit, unit_warn = detect_statement_unit(document, pages, ocr)

    per_page_rows: dict[int, list[RowCandidate]] = {}
    per_page_words: dict[int, list] = {}
    for p in pages:
        page = document[p - 1]
        words = words_from_ocr(page) if ocr else words_from_pdf(page)
        per_page_words[p] = words
        per_page_rows[p] = collect_rows(words, p, None, None, ocr=ocr)

    first_rows = per_page_rows[meta.page]
    header_lines, data_rows = _split_header(first_rows, meta)
    # on some scans the title block itself is OCR'd into the first data-like
    # rows; only rows below the header band count as data
    if header_lines:
        header_bottom = max(r.y1 for r in header_lines)
        for p in pages:
            per_page_rows[p] = [r for r in per_page_rows[p]
                                if p != meta.page or r.y0 >= header_bottom - 2]
    centers = cluster_columns([r for rows in per_page_rows.values()
                               for r in rows])
    ncols = len(centers)

    # header text per column, from the words in the header band
    col_headers = _column_headers(per_page_words[meta.page], meta,
                                  header_lines, centers)
    header_text = " ".join(h["text"] for h in col_headers) or " ".join(
        r.label for r in header_lines)

    # the report's own year comes from the page title/date; column captions
    # are then classified against it, so a 2023 report reads 2023 as current
    title_lines = _page_lines(document[meta.page - 1], ocr)[:8]
    title_area = " ".join(t for _, t in title_lines)
    report_year = None
    for _, text in title_lines:
        m = DATE_RE.search(text) or YEAR_RE.search(text)
        if m:
            report_year = m.group(1)
            break
    if not report_year:
        report_year = _report_year_from_name(document, meta)
    years = {"current": report_year} if report_year else {}
    if report_year:
        years["prior"] = str(int(report_year) - 1)
    header_years = resolve_years(header_text, title_area)
    for role, year in header_years.items():
        years.setdefault(role, year)
    if not report_year:
        m = DATE_RE.search(title_area) or YEAR_RE.search(title_area)
        report_year = m.group(1) if m else None
    # re-score roles now that the report year is known
    for h in col_headers:
        if h["role"] == "unknown":
            h["role"] = column_role(h["text"], report_year)

    scopes = _scope_layout(meta, col_headers, ncols)
    warnings = [unit_warn] if unit_warn else []
    results: list[ExtractedStatement] = []

    for scope in scopes:
        cols = [i for i, h in enumerate(col_headers) if h["scope"] == scope]
        if not cols:
            continue
        columns = []
        for j in cols:
            role = col_headers[j]["role"]
            columns.append({"index": j, "role": role,
                            "label": col_headers[j]["text"],
                            "year": years.get(role)})
        if any(c["role"] == "unknown" for c in columns):
            known = {c["role"] for c in columns} - {"unknown"}
            if known:
                # a caption OCR mangled ("_pP024...") leaves one column
                # unclassified; the other column already fixes the layout
                for c in columns:
                    if c["role"] == "unknown":
                        c["role"] = "prior" if "current" in known else "current"
            else:
                # years are lost together: the left column is the later year
                for i, c in enumerate(columns):
                    c["role"] = "current" if i == 0 else "prior"
            for c in columns:
                c["year"] = c["year"] or years.get(c["role"])
        elif not any(c["role"] != "unknown" for c in columns):
            columns[0]["role"] = "current"
            if len(columns) > 1:
                columns[1]["role"] = "prior"
            columns[0]["year"] = years.get("current")
            if len(columns) > 1:
                columns[1]["year"] = years.get("prior")

        stmt = ExtractedStatement(
            code=meta.code, scope=scope, title=meta.title, pages=pages,
            unit=unit, unit_scale=int(UNIT_SCALE[unit]),
            columns=columns, years=years, header_text=header_text.strip(),
            scanned=ocr, warnings=list(warnings),
        )
        seen: dict[str, ExtractedRow] = {}
        for p in pages:
            for row in per_page_rows[p]:
                if row in header_lines:
                    continue
                er = _extract_row(row, meta.code, centers, columns,
                                  scope, unit, extra_aliases)
                if er is None:
                    continue
                if er.key in seen:
                    prev = seen[er.key]
                    if len(er.values) > len(prev.values):
                        seen[er.key] = er
                    continue
                seen[er.key] = er
        stmt.rows = sorted(seen.values(), key=lambda r: BY_KEY[r.key].order)
        if not stmt.rows:
            stmt.warnings.append("未识别出标准科目，请人工复核")
        results.append(stmt)
    return results


def _split_header(rows: list[RowCandidate], meta: StatementMeta
                  ) -> tuple[list[RowCandidate], list[RowCandidate]]:
    """Split the page into header material and data rows.

    Data begins at the first row carrying a printed amount that is also
    followed by more amount rows (a title line with a stray figure is not the
    start of the table).  Everything above it is header material.
    """
    # the title line's own y is unreliable on scans, so the first *run* of
    # amount rows is what marks the table body
    for i, r in enumerate(rows):
        if not is_currency_like_row(r):
            continue
        window = rows[i:i + 6]
        if sum(1 for x in window if is_currency_like_row(x)) >= 3:
            # step back over a single caption-only row directly above; the
            # table often opens with a section head such as "流动资产："
            if i > 0 and not rows[i - 1].numbers and \
                    _looks_section_caption(rows[i - 1].label):
                i -= 1
            return rows[:i], rows[i:]
    for i, r in enumerate(rows):
        if r.y0 > meta.y + 2 and is_currency_like_row(r):
            return rows[:i], rows[i:]
    return rows, []


_HEADER_LABEL = re.compile(r"项目|附注|注释|行次|余额|金额|发生额|年度|本期|上期|数$")


_SECTION_CAPTION = re.compile(
    r"^(?:流动资产|非流动资产|流动负债|非流动负债|所有者权益|股东权益"
    r"|资产|负债|经营活动|投资活动|筹资活动)[^，。]{0,12}[:：]?$")


def _looks_section_caption(label: str) -> bool:
    t = re.sub(r"\s+", "", label or "")
    return bool(_SECTION_CAPTION.match(t))


def is_currency_like_row(row: RowCandidate) -> bool:
    return any(is_currency_like(c.text) for c in row.numbers)


def _column_headers(words, meta, header_rows, centers) -> list[dict]:
    """Header text overlaying each numeric column.

    Uses individual word boxes (not merged row labels) so that multi-level
    headers like "年末余额 | 年初余额" and combined "合并 / 母公司" sub-headers
    can be attributed to the right column.
    """
    # only the rows that name the columns; the company/title lines can sit
    # over the amount columns too and would pollute the attribution
    # header rows are those whose amount cells are column captions
    # ("期末余额", "合并") rather than printed figures, plus the label cell
    table_header = [r for r in header_rows
                    if is_header_like(r)]
    named = [r for r in table_header
             if _HEADER_LABEL.search(re.sub(r"\s+", "", r.label))] \
        or table_header
    if named:
        y_lo = min(r.y0 for r in named) - 2
        y_hi = max(r.y1 for r in named) + 3
    else:
        y_lo = min((r.y0 for r in header_rows), default=meta.y)
        y_hi = max((r.y1 for r in header_rows), default=meta.y) + 4
    band = [w for w in words if y_lo <= w.y0 <= y_hi
            and not is_currency_like(w.text) and w.text.strip()]
    # keep only the header words that sit above an amount column; a company
    # name centred on the page is not a column caption
    if centers:
        lo = min(centers) - min((b - a for a, b in zip(centers, centers[1:])),
                                default=120.0)
        band = [w for w in band if w.x1 >= lo]
    spacing = min((b - a for a, b in zip(centers, centers[1:])), default=120.0)
    tol = spacing * 0.45
    groups: list[list] = [[] for _ in centers]
    for w in band:
        if not centers:
            continue
        distances = [(abs(c - w.x1), i) for i, c in enumerate(centers)]
        dist, idx = min(distances)
        if dist <= tol:
            groups[idx].append(w)
    out = []
    for i in range(len(centers)):
        texts = [w.text for w in sorted(groups[i], key=lambda w: w.x0)]
        text = " ".join(texts)
        out.append({
            "index": i, "text": text,
            "role": column_role(text),
            "scope": column_scope(text),
        })
    return out


def is_header_like(row: RowCandidate) -> bool:
    """A table-header row: several column captions, no printed amounts."""
    if len(row.numbers) < 2:
        return False
    return not any(is_currency_like(c.text) for c in row.numbers)


def _col_bounds(centers: list[float], i: int) -> tuple[float, float]:
    left = (centers[i - 1] + centers[i]) / 2 if i > 0 else 0.0
    right = (centers[i] + centers[i + 1]) / 2 if i + 1 < len(centers) else 1e9
    return left, right


def _scope_layout(meta: StatementMeta, col_headers: list[dict],
                  ncols: int) -> list[str]:
    explicit = {h["scope"] for h in col_headers} & {"consolidated", "parent"}
    if meta.scope in {"consolidated", "parent"} and not is_combined_header(
            col_headers, ncols):
        # the page title names the scope; column captions can carry the
        # running-header text and must not override it
        for h in col_headers:
            h["scope"] = meta.scope
        return [meta.scope]
    if len(explicit) == 2 and is_combined_header(col_headers, ncols):
        for j, h in enumerate(col_headers):
            if h["scope"] == "unknown":
                h["scope"] = "consolidated" if j % 2 == 0 else "parent"
        return ["consolidated", "parent"]
    if not explicit and is_combined_header(col_headers, ncols):
        # the sub-headers were lost to OCR but the column pattern is the
        # combined one: even columns consolidated, odd columns parent
        for j, h in enumerate(col_headers):
            h["scope"] = "consolidated" if j % 2 == 0 else "parent"
        return ["consolidated", "parent"]
    if len(explicit) == 1:
        # one scope named in the header ("本年金额（合并）") applies to every
        # amount column of that table
        only = next(iter(explicit))
        for h in col_headers:
            h["scope"] = only
        return [only]
    # a four-column table is the combined layout even when the sub-headers
    # were lost to OCR: consolidated is the first of each year pair
    if ncols >= 4 and ncols % 2 == 0:
        for j, h in enumerate(col_headers):
            h["scope"] = "consolidated" if j % 2 == 0 else "parent"
        return ["consolidated", "parent"]
    if meta.scope in {"consolidated", "parent"}:
        for h in col_headers:
            h["scope"] = meta.scope
        return [meta.scope]
    for h in col_headers:
        h["scope"] = "consolidated"
    return ["consolidated"]


def _extract_row(row: RowCandidate, code: str, centers: list[float],
                 columns: list[dict], scope: str, unit: str,
                 extra_aliases: dict[str, str] | None) -> ExtractedRow | None:
    label = row.label.strip()
    if not label or len(label) < 2:
        return None
    key, method, score = match_account(label, code, extra_aliases)
    if not key:
        return None
    # a row with no amounts is either a section caption ("二、投资活动产生的
    # 现金流量") or a line item whose cells were all blank.  A caption only
    # ever matches fuzzily; a real blank line matches by name or alias.
    if not row.numbers and method == "fuzzy":
        return None
    per_share = key.endswith("_eps")
    values: dict[int, dict] = {}
    for cell in row.numbers:
        j = assign_column(cell, centers)
        if j is None:
            continue
        col = next((c for c in columns if c["index"] == j), None)
        if col is None:
            continue
        amount, blank = parse_amount(cell.text)
        conf = cell.conf
        if amount is None:
            value = None
            parse_error = not blank
        elif per_share:
            value = amount
            parse_error = False
        else:
            value = amount * UNIT_SCALE[unit]
            parse_error = False
        values[j] = {
            "value": None if value is None else str(value),
            "printed": cell.text,
            "raw_confidence": round(conf, 1),
            "confidence": _confidence(conf, amount is not None, blank,
                                      parse_error),
            "blank": blank,
            "parse_error": parse_error,
            "page": cell_page(row),
            "box": [round(cell.x0, 2), round(cell.y0, 2),
                    round(cell.x1, 2), round(cell.y1, 2)],
            "role": col["role"],
            "year": col["year"],
        }
    # an empty cell in a printed statement means zero, so every declared
    # column gets an entry; the UI shows these as blank rather than "0.00"
    for col in columns:
        j = col["index"]
        if j in values:
            continue
        values[j] = {
            "value": "0", "printed": "", "raw_confidence": 100.0,
            "confidence": 1.0, "blank": True, "parse_error": False,
            "page": row.page, "box": None,
            "role": col["role"], "year": col["year"],
            "implied_blank": True,
        }
    return ExtractedRow(key=key, canonical=BY_KEY[key].name, label=label,
                        method=method, score=score, page=row.page,
                        y=round(row.y0, 2), values=values)


def cell_page(row: RowCandidate) -> int:
    return row.page


LOW_CONFIDENCE = 0.85


def _confidence(ocr_conf: float, parsed: bool, blank: bool,
                parse_error: bool) -> float:
    """0-1 confidence that the extracted yuan value is right.

    Tesseract's word confidence saturates: a clean scan scores 88-100 for
    almost every word, so the mapping is stretched over that range — a 90 is
    already worth a second look on a long number, a 97 is usually clean.
    """
    base = 1.0
    if ocr_conf < 100:
        base = min(base, max(0.05, (ocr_conf - 70.0) / 28.0))
    if parse_error:
        base = min(base, 0.1)
    if blank:
        base = min(base, 0.95)
    return round(base, 3)
