"""Cross-checks (勾稽关系) over an extracted report.

Every rule returns a zero-or-more findings, each carrying a rounding
tolerance derived from the report's declared unit (a 万元 report can only
reconcile to 10,000 yuan).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .accounts import BY_KEY, SECTION_ACCOUNTS

D = Decimal


@dataclass
class CheckItem:
    key: str
    value: Decimal | None
    page: int | None = None
    box: list | None = None


@dataclass
class Finding:
    rule: str
    label: str
    statement: str
    role: str                 # current | prior
    year: str | None
    expected: Decimal
    actual: Decimal
    diff: Decimal
    tolerance: Decimal
    status: str               # ok | warning | error
    pages: list[int]
    participants: list[dict]
    message: str = ""


def _sum(vals):
    present = [v for v in vals if v is not None]
    if not present:
        return None
    return sum(present, D(0))


def _tol(unit_scale: int, places: int = 2, n_items: int = 1) -> Decimal:
    """Rounding tolerance for a sum of ``n_items`` printed amounts.

    Each printed amount is rounded to the report's precision, so a sum of n
    terms can drift by n half-steps; the extra printed unit absorbs the
    rounding of the total itself.
    """
    step = D(max(unit_scale, 1))
    half = step / (D(10) ** places) / 2
    return half * D(n_items) + step


@dataclass
class ReportView:
    """Normalised access to extracted statements."""
    statements: dict[str, "ExtractedStatement"]  # code -> stmt
    unit_scale: int = 1
    places: int = 2

    def value(self, code: str, key: str, role: str) -> tuple[Decimal | None, dict | None]:
        st = self.statements.get(code)
        if not st:
            return None, None
        for col in st.columns:
            if col["role"] == role:
                break
        else:
            return None, None
        for row in st.rows:
            if row.key != key:
                continue
            v = row.values.get(col["index"])
            if not v:
                return None, None
            raw = v.get("value")
            if raw in (None, ""):
                return None, v
            return D(raw), v
        return None, None


RULES = []

# rules whose mismatch is normal in real filings (restricted cash, parent-only
# differences); they are surfaced for review but never fail the report
SOFT_RULES = {"cross_statement"}


def rule(name, label):
    def deco(fn):
        fn.rule_name = name
        fn.rule_label = label
        RULES.append(fn)
        return fn
    return deco


def _finding(rule, label, statement, role, year, expected, actual, tol,
             participants, pages, message="") -> Finding:
    diff = actual - expected
    if abs(diff) <= tol:
        status = "ok"
    elif abs(diff) <= tol * 10:
        status = "warning"
    else:
        status = "error"
    if rule in SOFT_RULES and status == "error":
        status = "warning"
    return Finding(rule=rule, label=label, statement=statement, role=role,
                   year=year, expected=expected, actual=actual, diff=diff,
                   tolerance=tol, status=status, pages=pages,
                   participants=participants, message=message)


@rule("bs_balance", "资产 = 负债 + 所有者权益")
def check_bs_balance(view: ReportView) -> list[Finding]:
    out = []
    st = view.statements.get("BS")
    if not st:
        return out
    for col in st.columns:
        role = col["role"]
        ta, ta_v = view.value("BS", "total_assets", role)
        tl, _ = view.value("BS", "total_liabilities", role)
        te, _ = view.value("BS", "total_equity", role)
        tle, tle_v = view.value("BS", "total_liabilities_and_equity", role)
        if ta is None:
            continue
        if tl is not None and te is not None:
            expected = tl + te
            actual = tle if tle is not None else ta
            label = "负债和所有者权益总计" if tle is not None else "资产总计"
            out.append(_finding(
                "bs_balance", label, "BS", role, col.get("year"),
                expected, actual, _tol(view.unit_scale, view.places, 2),
                _pax(view, "BS", ["total_liabilities", "total_equity",
                                  label == "资产总计" and "total_assets"
                                  or "total_liabilities_and_equity"], role),
                [st.pages[0]],
                "资产总计与负债及所有者权益总计不相等"))
    return out


@rule("bs_subtotals", "小计 = 明细之和 (资产负债表)")
def check_bs_subtotals(view: ReportView) -> list[Finding]:
    out = []
    st = view.statements.get("BS")
    if not st:
        return out
    groups = [
        ("total_current_assets", "current_assets"),
        ("total_noncurrent_assets", "noncurrent_assets"),
        ("total_current_liabilities", "current_liabilities"),
        ("total_noncurrent_liabilities", "noncurrent_liabilities"),
        ("total_liabilities", None),
        ("total_equity", None),
        ("total_equity_parent", "equity_parent"),
    ]
    for total_key, section in groups:
        members = (SECTION_ACCOUNTS.get(section, []) if section else None)
        if members is None:
            if total_key == "total_liabilities":
                members = (SECTION_ACCOUNTS["current_liabilities"]
                           + SECTION_ACCOUNTS["noncurrent_liabilities"])
            elif total_key == "total_equity":
                members = (SECTION_ACCOUNTS["equity_parent"]
                           + SECTION_ACCOUNTS["equity_minority"])
            else:
                continue
        sign_map = {a.key: a.sign for a in
                    (BY_KEY[k] for k in members)}
        members = [m for m in members if m != total_key]
        for col in st.columns:
            role = col["role"]
            total, total_v = view.value("BS", total_key, role)
            if total is None:
                continue
            parts, participants = [], []
            for m in members:
                v, vv = view.value("BS", m, role)
                if v is None:
                    continue
                parts.append(v * sign_map.get(m, 1))
                participants.append({
                    "key": m, "value": str(v), "sign": sign_map.get(m, 1),
                    "page": (vv or {}).get("page"), "box": (vv or {}).get("box"),
                })
            if not parts:
                continue
            participants.append({"key": total_key, "value": str(total),
                                 "sign": 1,
                                 "page": (total_v or {}).get("page"),
                                 "box": (total_v or {}).get("box")})
            out.append(_finding(
                "bs_subtotals", BY_KEY[total_key].name, "BS", role,
                col.get("year"), _sum(parts), total,
                _tol(view.unit_scale, view.places, len(parts)),
                participants, [st.pages[0]],
                f"{BY_KEY[total_key].name}与明细项目之和不一致"))
    return out


@rule("is_subtotals", "营业利润/利润总额/净利润的勾稽")
def check_is_subtotals(view: ReportView) -> list[Finding]:
    out = []
    st = view.statements.get("IS")
    if not st:
        return out

    def val(key, role):
        return view.value("IS", key, role)[0]

    for col in st.columns:
        role = col["role"]
        op = val("operating_profit", role)
        if op is not None:
            parts = []
            for key in ("revenue", "cost_of_revenue", "taxes_and_surcharges",
                        "selling_expenses", "admin_expenses", "rnd_expenses",
                        "finance_expenses", "other_income",
                        "investment_income", "fv_change_income",
                        "credit_impairment_loss", "asset_impairment_loss",
                        "asset_disposal_income"):
                v = val(key, role)
                if v is None:
                    continue
                sign = -1 if key in {"cost_of_revenue", "taxes_and_surcharges",
                                     "selling_expenses", "admin_expenses",
                                     "rnd_expenses", "finance_expenses"} else 1
                parts.append(v * sign)
            if parts:
                out.append(_finding(
                    "is_subtotals", "营业利润", "IS", role, col.get("year"),
                    _sum(parts), op,
                    _tol(view.unit_scale, view.places, len(parts)),
                    _pax(view, "IS", ["operating_profit"], role),
                    [st.pages[0]], "营业利润与按项目加减计算的结果不一致"))
        tp = val("total_profit", role)
        o2 = val("operating_profit", role)
        ni = val("nonoperating_income", role) or D(0)
        ne = val("nonoperating_expense", role) or D(0)
        if tp is not None and o2 is not None:
            out.append(_finding(
                "is_subtotals", "利润总额", "IS", role, col.get("year"),
                o2 + ni - ne, tp,
                _tol(view.unit_scale, view.places, 3),
                _pax(view, "IS", ["total_profit", "operating_profit",
                                  "nonoperating_income", "nonoperating_expense"],
                     role),
                [st.pages[0]], "利润总额与营业利润加减营业外收支不一致"))
        np_ = val("net_profit", role)
        tax = val("income_tax_expense", role)
        if np_ is not None and tp is not None:
            out.append(_finding(
                "is_subtotals", "净利润", "IS", role, col.get("year"),
                tp - (tax or D(0)), np_,
                _tol(view.unit_scale, view.places, 2),
                _pax(view, "IS", ["net_profit", "total_profit",
                                  "income_tax_expense"], role),
                [st.pages[0]], "净利润与利润总额减所得税费用不一致"))
        npp = val("net_profit_parent", role)
        mi = val("minority_profit", role)
        if npp is not None and np_ is not None and mi is not None:
            out.append(_finding(
                "is_subtotals", "归属于母公司股东的净利润", "IS", role,
                col.get("year"), np_ - mi, npp,
                _tol(view.unit_scale, view.places, 2),
                _pax(view, "IS", ["net_profit_parent", "net_profit",
                                  "minority_profit"], role),
                [st.pages[0]], "归母净利润与净利润减少数股东损益不一致"))
    return out


@rule("cf_subtotals", "现金流入/流出小计与净额")
def check_cf_subtotals(view: ReportView) -> list[Finding]:
    out = []
    st = view.statements.get("CF")
    if not st:
        return out
    groups = [
        ("total_operating_cash_in", "operating_in"),
        ("total_operating_cash_out", "operating_out"),
        ("total_investing_cash_in", "investing_in"),
        ("total_investing_cash_out", "investing_out"),
        ("total_financing_cash_in", "financing_in"),
        ("total_financing_cash_out", "financing_out"),
    ]
    for total_key, section in groups:
        members = [k for k in SECTION_ACCOUNTS.get(section, [])]
        for col in st.columns:
            role = col["role"]
            total, tv = view.value("CF", total_key, role)
            if total is None:
                continue
            parts = [v for k in members
                     if (v := view.value("CF", k, role)[0]) is not None]
            if not parts:
                continue
            out.append(_finding(
                "cf_subtotals", BY_KEY[total_key].name, "CF", role,
                col.get("year"), _sum(parts), total,
                _tol(view.unit_scale, view.places, len(parts)),
                _pax(view, "CF", [total_key] + members, role),
                [st.pages[0]],
                f"{BY_KEY[total_key].name}与明细之和不一致"))
    nets = [
        ("net_operating_cash", "total_operating_cash_in",
         "total_operating_cash_out"),
        ("net_investing_cash", "total_investing_cash_in",
         "total_investing_cash_out"),
        ("net_financing_cash", "total_financing_cash_in",
         "total_financing_cash_out"),
    ]
    for net, cin, cout in nets:
        for col in st.columns:
            role = col["role"]
            net_v, nv = view.value("CF", net, role)
            iv = view.value("CF", cin, role)[0]
            ov = view.value("CF", cout, role)[0]
            if net_v is None or iv is None or ov is None:
                continue
            out.append(_finding(
                "cf_subtotals", BY_KEY[net].name, "CF", role, col.get("year"),
                iv - ov, net_v, _tol(view.unit_scale, view.places, 2),
                _pax(view, "CF", [net, cin, cout], role), [st.pages[0]],
                f"{BY_KEY[net].name}不等于现金流入小计减现金流出小计"))
    for col in st.columns:
        role = col["role"]
        nc, nv = view.value("CF", "net_cash_increase", role)
        if nc is None:
            continue
        parts = [view.value("CF", k, role)[0] for k in
                 ("net_operating_cash", "net_investing_cash",
                  "net_financing_cash", "fx_effect")]
        if any(p is None for p in parts):
            continue
        out.append(_finding(
            "cf_subtotals", "现金及现金等价物净增加额", "CF", role,
            col.get("year"), _sum(parts), nc,
            _tol(view.unit_scale, view.places, 4),
            _pax(view, "CF", ["net_cash_increase", "net_operating_cash",
                              "net_investing_cash", "net_financing_cash",
                              "fx_effect"], role),
            [st.pages[0]], "三类活动净额与汇率影响之和与现金净增加额不一致"))
        begin, _ = view.value("CF", "cash_beginning", role)
        end, ev = view.value("CF", "cash_ending", role)
        if begin is not None and end is not None:
            out.append(_finding(
                "cf_subtotals", "期末现金余额", "CF", role, col.get("year"),
                begin + nc, end, _tol(view.unit_scale, view.places, 2),
                _pax(view, "CF", ["cash_ending", "cash_beginning",
                                  "net_cash_increase"], role),
                [st.pages[0]], "期末余额不等于期初余额加净增加额"))
    return out


@rule("cross_statement", "跨表勾稽")
def check_cross(view: ReportView) -> list[Finding]:
    out = []
    bs = view.statements.get("BS")
    cf = view.statements.get("CF")
    if bs and cf:
        roles = {c["role"] for c in bs.columns} & {c["role"] for c in cf.columns}
        for role in roles:
            c_end, cv = view.value("CF", "cash_ending", role)
            bs_cash, bv = view.value("BS", "cash", role)
            if c_end is None or bs_cash is None:
                continue
            out.append(_finding(
                "cross_statement", "期末现金余额 vs 资产负债表货币资金", "CF",
                role, None, bs_cash, c_end,
                _tol(view.unit_scale, view.places, 2),
                [{"key": "cash_ending", "value": str(c_end),
                   "page": (cv or {}).get("page"),
                   "box": (cv or {}).get("box"),
                   "source": "CF"},
                 {"key": "cash", "value": str(bs_cash),
                  "page": (bv or {}).get("page"),
                  "box": (bv or {}).get("box"),
                  "source": "BS"}],
                (cf.pages[:1] + bs.pages[:1]),
                "现金流量表期末现金余额与资产负债表货币资金不一致"
                "（存在受限资金或非现金等价物时属正常差异）"))
    # prior column of the current report should match nothing else, but
    # 期初 cash (current role) should equal prior 期末 cash
    if cf:
        roles = [c["role"] for c in cf.columns]
        if "current" in roles and "prior" in roles:
            begin, _ = view.value("CF", "cash_beginning", "current")
            prior_end, _ = view.value("CF", "cash_ending", "prior")
            if begin is not None and prior_end is not None:
                out.append(_finding(
                    "cross_statement", "本期期初现金 = 上期期末现金", "CF",
                    "current", None, prior_end, begin,
                    _tol(view.unit_scale, view.places, 2),
                    _pax(view, "CF", ["cash_beginning"], "current"),
                    cf.pages[:1], "本期期初余额与上期期末余额不一致"))
    # revenue vs cash receipts is informational only
    if isinstance(out, list):
        pass
    return out


def _pax(view: ReportView, code: str, keys: list[str], role: str) -> list[dict]:
    out = []
    st = view.statements.get(code)
    if not st:
        return out
    col = next((c for c in st.columns if c["role"] == role), None)
    if not col:
        return out
    for i, key in enumerate(keys):
        v = view.value(code, key, role)[1]
        out.append({"key": key, "value": (v or {}).get("value"),
                    "page": (v or {}).get("page"),
                    "box": (v or {}).get("box")})
    return out


def run_checks(statements: list, unit_scale: int | None = None,
               places: int = 2) -> list[dict]:
    """Run all rules over the consolidated statements.

    The tolerance is derived from the report's printed unit: a 百万元 report
    can only reconcile to a million yuan, so differences below that are
    rounding, not errors.  ``unit_scale`` defaults to the unit the extractor
    recorded on the statements themselves.

    Returns serialisable findings sorted by severity.
    """
    by_code = {}
    for st in statements:
        if st.scope != "consolidated":
            continue
        by_code[st.code] = st
    if unit_scale is None:
        scales = [getattr(st, "unit_scale", 1) or 1 for st in by_code.values()]
        unit_scale = max(scales) if scales else 1
    if places is None:
        places = 2
    view = ReportView(by_code, unit_scale, places)
    findings: list[Finding] = []
    for fn in RULES:
        findings.extend(fn(view))
    order = {"error": 0, "warning": 1, "ok": 2}
    findings.sort(key=lambda f: (order[f.status], f.statement, f.rule))
    return [
        {
            "rule": f.rule,
            "label": f.label,
            "statement": f.statement,
            "role": f.role,
            "year": f.year,
            "expected": str(f.expected),
            "actual": str(f.actual),
            "diff": str(f.diff),
            "tolerance": str(f.tolerance),
            "status": f.status,
            "pages": f.pages,
            "participants": f.participants,
            "message": f.message,
        }
        for f in findings
    ]
