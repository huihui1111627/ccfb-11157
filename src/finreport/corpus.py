"""Generate a synthetic annual-report corpus with ground truth.

Each document is a multi-page PDF containing the three primary statements
(consolidated and parent), deliberately varied in unit (元/千元/万元/百万元),
header style, row numbering, label wording, grid style and pagination.  Some
documents are rasterised into image-only ("scanned") PDFs.

For every PDF a sibling ``.truth.json`` records, per statement row, the
standard account key, the printed label and the *exact* value in yuan that a
perfect extractor should return, plus the page range occupied by the table.
"""
from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

from .accounts import ACCOUNTS, BY_KEY, STATEMENT_NAMES, normalize_label
from .data import Company, build_company
from .values import UNIT_SCALE

D = Decimal

PAGE_W, PAGE_H = A4

# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------

_FONT_SOURCES = [
    ("SongLight", "/System/Library/Fonts/Supplemental/Songti.ttc", 3),
    ("SongBold", "/System/Library/Fonts/Supplemental/Songti.ttc", 1),
    ("SongBlack", "/System/Library/Fonts/Supplemental/Songti.ttc", 0),
    ("HeiSC", "/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("Kai", "/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
]
_registered: list[str] = []


def register_fonts() -> list[str]:
    """Register CJK fonts; fall back to the first working one for the rest."""
    if _registered:
        return _registered
    for name, path, idx in _FONT_SOURCES:
        try:
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=idx))
            _registered.append(name)
        except Exception:
            continue
    if not _registered:
        raise RuntimeError("no CJK font available for the corpus generator")
    base = _registered[0]
    for alias in ("SongLight", "SongBold", "SongBlack", "HeiSC", "Kai"):
        if alias not in _registered:
            pdfmetrics.registerFont(TTFont(alias, _FONT_SOURCES[0][1],
                                           subfontIndex=_FONT_SOURCES[0][2]))
            _registered.append(alias)
    return _registered


# --------------------------------------------------------------------------
# style sampling
# --------------------------------------------------------------------------

HEADER_VARIANTS_BS = [
    ("期末余额", "期初余额"),
    ("年末余额", "年初余额"),
    ("2024年12月31日", "2023年12月31日"),
    ("本期末", "上期末"),
]
HEADER_VARIANTS_IS = [
    ("本期发生额", "上期发生额"),
    ("2024年度", "2023年度"),
    ("本年金额", "上年金额"),
    ("本期数", "上期数"),
]
HEADER_VARIANTS_CF = [
    ("本期发生额", "上期发生额"),
    ("2024年度", "2023年度"),
    ("本年金额", "上年金额"),
]


@dataclass
class Style:
    seed: int
    unit_label: str = "元"
    unit_scale: int = 1
    places: int = 2
    scope_mode: str = "separate"      # separate | combined
    grid: str = "grid"                # grid | header | none
    font_body: str = "SongLight"
    font_bold: str = "SongBold"
    size_title: float = 17
    size_body: float = 9.2
    size_num: float = 9.2
    row_h: float = 17.0
    blank_style: str = "dash"         # dash | zero | empty
    number_labels: bool = False
    section_style: str = "colon"      # colon | plain | none
    note_col: bool = True
    label_width: float = 246
    indent: float = 10
    forced_break: bool = True
    cover_pages: int = 3
    order: tuple[str, ...] = ("BS", "IS", "CF")
    use_alias: float = 0.35
    header_variant: int = 0
    footer_unit: bool = False
    title_scope_word: bool = True
    scanned: bool = False
    negative_paren: bool = True
    titles: dict = field(default_factory=dict)
    company_header: bool = True

    @classmethod
    def sample(cls, seed: int, scanned: bool = False) -> "Style":
        rng = random.Random(seed)
        unit = rng.choice(["元", "元", "千元", "万元", "万元", "百万元"])
        places = rng.choice([2, 2, 2, 0]) if unit != "元" else 2
        s = cls(seed=seed, scanned=scanned)
        s.unit_label = unit
        s.unit_scale = int(UNIT_SCALE[unit])
        s.places = places
        s.scope_mode = "combined" if rng.random() < 0.3 else "separate"
        s.grid = rng.choice(["grid", "grid", "header", "none"])
        body_fonts = ["SongLight", "SongLight", "HeiSC", "Kai"]
        s.font_body = rng.choice(body_fonts)
        s.font_bold = "SongBold"
        s.size_title = rng.choice([15, 16, 17, 18])
        s.size_body = rng.choice([8.4, 8.8, 9.2, 9.6, 10.0])
        s.size_num = s.size_body
        s.row_h = rng.choice([15.5, 16.5, 17.5, 18.5])
        s.blank_style = rng.choice(["dash", "dash", "zero", "empty"])
        s.number_labels = rng.random() < 0.45
        s.section_style = rng.choice(["colon", "plain", "none"])
        s.note_col = rng.random() < 0.6
        s.forced_break = rng.random() < 0.6
        s.cover_pages = rng.choice([1, 2, 3, 4])
        order = ["BS", "IS", "CF"]
        rng.shuffle(order)
        s.order = tuple(order)
        s.use_alias = rng.choice([0.0, 0.2, 0.35, 0.5])
        s.header_variant = rng.randrange(3)
        s.footer_unit = rng.random() < 0.2
        s.title_scope_word = rng.random() < 0.8
        s.negative_paren = rng.random() < 0.8
        s.company_header = rng.random() < 0.7
        if scanned:
            s.grid = rng.choice(["grid", "header"])
            s.places = min(s.places, 2)
        return s


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------

@dataclass
class Row:
    kind: str = "item"          # item | section | subtotal | total | blank
    key: str | None = None
    label: str = ""
    values: dict[str, Decimal] = field(default_factory=dict)  # (scope,year)
    indent: int = 0

    def first_for(self, scope: str, year: str):
        return self.values.get(f"{scope}:{year}")


def _fmt_amount(value: Decimal | None, style: Style,
                per_share: bool = False) -> str:
    """Format a stored yuan amount in the statement's declared unit."""
    if value is None:
        return ""
    if value == 0:
        if style.blank_style == "dash":
            return "-"
        if style.blank_style == "zero":
            return "0.00" if style.places else "0"
        return ""
    neg = value < 0
    if per_share:
        q = abs(value).quantize(D("0.0001"))
        places = 4
    else:
        q = abs(value) / D(style.unit_scale)
        places = style.places
        q = q.quantize(D(1).scaleb(-places))
    text = f"{q:,.{places}f}"
    if neg:
        text = f"({text})" if style.negative_paren else f"-{text}"
    return text


def _section_label(text: str, style: Style) -> str:
    if style.section_style == "colon":
        return f"{text}："
    if style.section_style == "plain":
        return text
    return ""


BS_SECTIONS = {
    "current_assets": "流动资产",
    "noncurrent_assets": "非流动资产",
    "current_liabilities": "流动负债",
    "noncurrent_liabilities": "非流动负债",
    "equity_parent": "所有者权益（或股东权益）",
}

IS_NOTES = {
    "interest_expense": "其中：利息费用",
    "interest_income": "利息收入",
    "associate_investment_income": "其中：对联营企业和合营企业的投资收益",
}

CF_SECTIONS = [
    ("operating_in", "一、经营活动产生的现金流量"),
    ("operating_out", None),
    ("investing_in", "二、投资活动产生的现金流量"),
    ("investing_out", None),
    ("financing_in", "三、筹资活动产生的现金流量"),
    ("financing_out", None),
]


def _account_label(acc, style: Style, rng: random.Random) -> str:
    label = acc.name
    if acc.aliases and rng.random() < style.use_alias:
        label = rng.choice(acc.aliases)
    if acc.key.endswith("_eps") and rng.random() < 0.5:
        label = f"{label}（元/股）"
    return label


def build_rows(company: Company, code: str, style: Style,
               rng: random.Random) -> list[Row]:
    accs = [a for a in ACCOUNTS if a.statement == code]
    rows: list[Row] = []
    last_section = None
    cf_section_shown = set()
    for acc in accs:
        section = acc.section
        if code == "BS" and section in BS_SECTIONS and section != last_section:
            label = _section_label(BS_SECTIONS[section], style)
            if label:
                rows.append(Row("section", label=label))
            last_section = section
        if code == "CF":
            for sec, title in CF_SECTIONS:
                if section == sec and title and sec not in cf_section_shown:
                    rows.append(Row("section", label=title
                                    if style.section_style != "none" else ""))
                    cf_section_shown.add(sec)
        label = _account_label(acc, style, rng)
        if code == "IS" and acc.key in IS_NOTES and rng.random() < 0.6 \
                and style.section_style != "none":
            label = IS_NOTES[acc.key]
        if acc.key == "treasury_stock" and rng.random() < 0.5:
            label = "减：库存股"
        if style.number_labels and code == "BS" and acc.section.endswith("total"):
            pass
        kind = "item"
        if acc.section.endswith("_total") or acc.key in {
                "total_assets", "total_liabilities", "total_equity",
                "total_liabilities_and_equity", "net_profit",
                "operating_profit", "total_profit", "net_operating_cash",
                "net_investing_cash", "net_financing_cash",
                "net_cash_increase"}:
            kind = "total" if acc.key in {
                "total_assets", "total_liabilities_and_equity", "net_profit",
                "net_cash_increase", "total_equity", "total_liabilities"} \
                else "subtotal"
        indent = 1 if acc.key in IS_NOTES else 0
        values = {}
        for scope in ("consolidated", "parent"):
            st = company.statement(code, scope)
            for year in company.years:
                values[f"{scope}:{year}"] = st.get(acc.key, year)
        rows.append(Row(kind, acc.key, label, values, indent))
    return rows


def quantize_rows(rows: list[Row], company: Company, code: str,
                  style: Style) -> dict:
    """Round values to what the chosen unit/decimals can express.

    Returns the truth mapping ``{scope: {key: {year: printed_yuan_string}}}``.
    """
    truth: dict[str, dict[str, dict[str, str]]] = {
        "consolidated": {}, "parent": {}}
    for row in rows:
        if row.kind == "section" or not row.key:
            continue
        acc = BY_KEY[row.key]
        per_share = acc.key.endswith("_eps")
        for scope in ("consolidated", "parent"):
            for year in company.years:
                k = f"{scope}:{year}"
                v = row.values.get(k)
                if v is None:
                    continue
                if per_share:
                    q = v.quantize(D("0.0001"))
                    row.values[k] = q
                    truth[scope].setdefault(row.key, {})[year] = str(q)
                else:
                    scale = D(style.unit_scale)
                    q = (v / scale).quantize(D(1).scaleb(-style.places))
                    yuan = q * scale
                    row.values[k] = yuan
                    truth[scope].setdefault(row.key, {})[year] = str(yuan)
    return truth


# --------------------------------------------------------------------------
# renderer
# --------------------------------------------------------------------------

@dataclass
class Header:
    rows: list[list[tuple[str, int, int]]]  # (text, colspan, rowspan)
    numeric_cols: list[tuple[str, str]]     # (scope, year)


def _headers(company: Company, code: str, style: Style) -> list[str]:
    variants = {"BS": HEADER_VARIANTS_BS, "IS": HEADER_VARIANTS_IS,
                "CF": HEADER_VARIANTS_CF}[code]
    pair = variants[style.header_variant % len(variants)]
    cur, prior = company.years[1], company.years[0]
    # single-pass substitution: chained replaces would rewrite the year a
    # second time ("2024"->"2023"->"2022")
    def subst(text: str) -> str:
        return re.sub(r"20\d{2}", lambda m: cur if m.group(0) == "2024" else prior,
                      text)
    return [subst(pair[0]), subst(pair[1])]


def build_header(company: Company, code: str, style: Style,
                 scope: str = "consolidated") -> Header:
    labels = _headers(company, code, style)
    y1, y0 = company.years[1], company.years[0]
    if style.scope_mode == "combined":
        cells = [("项目", 1, 2)]
        if style.note_col:
            cells.append(("附注", 1, 2))
        cells.append((labels[0], 2, 1))
        cells.append((labels[1], 2, 1))
        second = [("合并", 1, 1), ("母公司", 1, 1),
                  ("合并", 1, 1), ("母公司", 1, 1)]
        return Header([cells, second],
                      [("consolidated", y1), ("parent", y1),
                       ("consolidated", y0), ("parent", y0)])
    cells = [("项目", 1, 1)]
    if style.note_col:
        cells.append(("附注", 1, 1))
    cells.append((labels[0], 1, 1))
    cells.append((labels[1], 1, 1))
    return Header([cells], [(scope, y1), (scope, y0)])


# --------------------------------------------------------------------------
# page composition
# --------------------------------------------------------------------------

class DocRenderer:
    def __init__(self, path: Path, company: Company, style: Style,
                 rng: random.Random):
        register_fonts()
        self.path = path
        self.company = company
        self.style = style
        self.rng = rng
        self.c = rl_canvas.Canvas(str(path), pagesize=A4)
        self.page = 0
        self.page_total_placeholder = 0
        self.started = False
        self.remainder = 0.0

    # -- primitives -------------------------------------------------------
    def new_page(self, footer: bool = True) -> None:
        if self.started:
            self._footer()
            self.c.showPage()
        self.started = True
        self.page += 1
        self.y = PAGE_H - 56
        if self.style.company_header:
            self._text(56, PAGE_H - 40, f"{self.company.name} {self.company.years[1]} 年年度报告",
                       "HeiSC", 7.5, fill=0.45)
            self.c.setStrokeColorRGB(0.7, 0.7, 0.7)
            self.c.setLineWidth(0.4)
            self.c.line(56, PAGE_H - 46, PAGE_W - 56, PAGE_H - 46)
            self.y = PAGE_H - 62
        self._page_no = self.page

    def _footer(self) -> None:
        c = self.c
        c.setFont("SongLight", 8)
        c.setFillGray(0.35)
        c.drawCentredString(PAGE_W / 2, 32, f"第 {self.page} 页")
        c.setFillGray(0)
        if self.style.footer_unit:
            c.setFont("SongLight", 7.5)
            c.drawString(56, 32, f"单位：人民币{self.style.unit_label}")

    def _text(self, x, y, text, font, size, fill=0.0, align="left"):
        c = self.c
        c.setFont(font, size)
        c.setFillGray(fill)
        if align == "right":
            c.drawRightString(x, y, text)
        elif align == "center":
            c.drawCentredString(x, y, text)
        else:
            c.drawString(x, y, text)
        c.setFillGray(0)

    def title_block(self, code: str, scope: str | None) -> None:
        st = self.style
        if code == "BS":
            base = "资产负债表"
        elif code == "IS":
            base = "利润表"
        else:
            base = "现金流量表"
        if scope == "consolidated":
            title = f"合并{base}"
        elif scope == "parent":
            title = f"母公司{base}"
        else:
            title = base
        if not st.title_scope_word:
            title = base
        y = self.y
        self._text(PAGE_W / 2, y, self.company.name, "SongBold", st.size_title - 5,
                   align="center")
        self.y -= st.size_title + 2
        self._text(PAGE_W / 2, y - st.size_title, title, "SongBlack",
                   st.size_title, align="center")
        self.y -= st.size_title + 8
        year = self.company.years[1]
        if code == "BS":
            date_line = f"{year} 年 12 月 31 日"
        else:
            date_line = f"{year} 年度"
        self._text(PAGE_W / 2, self.y, date_line, "SongLight",
                   st.size_body + 1, align="center")
        self.y -= st.size_body + 8
        left_bits = [f"编制单位：{self.company.name}"]
        if self.style.unit_label:
            left_bits.append(f"单位：人民币{self.style.unit_label}"
                             if not self.style.footer_unit else "")
        left = "    ".join(b for b in left_bits if b)
        self._text(56, self.y, left, "SongLight", st.size_body - 0.6)
        self._text(PAGE_W - 56, self.y, f"单位：元" if False else "", "SongLight",
                   st.size_body - 0.6, align="right")
        self.y -= st.size_body + 7

    def continuation_title(self, code: str, scope: str | None) -> None:
        base = {"BS": "资产负债表", "IS": "利润表", "CF": "现金流量表"}[code]
        if scope == "consolidated":
            base = f"合并{base}"
        elif scope == "parent":
            base = f"母公司{base}"
        self._text(PAGE_W / 2, self.y, f"{base}（续）", "SongBlack",
                   self.style.size_title - 4, align="center")
        self.y -= self.style.size_title + 6

    # -- table geometry ---------------------------------------------------
    def columns(self, header: Header) -> dict:
        st = self.style
        left = 56
        right = PAGE_W - 56
        n = len(header.numeric_cols)
        note_w = 34 if st.note_col else 0
        # tighten the label column when there are four amount columns, as a
        # real four-column statement does
        label_w = st.label_width if n <= 2 else st.label_width - 34
        num_w = (right - left - label_w - note_w) / n
        cols = {
            "label": (left, left + label_w),
            "note": (left + label_w, left + label_w + note_w),
            "nums": [(left + label_w + note_w + i * num_w,
                      left + label_w + note_w + (i + 1) * num_w)
                     for i in range(n)],
            "left": left,
            "right": right,
        }
        return cols

    def header_height(self, header: Header) -> float:
        return (self.style.row_h - 1) * len(header.rows)

    def draw_header(self, header: Header, cols: dict) -> None:
        st = self.style
        c = self.c
        c.setLineWidth(0.5)
        c.setStrokeGray(0.25)
        rh = st.row_h - 1
        # compute cell rectangles by walking spans
        occupied: dict[tuple[int, int], int] = {}
        ncols = 1 + (1 if st.note_col else 0) + len(header.numeric_cols)
        def x_edges():
            e = [cols["label"][0], cols["label"][1]]
            if st.note_col:
                e.append(cols["note"][1])
            for a, b in cols["nums"]:
                e.append(b)
            return e
        edges = x_edges()
        for r, row_cells in enumerate(header.rows):
            col = 0
            for text, colspan, rowspan in row_cells:
                while occupied.get((r, col)):
                    col += 1
                x0 = edges[col]
                x1 = edges[min(col + colspan, ncols)]
                y1 = self.y - (self.header_height(header)) + (len(header.rows) - r) * rh
                # y positions: row r occupies band [top - (r+rowspan)*rh, top - r*rh]
                top = self.y
                ytop = top - r * rh
                ybot = top - (r + rowspan) * rh
                if st.grid in {"grid", "header"}:
                    c.rect(x0, ybot, x1 - x0, ytop - ybot, stroke=1, fill=0)
                for cc in range(col, col + colspan):
                    for rr in range(r, r + rowspan):
                        occupied[(rr, cc)] = 1
                self._text((x0 + x1) / 2, ybot + (ytop - ybot) / 2 - st.size_body * 0.35,
                           text, "SongBold", st.size_body - 0.4, align="center")
                col += colspan
        self.y -= self.header_height(header)
        c.setStrokeGray(0.0)
        if st.grid == "header":
            c.setLineWidth(0.8)
            c.line(cols["left"], self.y, cols["right"], self.y)

    def draw_row(self, row: Row, header: Header, cols: dict,
                 grid_top: bool = False) -> None:
        st = self.style
        c = self.c
        rh = st.row_h
        ybase = self.y - rh + (rh - st.size_body) / 2 + 1.2
        is_bold = row.kind in {"total", "subtotal", "section"}
        font = st.font_bold if is_bold else st.font_body
        if row.kind == "section":
            label = row.label
            self._text(cols["label"][0] + 2, ybase, label, st.font_bold,
                       st.size_body, align="left")
        elif row.kind == "blank":
            pass
        else:
            x = cols["label"][0] + 2 + row.indent * st.indent
            self._text(x, ybase, row.label, font, st.size_body)
            if st.note_col:
                note = self.note_for(row)
                if note:
                    self._text(cols["note"][1] - 4, ybase, note,
                               st.font_body, st.size_body - 1, align="right")
            for i, (scope, year) in enumerate(header.numeric_cols):
                v = row.values.get(f"{scope}:{year}")
                is_eps = bool(row.key and row.key.endswith("_eps"))
                txt = _fmt_amount(v, st, per_share=is_eps)
                xr = cols["nums"][i][1] - 3
                self._text(xr, ybase, txt, "Helvetica" if not is_bold else "Helvetica-Bold",
                           st.size_num, align="right")
        if st.grid == "grid":
            c.setLineWidth(0.35)
            c.setStrokeGray(0.45)
            c.rect(cols["left"], self.y - rh, cols["right"] - cols["left"], rh,
                   stroke=1, fill=0)
            c.setStrokeGray(0.0)
        elif row.kind == "total":
            c.setLineWidth(0.6)
            c.line(cols["left"], self.y, cols["right"], self.y)
            c.line(cols["left"], self.y - rh, cols["right"], self.y - rh)
        self.y -= rh

    def note_for(self, row: Row) -> str:
        if row.kind == "section" or not row.key:
            return ""
        n = (BY_KEY[row.key].order // 10) % 40 + 1
        style = self.rng.choice(["五、%d", "%d", "注%d", "（%d）"])
        return style % n

    def page_break_allowed(self) -> bool:
        return self.y < 90 + self.style.row_h

    # -- statement --------------------------------------------------------
    def draw_statement(self, code: str, scope: str | None, header: Header,
                       rows: list[Row],
                       force_split: bool = False) -> tuple[int, int]:
        if self.started and self.y < PAGE_H - 100:
            self.new_page()
        first = self.page
        self.title_block(code, scope)
        self.draw_header(header, self.columns(header))
        # how many rows fit before the natural page break; a "forced" break
        # just ends this page a few rows early, as a typesetter would
        capacity = int((self.y - 96) // self.style.row_h)
        force_split_at = None
        if force_split and 8 < capacity < len(rows):
            jitter = self.rng.randrange(-3, 0)
            force_split_at = max(8, capacity + jitter)
        for i, row in enumerate(rows):
            if self.page_break_allowed():
                # the natural break already ends the page here; never force a
                # second break on the same row or the page would be header-only
                force_split_at = None
                self.new_page()
                self.continuation_title(code, scope)
                self.draw_header(header, self.columns(header))
            if force_split_at is not None and i == force_split_at:
                force_split_at = None
                self.new_page()
                self.continuation_title(code, scope)
                self.draw_header(header, self.columns(header))
            self.draw_row(row, header, self.columns(header))
        return first, self.page

    # -- closing ----------------------------------------------------------
    def finish(self) -> int:
        if self.started:
            self._footer()
            self.c.showPage()
        self.c.save()
        return self.page


# --------------------------------------------------------------------------
# extra pages
# --------------------------------------------------------------------------

def cover_page(r: DocRenderer) -> None:
    c, st, co = r.c, r.style, r.company
    r.new_page()
    c.setFillGray(0)
    r._text(PAGE_W / 2, PAGE_H - 210, co.name, "SongBlack", 24, align="center")
    r._text(PAGE_W / 2, PAGE_H - 260, f"{co.years[1]} 年年度报告", "HeiSC", 20,
            align="center")
    r._text(PAGE_W / 2, PAGE_H - 300, f"股票代码：{co.code}", "SongLight", 12,
            align="center")
    r._text(PAGE_W / 2, 140, f"二〇{_cn_year(co.years[1])}年三月", "SongLight", 12,
            align="center")


def _cn_year(y: str) -> str:
    digits = "〇一二三四五六七八九"
    return "".join(digits[int(d)] for d in y)


def toc_page(r: DocRenderer) -> None:
    r.new_page()
    r._text(PAGE_W / 2, PAGE_H - 120, "目  录", "SongBlack", 18, align="center")
    items = ["第一节  重要提示、目录和释义", "第二节  公司简介和主要财务指标",
             "第三节  管理层讨论与分析", "第四节  公司治理",
             "第十节  财务报告"]
    y = PAGE_H - 180
    for i, it in enumerate(items):
        r._text(110, y, it, "SongLight", 11)
        r._text(PAGE_W - 110, y, str(4 + i * 7), "Helvetica", 11, align="right")
        y -= 30


def key_figures_page(r: DocRenderer) -> None:
    """主要会计数据 -- a distractor table that must not be read as a statement."""
    c, st, co = r.c, r.style, r.company
    r.new_page()
    r._text(PAGE_W / 2, PAGE_H - 110, "主要会计数据和财务指标", "SongBlack", 15,
            align="center")
    revenue = co.is_.get("revenue", co.years[1])
    nparent = co.is_.get("net_profit_parent", co.years[1])
    assets = co.bs.get("total_assets", co.years[1])
    rows = [
        ("营业收入（元）", revenue, revenue * D("0.92")),
        ("归属于上市公司股东的净利润（元）", nparent, nparent * D("0.88")),
        ("总资产（元）", assets, assets * D("0.95")),
        ("基本每股收益（元/股）", co.is_.get("basic_eps", co.years[1]),
         D("1.2345")),
    ]
    y = PAGE_H - 170
    r._text(90, y, "项目", "SongBold", 10)
    r._text(360, y, co.years[1], "SongBold", 10, align="right")
    r._text(480, y, co.years[0], "SongBold", 10, align="right")
    y -= 8
    c.line(90, y, 480, y)
    y -= 20
    for label, cur, prev in rows:
        r._text(90, y, label, "SongLight", 10)
        for x, v in ((360, cur), (480, prev)):
            txt = f"{v:,.4f}" if abs(v) < 1000 else f"{v:,.2f}"
            r._text(x, y, txt, "Helvetica", 10, align="right")
        y -= 22


def equity_change_page(r: DocRenderer) -> None:
    """所有者权益变动表 -- visually similar to a balance sheet, a distractor."""
    c, st, co = r.c, r.style, r.company
    r.new_page()
    r._text(PAGE_W / 2, PAGE_H - 110, "合并所有者权益变动表", "SongBlack", 15,
            align="center")
    y = PAGE_H - 160
    heads = ["项目", "实收资本", "资本公积", "盈余公积", "未分配利润", "所有者权益合计"]
    xs = [70, 220, 300, 380, 450, 530]
    for h, x in zip(heads, xs):
        r._text(x, y, h, "SongBold", 8.5, align="right" if x > 100 else "left")
    y -= 6
    c.line(70, y, 560, y)
    y -= 18
    lines = ["一、上年年末余额", "加：会计政策变更", "二、本年年初余额",
             "三、本年增减变动金额", "（一）综合收益总额", "（二）所有者投入和减少资本",
             "四、本年年末余额"]
    for i, label in enumerate(lines):
        r._text(70, y, label, "SongLight", 8.5)
        row = [co.bs.get(k, co.years[1]) for k in
               ("paid_in_capital", "capital_reserve", "surplus_reserve",
                "retained_earnings", "total_equity")]
        for x, v in zip(xs[1:], row):
            vv = v / D(str(st.unit_scale)) if v is not None else D(0)
            r._text(x, y, f"{vv:,.{st.places}f}", "Helvetica", 8.5, align="right")
        y -= 18
    r._text(70, y - 10, "（本表数据与资产负债表相关项目一致）", "SongLight", 8)


def audit_page(r: DocRenderer) -> None:
    c, co = r.c, r.company
    r.new_page()
    r._text(PAGE_W / 2, PAGE_H - 120, "审计报告", "SongBlack", 16, align="center")
    text = (
        "毕马威华振审字第 2500001 号\n\n"
        f"{co.name}全体股东：\n\n"
        "一、审计意见\n"
        f"我们审计了{co.name}（以下简称“公司”）财务报表，包括"
        f"{co.years[1]}年12月31日的合并及母公司资产负债表，"
        f"{co.years[1]}年度的合并及母公司利润表、合并及母公司现金流量表、"
        "合并及母公司所有者权益变动表以及相关财务报表附注。\n\n"
        "我们认为，后附的财务报表在所有重大方面按照企业会计准则的规定编制，"
        f"公允反映了公司{co.years[1]}年12月31日的合并及母公司财务状况以及"
        f"{co.years[1]}年度的合并及母公司经营成果和现金流量。\n\n"
        "二、形成审计意见的基础\n"
        "我们按照中国注册会计师审计准则的规定执行了审计工作。审计报告的"
        "“注册会计师对财务报表审计的责任”部分进一步阐述了我们在这些准则下的责任。"
    )
    y = PAGE_H - 170
    for para in text.split("\n"):
        for line in _wrap(para, 44):
            r._text(80, y, line, "SongLight", 10.5)
            y -= 18
        y -= 4


def _wrap(text: str, width: int) -> list[str]:
    out, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= width:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out or [""]


def notes_page(r: DocRenderer) -> None:
    c, co = r.c, r.company
    r.new_page()
    r._text(70, PAGE_H - 100, "财务报表附注", "SongBlack", 15)
    lines = [
        "一、公司基本情况",
        f"{co.name}（以下简称“本公司”）系依照《中华人民共和国公司法》设立的股份有限公司。",
        "二、财务报表的编制基础",
        "本财务报表按照财政部颁布的企业会计准则及其应用指南和解释编制。",
        "三、重要会计政策及会计估计",
        "1. 收入确认：本公司在履行了合同中的履约义务，即在客户取得相关商品控制权时确认收入。",
        "2. 金融工具：本公司以预期信用损失为基础，对以摊余成本计量的金融资产计提减值准备。",
    ]
    y = PAGE_H - 140
    for line in lines:
        for sub in _wrap(line, 46):
            r._text(70, y, sub, "SongLight", 10)
            y -= 17
        y -= 5
    for i in range(6):
        co_name = f"{co.short_name}第{i + 1}页"
        r._text(70, y, co_name, "SongLight", 9)
        y -= 16


# --------------------------------------------------------------------------
# document assembly
# --------------------------------------------------------------------------

def build_document(out_dir: Path, doc_id: str, company: Company,
                   style: Style) -> dict:
    rng = random.Random(style.seed * 7919 + 13)
    pdf_path = out_dir / f"{doc_id}.pdf"
    renderer = DocRenderer(pdf_path, company, style, rng)

    cover_page(renderer)
    if style.cover_pages >= 2:
        toc_page(renderer)
    if style.cover_pages >= 3:
        audit_page(renderer)
    if style.cover_pages >= 4:
        notes_page(renderer)
    key_figures_page(renderer)

    statements = []
    for code in style.order:
        if style.scope_mode == "combined":
            rows = build_rows(company, code, style, rng)
            truth = quantize_rows(rows, company, code, style)
            header = build_header(company, code, style)
            renderer.new_page()
            first, last = renderer.draw_statement(code, None, header, rows,
                                                  force_split=style.forced_break)
            statements.append({
                "code": code, "scope": "combined", "title": STATEMENT_NAMES[code],
                "page_start": first, "page_end": last,
                "columns": [{"scope": s, "year": y} for s, y in header.numeric_cols],
                "rows": _truth_rows(rows, truth, ("consolidated", "parent")),
                "truth": truth,
            })
        else:
            for scope in ("consolidated", "parent"):
                rows = build_rows(company, code, style, rng)
                truth = quantize_rows(rows, company, code, style)
                header = build_header(company, code, style, scope=scope)
                _scope_header(header, style, scope)
                renderer.new_page()
                first, last = renderer.draw_statement(code, scope, header, rows,
                                                      force_split=style.forced_break)
                statements.append({
                    "code": code, "scope": scope,
                    "title": _title_for(code, scope, style),
                    "page_start": first, "page_end": last,
                    "columns": [{"scope": scope, "year": company.years[1]},
                                {"scope": scope, "year": company.years[0]}],
                    "rows": _truth_rows(rows, truth, (scope,)),
                    "truth": {scope: truth[scope]},
                })
    equity_change_page(renderer)
    total_pages = renderer.finish()

    meta = {
        "doc_id": doc_id,
        "pdf": pdf_path.name,
        "company": {"code": company.code, "name": company.name,
                    "short_name": company.short_name},
        "years": list(company.years),
        "unit": style.unit_label,
        "unit_scale": style.unit_scale,
        "places": style.places,
        "scope_mode": style.scope_mode,
        "scanned": style.scanned,
        "pages": total_pages,
        "layout": {
            "grid": style.grid, "font": style.font_body,
            "size": style.size_body, "row_h": style.row_h,
            "note_col": style.note_col, "blank": style.blank_style,
            "section_style": style.section_style,
            "header_variant": style.header_variant,
            "footer_unit": style.footer_unit,
            "title_scope_word": style.title_scope_word,
        },
        "statements": statements,
    }
    return meta


def _scope_header(header: Header, style: Style, scope: str) -> None:
    if not style.title_scope_word:
        lbl = "合并" if scope == "consolidated" else "母公司"
        cell = header.rows[0][-2]
        header.rows[0][-2] = (f"{cell[0]}（{lbl}）", cell[1], cell[2])


def _title_for(code: str, scope: str, style: Style) -> str:
    base = STATEMENT_NAMES[code]
    if not style.title_scope_word:
        return base
    return ("合并" if scope == "consolidated" else "母公司") + base


def _truth_rows(rows: list[Row], truth: dict, scopes: tuple[str, ...],
                blank_style: str = "dash") -> list[dict]:
    out = []
    for row in rows:
        if row.kind == "section" or not row.key:
            continue
        acc = BY_KEY[row.key]
        out.append({
            "key": row.key,
            "name": acc.name,
            "label_printed": row.label,
            "section": acc.section,
            "per_share": acc.key.endswith("_eps"),
            "values": {f"{s}:{y}": str(row.values.get(f"{s}:{y}"))
                       for s in scopes for y in
                       sorted({k.split(":", 1)[1] for k in row.values
                               if k.startswith(s + ":")})},
        })
    return out


# --------------------------------------------------------------------------
# scanned conversion
# --------------------------------------------------------------------------

def rasterize(src: Path, dst: Path, dpi: int = 200, rotate: float = 0.0,
              noise: float = 0.0) -> None:
    import fitz
    from PIL import Image, ImageFilter
    import io

    doc = fitz.open(src)
    out = fitz.open()
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
        if rotate:
            img = img.rotate(rotate, resample=Image.BICUBIC, fillcolor=255,
                             expand=False)
        if noise:
            from PIL import ImageEnhance
            img = ImageEnhance.Contrast(img).enhance(1.0 - noise)
            img = img.filter(ImageFilter.GaussianBlur(0.3))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82)
        buf.seek(0)
        rect = fitz.Rect(0, 0, page.rect.width, page.rect.height)
        newpage = out.new_page(width=rect.width, height=rect.height)
        newpage.insert_image(rect, stream=buf.read())
    out.save(dst)
    doc.close()
    out.close()
