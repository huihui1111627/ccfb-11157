"""Synthetic annual-report financial data with internal consistency.

Produces a two-year balance sheet, income statement and cash flow statement for
a listed company, in *yuan*.  Values are constructed so that subtotals,
totals and cross-statement links reconcile; scenario knobs let the corpus
generator emit deliberately broken statements for the 勾稽校验 feature.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

from .accounts import ACCOUNTS, BY_KEY
from .values import round_to_printed

D = Decimal


@dataclass
class Line:
    key: str
    values: dict[str, Decimal]  # year -> yuan
    kind: str = "item"          # item | subtotal | total


@dataclass
class Statement:
    code: str
    lines: list[Line] = field(default_factory=list)
    # how the "上年" column relates to this year's opening position
    prev_year_is_reported: bool = True

    def get(self, key: str, year: str) -> Decimal | None:
        for ln in self.lines:
            if ln.key == key:
                return ln.values.get(year)
        return None

    def set(self, key: str, year: str, value: Decimal) -> None:
        for ln in self.lines:
            if ln.key == key:
                ln.values[year] = value
                return
        self.lines.append(Line(key, {year: value}))


@dataclass
class Company:
    code: str
    name: str
    short_name: str
    years: tuple[str, str]           # (previous, current)
    bs: Statement
    bs_parent: Statement
    is_: Statement
    is_parent: Statement
    cf: Statement
    cf_parent: Statement
    scale: int = 1                   # display unit divisor (1 = 元)
    places: int = 2
    style: dict = field(default_factory=dict)

    def statement(self, code: str, scope: str = "consolidated") -> Statement:
        table = {
            ("BS", "consolidated"): self.bs,
            ("BS", "parent"): self.bs_parent,
            ("IS", "consolidated"): self.is_,
            ("IS", "parent"): self.is_parent,
            ("CF", "consolidated"): self.cf,
            ("CF", "parent"): self.cf_parent,
        }
        return table[(code, scope)]

    def to_dict(self) -> dict:
        out = {}
        for code in ("BS", "IS", "CF"):
            for scope in ("consolidated", "parent"):
                st = self.statement(code, scope)
                out[f"{code}.{scope}"] = {
                    ln.key: {y: str(v) for y, v in ln.values.items()}
                    for ln in st.lines
                }
        return out


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------

def _r(x, step=1000) -> Decimal:
    """Round to a plausible reporting precision (yuan)."""
    return D(int(round(float(x) / step)) * step)


def _pct(rng: random.Random, lo: float, hi: float) -> Decimal:
    return D(str(round(rng.uniform(lo, hi), 6)))


def _scale_around(rng: random.Random, base: float, lo: float, hi: float) -> Decimal:
    return D(str(base)) * _pct(rng, lo, hi)


def _apply_breakage(lines: list[Line], year: str, broken: set[str]) -> None:
    """Re-introduce the scenario's deliberate inconsistencies."""
    def k(key):
        for ln in lines:
            if ln.key == key:
                return ln.values.get(year)
        return None

    def setv(key, value):
        for ln in lines:
            if ln.key == key:
                ln.values[year] = value
                return

    ta, tl = k("total_assets") or D(0), k("total_liabilities") or D(0)
    te = k("total_equity") or D(0)
    if "assets" in broken:
        setv("total_assets", ta + _r(ta * D("0.013"), step=1000))
    if "balance" in broken:
        setv("total_liabilities_and_equity",
             tl + te + _r(ta * D("0.004"), step=1000))
    if "subtotal" in broken:
        tca = k("total_current_assets") or D(0)
        setv("total_current_assets", tca + _r(tca * D("0.007"), step=1000))
    if "equity_total" in broken:
        mi = k("minority_interests") or D(0)
        parent_eq = (k("total_equity_parent") or D(0)) + _r(te * D("0.01"),
                                                            step=1000)
        setv("total_equity_parent", parent_eq)
        setv("total_equity", parent_eq + mi)


def _broken_keys(broken: set[str]) -> set[str]:
    """Totals that a scenario asks to leave unreconciled."""
    mapping = {
        "balance": {"total_liabilities_and_equity"},
        "assets": {"total_assets"},
        "subtotal": {"total_current_assets"},
        "equity_total": {"total_equity_parent", "total_equity"},
    }
    out: set[str] = set()
    for name in broken:
        out |= mapping.get(name, set())
    return out


def _build_balance_sheet(rng: random.Random, year: str, scale: float,
                         broken: set[str], prior: dict | None = None,
                         with_minority: bool = True) -> list[Line]:
    """Build one column set of a balance sheet at ``scale`` (total assets)."""
    ta = _r(scale)
    ca_share = _pct(rng, 0.32, 0.68)
    tca = _r(ta * ca_share)
    tnca = ta - tca

    def split(total: Decimal, weights: list[float]) -> list[Decimal]:
        s = sum(weights)
        parts = [_r(total * (w / s)) for w in weights]
        parts[-1] = total - sum(parts[:-1])
        return parts

    cur_w = [_pct(rng, 0.5, 1.5) for _ in range(11)]   # 11 current-asset items
    cur = split(tca, cur_w)
    keys_cur = ["cash", "trading_fin_assets", "notes_receivable",
                "accounts_receivable", "receivables_financing", "prepayments",
                "other_receivables", "inventory", "contract_assets",
                "other_current_assets", "noncurrent_due_within_one_year"]
    ncur_w = [_pct(rng, 0.4, 1.4) for _ in range(9)]
    ncur = split(tnca, ncur_w)
    keys_ncur = ["fixed_assets", "construction_in_progress", "intangible_assets",
                 "goodwill", "long_term_equity_investment", "investment_property",
                 "right_of_use_assets", "deferred_tax_assets",
                 "other_noncurrent_assets"]

    values: dict[str, Decimal] = {}
    values.update(zip(keys_cur, cur))
    values.update(zip(keys_ncur, ncur))

    # liabilities: 40-65% of assets
    tl = _r(ta * _pct(rng, 0.40, 0.65))
    tcl = _r(tl * _pct(rng, 0.55, 0.85))
    tncl = tl - tcl
    cl_w = [_pct(rng, 0.5, 1.5) for _ in range(10)]
    cl = split(tcl, cl_w)
    keys_cl = ["short_term_borrowings", "notes_payable", "accounts_payable",
               "contract_liabilities", "employee_benefits_payable",
               "taxes_payable", "other_payables",
               "noncurrent_liab_due_within_one_year", "other_current_liabilities",
               "advance_receipts"]
    values.update(zip(keys_cl, cl))
    ncl_w = [_pct(rng, 0.4, 1.3) for _ in range(6)]
    ncl = split(tncl, ncl_w)
    keys_ncl = ["long_term_borrowings", "bonds_payable", "lease_liabilities",
                "deferred_income", "deferred_tax_liabilities",
                "provisions"]
    values.update(zip(keys_ncl, ncl))

    te = ta - tl
    pic = _r(te * _pct(rng, 0.18, 0.35))
    cap_res = _r(te * _pct(rng, 0.10, 0.30))
    surplus = _r(te * _pct(rng, 0.04, 0.10))
    mi = _r(te * _pct(rng, 0.02, 0.18)) if with_minority else D(0)
    oci = _r(te * _pct(rng, -0.01, 0.03), step=1000)
    treas = _r(te * _pct(rng, 0.0, 0.03))
    retained = te - pic - cap_res - surplus - mi - oci + treas
    values.update({
        "paid_in_capital": pic,
        "capital_reserve": cap_res,
        "surplus_reserve": surplus,
        "treasury_stock": treas,
        "other_comprehensive_income": oci,
        "minority_interests": mi,
        "retained_earnings": retained,
        "total_equity_parent": te - mi,
    })

    values["total_current_assets"] = tca
    values["total_noncurrent_assets"] = tnca
    values["total_assets"] = ta
    values["total_current_liabilities"] = tcl
    values["total_noncurrent_liabilities"] = tncl
    values["total_liabilities"] = tl
    values["total_equity"] = te
    values["total_liabilities_and_equity"] = tl + te

    # deliberately broken balances for the 勾稽校验 demo
    if "assets" in broken:
        values["total_assets"] = ta + _r(ta * D("0.013"), step=1000)
    if "balance" in broken:
        # an unbalanced sheet: the printed grand total is off by a
        # transcription-sized amount
        values["total_liabilities_and_equity"] = (
            tl + te + _r(ta * D("0.004"), step=1000))
    if "subtotal" in broken:
        values["total_current_assets"] = tca + _r(tca * D("0.007"), step=1000)
    if "equity_total" in broken:
        # a transposed figure in the equity subtotal, left unreconciled by
        # skipping the subtotal rebuild for this sheet
        values["total_equity_parent"] = (te - mi) + _r(te * D("0.01"),
                                                       step=1000)
        values["total_equity"] = values["total_equity_parent"] + mi

    order = [a.key for a in ACCOUNTS if a.statement == "BS"]
    return [Line(k, {year: values[k]}) for k in order if k in values]


def _build_income(rng: random.Random, year: str, bs_lines: list[Line],
                  prior_income: dict | None, broken: set[str]) -> list[Line]:
    bs = {ln.key: ln.values[year] for ln in bs_lines}
    revenue = _r(bs["total_assets"] * _pct(rng, 0.35, 0.95))
    cost = _r(revenue * _pct(rng, 0.55, 0.78))
    tax_sur = _r(revenue * _pct(rng, 0.005, 0.018))
    selling = _r(revenue * _pct(rng, 0.02, 0.07))
    admin = _r(revenue * _pct(rng, 0.03, 0.08))
    rnd = _r(revenue * _pct(rng, 0.02, 0.09))
    finance = _r(bs["total_liabilities"] * _pct(rng, 0.005, 0.03))
    interest_exp = _r(max(finance, 0) * _pct(rng, 0.6, 1.0))
    interest_inc = _r(max(finance, 0) * _pct(rng, 0.1, 0.5))
    other_income = _r(revenue * _pct(rng, 0.002, 0.012))
    inv_income = _r(revenue * _pct(rng, -0.002, 0.012))
    fv_change = _r(revenue * _pct(rng, -0.002, 0.004))
    credit_imp = -_r(bs["accounts_receivable"] * _pct(rng, 0.002, 0.02))
    asset_imp = -_r(bs["inventory"] * _pct(rng, 0.001, 0.012))
    disposal = _r(revenue * _pct(rng, -0.001, 0.004))

    operating = (revenue - cost - tax_sur - selling - admin - rnd - finance
                 + other_income + inv_income + fv_change + credit_imp
                 + asset_imp + disposal)
    nonop_in = _r(revenue * _pct(rng, 0.001, 0.005))
    nonop_out = _r(revenue * _pct(rng, 0.001, 0.004))
    total_profit = operating + nonop_in - nonop_out
    tax = _r(max(total_profit, D(0)) * _pct(rng, 0.15, 0.25))
    net = total_profit - tax
    minority = _r(net * _pct(rng, 0.02, 0.15))
    parent_net = net - minority
    has_discontinued = rng.random() < 0.15
    disc = _r(net * _pct(rng, -0.02, 0.05)) if has_discontinued else D(0)
    continuing = net - disc

    shares = bs["paid_in_capital"] / D(1) if bs["paid_in_capital"] else D(1)
    basic_eps = (parent_net / shares).quantize(D("0.0001"))
    diluted_eps = (basic_eps * D("0.998")).quantize(D("0.0001"))

    values = {
        "revenue": revenue, "cost_of_revenue": cost,
        "taxes_and_surcharges": tax_sur, "selling_expenses": selling,
        "admin_expenses": admin, "rnd_expenses": rnd,
        "finance_expenses": finance, "interest_expense": interest_exp,
        "interest_income": interest_inc, "other_income": other_income,
        "investment_income": inv_income,
        "associate_investment_income": _r(inv_income * D("0.4")),
        "fv_change_income": fv_change, "credit_impairment_loss": credit_imp,
        "asset_impairment_loss": asset_imp, "asset_disposal_income": disposal,
        "operating_profit": operating, "nonoperating_income": nonop_in,
        "nonoperating_expense": nonop_out, "total_profit": total_profit,
        "income_tax_expense": tax, "net_profit": net,
        "continuing_net_profit": continuing,
        "discontinued_net_profit": disc,
        "net_profit_parent": parent_net, "minority_profit": minority,
        "basic_eps": basic_eps, "diluted_eps": diluted_eps,
    }
    if "operating_profit" in broken:
        values["operating_profit"] = operating + _r(revenue * D("0.005"))
    if "net_profit_parent" in broken:
        values["net_profit_parent"] = parent_net + _r(revenue * D("0.002"))

    order = [a.key for a in ACCOUNTS if a.statement == "IS"]
    return [Line(k, {year: values[k]}) for k in order if k in values]


def _build_cashflow(rng: random.Random, year: str, is_lines: list[Line],
                    bs_lines_cur: list[Line], net_profit: Decimal,
                    broken: set[str], cash_begin: Decimal | None = None,
                    cash_end: Decimal | None = None
                    ) -> tuple[list[Line], dict]:
    revenue = next(ln.values[year] for ln in is_lines if ln.key == "revenue")
    sales_cash = _r(revenue * _pct(rng, 0.85, 1.05))
    tax_refund = _r(revenue * _pct(rng, 0.0, 0.01))
    other_in = _r(revenue * _pct(rng, 0.01, 0.05))
    op_in = sales_cash + tax_refund + other_in
    goods = _r(revenue * _pct(rng, 0.55, 0.8))
    employees = _r(revenue * _pct(rng, 0.08, 0.16))
    taxes = _r(revenue * _pct(rng, 0.02, 0.05))
    other_out = _r(revenue * _pct(rng, 0.03, 0.1))
    op_out = goods + employees + taxes + other_out

    inv_in_recovery = _r(revenue * _pct(rng, 0.0, 0.06))
    inv_income_cash = _r(revenue * _pct(rng, 0.001, 0.01))
    dispose = _r(revenue * _pct(rng, 0.0, 0.01))
    inv_in = inv_in_recovery + inv_income_cash + dispose
    buy_assets = _r(revenue * _pct(rng, 0.03, 0.12))
    inv_pay = _r(revenue * _pct(rng, 0.0, 0.1))
    inv_out = buy_assets + inv_pay

    fin_in_contrib = _r(revenue * _pct(rng, 0.0, 0.03))
    fin_in_borrow = _r(revenue * _pct(rng, 0.0, 0.08))
    fin_in_other = _r(revenue * _pct(rng, 0.0, 0.01))
    fin_in = fin_in_contrib + fin_in_borrow + fin_in_other
    fin_out_debt = _r(revenue * _pct(rng, 0.0, 0.09))
    fin_out_div = _r(revenue * _pct(rng, 0.005, 0.03))
    fin_out = fin_out_debt + fin_out_div

    net_op = op_in - op_out
    net_inv = inv_in - inv_out
    fx = _r(revenue * _pct(rng, -0.002, 0.002), step=1000)
    cash_bs = next(ln.values[year] for ln in bs_lines_cur if ln.key == "cash")
    # closing cash matches the balance-sheet 货币资金 and the opening balance
    # is the prior year's closing balance, as a real report has it
    end = cash_end if cash_end is not None else cash_bs
    begin = cash_begin if cash_begin is not None else _r(
        end * _pct(rng, 0.7, 1.0))
    target_change = end - begin
    # absorb the residual into 收到其他与筹资活动有关的现金 so that all three
    # activity nets plus FX add up to the cash movement
    fin_in_other += target_change - (net_op + net_inv + (fin_in - fin_out) + fx)
    fin_in = fin_in_contrib + fin_in_borrow + fin_in_other
    net_fin = fin_in - fin_out
    net_change = target_change
    cash_begin, cash_end = begin, end

    values = {
        "cash_from_sales": sales_cash, "tax_refund": tax_refund,
        "other_operating_cash_in": other_in,
        "total_operating_cash_in": op_in,
        "cash_for_goods": goods, "cash_for_employees": employees,
        "taxes_paid": taxes, "other_operating_cash_out": other_out,
        "total_operating_cash_out": op_out, "net_operating_cash": net_op,
        "cash_from_investment_recovery": inv_in_recovery,
        "cash_from_investment_income": inv_income_cash,
        "cash_from_disposal_assets": dispose,
        "cash_for_asset_purchase": buy_assets,
        "cash_for_investments": inv_pay,
        "total_investing_cash_in": inv_in, "total_investing_cash_out": inv_out,
        "net_investing_cash": net_inv,
        "cash_from_capital_contribution": fin_in_contrib,
        "cash_from_borrowings": fin_in_borrow,
        "other_financing_cash_in": fin_in_other,
        "cash_for_debt_repayment": fin_out_debt,
        "cash_for_dividends_interest": fin_out_div,
        "total_financing_cash_in": fin_in, "total_financing_cash_out": fin_out,
        "net_financing_cash": net_fin, "fx_effect": fx,
        "net_cash_increase": net_change, "cash_beginning": cash_begin,
        "cash_ending": cash_end,
    }
    if "cash_recon" in broken:
        # a transcription error: the printed net increase no longer ties to
        # the beginning/ending balances
        values["net_cash_increase"] = net_change + _r(revenue * D("0.01"),
                                                      step=1000)

    order = [a.key for a in ACCOUNTS if a.statement == "CF"]
    lines = [Line(k, {year: values[k]}) for k in order if k in values]
    carry = {"cash_beginning": cash_begin, "cash_ending": cash_end}
    return lines, carry


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

OPINION_TEMPLATES = [
    "我们认为，后附的财务报表在所有重大方面按照企业会计准则的规定编制，"
    "公允反映了{company}20{yy}年12月31日的合并及母公司财务状况以及20{yy}年度的"
    "合并及母公司经营成果和现金流量。",
    "我们审计了{company}财务报表，包括20{yy}年12月31日的合并及母公司资产负债表，"
    "20{yy}年度的合并及母公司利润表、合并及母公司现金流量表、合并及母公司所有者"
    "权益变动表以及相关财务报表附注。",
    "我们认为，上述财务报表在所有重大方面按照企业会计准则的规定编制，公允反映了"
    "{company}20{yy}年12月31日的财务状况以及20{yy}年度的经营成果和现金流量。",
]


def build_company(code: str, name: str, short_name: str, seed: int,
                  base_assets: float | None = None,
                  scale: int = 1, places: int = 2,
                  broken: set[str] | None = None,
                  years: tuple[str, str] | None = None) -> Company:
    rng = random.Random(seed)
    broken = broken or set()
    years = years or ("2023", "2024")
    base_assets = D(str(base_assets)) if base_assets else _scale_around(rng, 8e9, 0.4, 6.0)

    bs_cur = _build_balance_sheet(rng, years[1], base_assets, broken)
    prev_scale = base_assets * _pct(rng, 0.85, 1.12)
    bs_prev = _build_balance_sheet(rng, years[0], prev_scale, set())
    for ln in bs_cur:
        prev = {l.key: l.values[years[0]] for l in bs_prev}
        if ln.key in prev:
            ln.values[years[0]] = prev[ln.key]
    # subtotals of the prior column must also reconcile to the prior column
    keep = _broken_keys(broken)
    bs_cur = _reconcile_bs(bs_cur, years[0])
    bs_cur = _reconcile_bs(bs_cur, years[1], keep)
    if keep:
        # re-apply the broken figures after reconciliation
        _apply_breakage(bs_cur, years[1], broken)

    # parent-company statements: scale down and keep balance
    p_scale = base_assets * _pct(rng, 0.6, 0.95)
    bs_p_cur = _build_balance_sheet(rng, years[1], p_scale, set(),
                                    with_minority=False)
    bs_p_prev = _build_balance_sheet(rng, years[0],
                                     p_scale * _pct(rng, 0.85, 1.1), set(),
                                     with_minority=False)
    prev_p = {l.key: l.values[years[0]] for l in bs_p_prev}
    for ln in bs_p_cur:
        if ln.key in prev_p:
            ln.values[years[0]] = prev_p[ln.key]
    for ln in bs_p_cur:
        if ln.key == "minority_interests":
            ln.values = {y: D(0) for y in ln.values}
        elif ln.key == "total_equity_parent":
            ln.values = {y: next(x.values[y] for x in bs_p_cur
                                 if x.key == "total_equity")
                         for y in ln.values}
    bs_p_cur = _reconcile_bs(bs_p_cur, years[0])
    bs_p_cur = _reconcile_bs(bs_p_cur, years[1])

    is_cur = _build_income(rng, years[1], bs_cur, None, broken)
    is_prev = _build_income(rng, years[0], bs_cur, None, set())
    for ln in is_cur:
        prev = {l.key: l.values[years[0]] for l in is_prev}
        if ln.key in prev:
            ln.values[years[0]] = prev[ln.key]

    np_cur = next(ln.values[years[1]] for ln in is_cur if ln.key == "net_profit")
    np_prev = next(ln.values[years[0]] for ln in is_cur if ln.key == "net_profit")
    prev_bs_lines = [Line(l.key, {years[0]: l.values[years[0]]}) for l in bs_prev]

    prev_cash = next(ln.values[years[0]] for ln in prev_bs_lines
                     if ln.key == "cash")
    cur_cash = next(ln.values[years[1]] for ln in bs_cur if ln.key == "cash")
    cf_cur, carry_prev = _build_cashflow(rng, years[0], is_prev, prev_bs_lines,
                                         np_prev, set(), cash_end=prev_cash)
    cf_now, _ = _build_cashflow(rng, years[1], is_cur, bs_cur, np_cur, broken,
                                cash_begin=prev_cash, cash_end=cur_cash)
    # opening cash of current year == closing cash of prior year
    prev_rows = {l.key: l for l in cf_cur}
    for ln in cf_now:
        prev = prev_rows.get(ln.key)
        if prev is not None and years[0] in prev.values:
            ln.values[years[0]] = prev.values[years[0]]

    # parent cash flow / income: scaled variant
    is_p_cur = _build_income(rng, years[1], bs_p_cur, None, set())
    is_p_prev = _build_income(rng, years[0], bs_p_cur, None, set())
    prev_pi = {l.key: l.values[years[0]] for l in is_p_prev}
    for ln in is_p_cur:
        if ln.key in prev_pi:
            ln.values[years[0]] = prev_pi[ln.key]
    for ln in is_p_cur:
        if ln.key == "minority_profit":
            ln.values = {y: D(0) for y in ln.values}
        elif ln.key == "net_profit_parent":
            ln.values = {y: next(x.values[y] for x in is_p_cur
                                 if x.key == "net_profit")
                         for y in ln.values}
    cf_p_cur, _ = _build_cashflow(rng, years[1], is_p_cur, bs_p_cur,
                                  next(l.values[years[1]] for l in is_p_cur
                                       if l.key == "net_profit"), set())
    cf_p_prev, _ = _build_cashflow(rng, years[0], is_p_prev,
                                   [Line(l.key, {years[0]: l.values[years[0]]})
                                    for l in bs_p_prev],
                                   next(l.values[years[0]] for l in is_p_cur
                                        if l.key == "net_profit"), set())
    for ln in cf_p_cur:
        prev = {l.key: l.values[years[0]] for l in cf_p_prev}
        if ln.key in prev:
            ln.values[years[0]] = prev[ln.key]

    # a printed statement lists every standard line, using a dash for items
    # that are nil this year; add those zero rows so the corpus is realistic
    for stmt in (bs_cur, bs_p_cur, is_cur, is_p_cur, cf_now, cf_p_cur):
        _fill_zero_rows(stmt)

    return Company(
        code=code, name=name, short_name=short_name, years=years,
        bs=Statement("BS", bs_cur, True), bs_parent=Statement("BS", bs_p_cur, True),
        is_=Statement("IS", is_cur, True), is_parent=Statement("IS", is_p_cur, True),
        cf=Statement("CF", cf_now, True), cf_parent=Statement("CF", cf_p_cur, True),
        scale=scale, places=places,
    )


def _fill_zero_rows(lines: list[Line]) -> None:
    """Append a zero-valued row for every standard line not already present."""
    codes = {ln.key for ln in lines}
    if not codes:
        return
    statement = BY_KEY[next(iter(codes))].statement
    years = list(lines[0].values)
    for acc in ACCOUNTS:
        if acc.statement != statement or acc.key in codes:
            continue
        lines.append(Line(acc.key, {y: D(0) for y in years}))


def _reconcile_bs(lines: list[Line], year: str,
                  keep: set[str] | None = None) -> list[Line]:
    """Force the printed column to obey the balance-sheet arithmetic.

    Totals are recomputed from their components so the printed column holds
    exactly at the reporting precision.  Keys in ``keep`` are left untouched,
    which is how the deliberately-broken corpus documents stay broken.
    """
    keep = keep or set()
    def k(key):
        for ln in lines:
            if ln.key == key:
                return ln.values.get(year)
        return None

    def setv(key, value):
        for ln in lines:
            if ln.key == key:
                ln.values[year] = value
                return

    cur_keys = ["cash", "trading_fin_assets", "notes_receivable",
                "accounts_receivable", "receivables_financing", "prepayments",
                "other_receivables", "inventory", "contract_assets",
                "other_current_assets", "noncurrent_due_within_one_year"]
    ncur_keys = ["fixed_assets", "construction_in_progress", "intangible_assets",
                 "goodwill", "long_term_equity_investment", "investment_property",
                 "right_of_use_assets", "deferred_tax_assets",
                 "other_noncurrent_assets"]
    cl_keys = ["short_term_borrowings", "notes_payable", "accounts_payable",
               "contract_liabilities", "employee_benefits_payable",
               "taxes_payable", "other_payables",
               "noncurrent_liab_due_within_one_year", "other_current_liabilities",
               "advance_receipts"]
    ncl_keys = ["long_term_borrowings", "bonds_payable", "lease_liabilities",
                "deferred_income", "deferred_tax_liabilities", "provisions"]

    def sum_keys(keys):
        return sum((k(x) or D(0) for x in keys), D(0))

    setv("total_current_assets", sum_keys(cur_keys))
    setv("total_noncurrent_assets", sum_keys(ncur_keys))
    setv("total_assets", sum_keys(cur_keys) + sum_keys(ncur_keys))
    setv("total_current_liabilities", sum_keys(cl_keys))
    setv("total_noncurrent_liabilities", sum_keys(ncl_keys))
    setv("total_liabilities",
         sum_keys(cl_keys) + sum_keys(ncl_keys))
    if "total_equity_parent" not in keep:
        setv("total_equity_parent",
             (k("paid_in_capital") or D(0)) + (k("capital_reserve") or D(0))
             + (k("surplus_reserve") or D(0)) + (k("retained_earnings") or D(0))
             + (k("other_comprehensive_income") or D(0))
             - (k("treasury_stock") or D(0)))
    if "total_equity" not in keep:
        setv("total_equity", (k("total_equity_parent") or D(0))
             + (k("minority_interests") or D(0)))
    setv("total_liabilities_and_equity",
         (k("total_liabilities") or D(0)) + (k("total_equity") or D(0)))
    return lines
