"""Standard account chart, label normalisation and account matching.

Everything downstream (corpus generator, extractor, validator, UI) speaks the
account keys defined here.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Account definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    key: str
    name: str
    statement: str          # BS / IS / CF
    section: str            # grouping used by the validation rules
    order: int
    aliases: tuple[str, ...] = field(default_factory=tuple)
    sign: int = 1           # sign used when summing equity items


def _a(key, name, stmt, section, order, aliases=(), sign=1):
    return Account(key, name, stmt, section, order, tuple(aliases), sign)


ACCOUNTS: list[Account] = [
    # ----------------------------- balance sheet ---------------------------
    _a("cash", "货币资金", "BS", "current_assets", 10,
       ("现金及现金等价物", "货币资金及现金等价物")),
    _a("trading_fin_assets", "交易性金融资产", "BS", "current_assets", 20,
       ("以公允价值计量且其变动计入当期损益的金融资产",)),
    _a("derivative_fin_assets", "衍生金融资产", "BS", "current_assets", 30),
    _a("notes_receivable", "应收票据", "BS", "current_assets", 40),
    _a("accounts_receivable", "应收账款", "BS", "current_assets", 50,
       ("应收帐款", "应收账款净额")),
    _a("receivables_financing", "应收款项融资", "BS", "current_assets", 60),
    _a("prepayments", "预付款项", "BS", "current_assets", 70,
       ("预付账款", "预付款")),
    _a("other_receivables", "其他应收款", "BS", "current_assets", 80),
    _a("inventory", "存货", "BS", "current_assets", 90, ("存货净额",)),
    _a("contract_assets", "合同资产", "BS", "current_assets", 100),
    _a("assets_held_for_sale", "持有待售资产", "BS", "current_assets", 110),
    _a("noncurrent_due_within_one_year", "一年内到期的非流动资产", "BS",
       "current_assets", 120),
    _a("other_current_assets", "其他流动资产", "BS", "current_assets", 130),
    _a("total_current_assets", "流动资产合计", "BS", "current_assets_total", 140,
       ("流动资产小计",)),

    _a("debt_investment", "债权投资", "BS", "noncurrent_assets", 150),
    _a("other_debt_investment", "其他债权投资", "BS", "noncurrent_assets", 160),
    _a("long_term_receivables", "长期应收款", "BS", "noncurrent_assets", 170),
    _a("long_term_equity_investment", "长期股权投资", "BS", "noncurrent_assets", 180),
    _a("other_equity_instrument_investment", "其他权益工具投资", "BS",
       "noncurrent_assets", 190),
    _a("other_noncurrent_fin_assets", "其他非流动金融资产", "BS",
       "noncurrent_assets", 200),
    _a("investment_property", "投资性房地产", "BS", "noncurrent_assets", 210),
    _a("fixed_assets", "固定资产", "BS", "noncurrent_assets", 220,
       ("固定资产净额", "固定资产原值")),
    _a("construction_in_progress", "在建工程", "BS", "noncurrent_assets", 230),
    _a("productive_biological_assets", "生产性生物资产", "BS",
       "noncurrent_assets", 240),
    _a("oil_gas_assets", "油气资产", "BS", "noncurrent_assets", 250),
    _a("right_of_use_assets", "使用权资产", "BS", "noncurrent_assets", 260),
    _a("intangible_assets", "无形资产", "BS", "noncurrent_assets", 270,
       ("无形资产净额",)),
    _a("development_expenditure", "开发支出", "BS", "noncurrent_assets", 280),
    _a("goodwill", "商誉", "BS", "noncurrent_assets", 290),
    _a("long_term_prepaid_expenses", "长期待摊费用", "BS", "noncurrent_assets", 300),
    _a("deferred_tax_assets", "递延所得税资产", "BS", "noncurrent_assets", 310),
    _a("other_noncurrent_assets", "其他非流动资产", "BS", "noncurrent_assets", 320),
    _a("total_noncurrent_assets", "非流动资产合计", "BS", "noncurrent_assets_total",
       330, ("非流动资产小计",)),
    _a("total_assets", "资产总计", "BS", "assets_total", 340,
       ("资产合计", "资产总额")),

    _a("short_term_borrowings", "短期借款", "BS", "current_liabilities", 400),
    _a("trading_fin_liabilities", "交易性金融负债", "BS", "current_liabilities", 410),
    _a("derivative_fin_liabilities", "衍生金融负债", "BS", "current_liabilities", 420),
    _a("notes_payable", "应付票据", "BS", "current_liabilities", 430),
    _a("accounts_payable", "应付账款", "BS", "current_liabilities", 440,
       ("应付帐款",)),
    _a("advance_receipts", "预收款项", "BS", "current_liabilities", 450,
       ("预收账款",)),
    _a("contract_liabilities", "合同负债", "BS", "current_liabilities", 460),
    _a("employee_benefits_payable", "应付职工薪酬", "BS", "current_liabilities", 470,
       ("应付工资", "应付职工工资")),
    _a("taxes_payable", "应交税费", "BS", "current_liabilities", 480,
       ("应交税金",)),
    _a("other_payables", "其他应付款", "BS", "current_liabilities", 490),
    _a("liabilities_held_for_sale", "持有待售负债", "BS", "current_liabilities", 500),
    _a("noncurrent_liab_due_within_one_year", "一年内到期的非流动负债", "BS",
       "current_liabilities", 510),
    _a("other_current_liabilities", "其他流动负债", "BS", "current_liabilities", 520),
    _a("total_current_liabilities", "流动负债合计", "BS",
       "current_liabilities_total", 530, ("流动负债小计",)),

    _a("long_term_borrowings", "长期借款", "BS", "noncurrent_liabilities", 540),
    _a("bonds_payable", "应付债券", "BS", "noncurrent_liabilities", 550),
    _a("lease_liabilities", "租赁负债", "BS", "noncurrent_liabilities", 560),
    _a("long_term_payables", "长期应付款", "BS", "noncurrent_liabilities", 570),
    _a("provisions", "预计负债", "BS", "noncurrent_liabilities", 580),
    _a("deferred_income", "递延收益", "BS", "noncurrent_liabilities", 590),
    _a("deferred_tax_liabilities", "递延所得税负债", "BS",
       "noncurrent_liabilities", 600),
    _a("other_noncurrent_liabilities", "其他非流动负债", "BS",
       "noncurrent_liabilities", 610),
    _a("total_noncurrent_liabilities", "非流动负债合计", "BS",
       "noncurrent_liabilities_total", 620, ("非流动负债小计",)),
    _a("total_liabilities", "负债合计", "BS", "liabilities_total", 630,
       ("负债总计",)),

    _a("paid_in_capital", "实收资本（或股本）", "BS", "equity_parent", 700,
       ("实收资本", "股本", "实收资本或股本")),
    _a("other_equity_instruments", "其他权益工具", "BS", "equity_parent", 710),
    _a("capital_reserve", "资本公积", "BS", "equity_parent", 720),
    _a("treasury_stock", "库存股", "BS", "equity_parent", 730,
       ("减：库存股",), -1),
    _a("other_comprehensive_income", "其他综合收益", "BS", "equity_parent", 740),
    _a("special_reserve", "专项储备", "BS", "equity_parent", 750),
    _a("surplus_reserve", "盈余公积", "BS", "equity_parent", 760),
    _a("retained_earnings", "未分配利润", "BS", "equity_parent", 770),
    _a("total_equity_parent", "归属于母公司所有者权益合计", "BS",
       "equity_parent_total", 780,
       ("归属于母公司股东权益合计", "归属于母公司所有者权益（或股东权益）合计",
        "归属于母公司股东的权益合计")),
    _a("minority_interests", "少数股东权益", "BS", "equity_minority", 790),
    _a("total_equity", "所有者权益合计", "BS", "equity_total", 800,
       ("股东权益合计", "所有者权益（或股东权益）合计")),
    _a("total_liabilities_and_equity", "负债和所有者权益总计", "BS",
       "liabilities_equity_total", 810,
       ("负债和股东权益总计", "负债和所有者权益（或股东权益）总计",
        "负债及所有者权益总计")),

    # ------------------------------ income statement -----------------------
    _a("revenue", "营业收入", "IS", "revenue", 1000,
       ("营业总收入", "主营业务收入", "营业收入合计")),
    _a("cost_of_revenue", "营业成本", "IS", "cost", 1010,
       ("主营业务成本", "营业成本合计")),
    _a("taxes_and_surcharges", "税金及附加", "IS", "expense", 1020,
       ("营业税金及附加",)),
    _a("selling_expenses", "销售费用", "IS", "expense", 1030,
       ("营业费用", "销售费")),
    _a("admin_expenses", "管理费用", "IS", "expense", 1040),
    _a("rnd_expenses", "研发费用", "IS", "expense", 1050,
       ("研发开支", "研究开发费用", "研发支出", "研发费")),
    _a("finance_expenses", "财务费用", "IS", "expense", 1060,
       ("财务费", "财务开支")),
    _a("interest_expense", "利息费用", "IS", "finance_detail", 1070,
       ("利息支出", "利息费")),
    _a("interest_income", "利息收入", "IS", "finance_detail", 1080,
       ("利息收入净额",)),
    _a("other_income", "其他收益", "IS", "other_income", 1090),
    _a("investment_income", "投资收益", "IS", "other_income", 1100,
       ("投资收益（损失以“-”号填列）",)),
    _a("associate_investment_income", "对联营企业和合营企业的投资收益", "IS",
       "other_income", 1110,
       ("其中：对联营企业和合营企业的投资收益",)),
    _a("fv_change_income", "公允价值变动收益", "IS", "other_income", 1120,
       ("公允价值变动损益",)),
    _a("credit_impairment_loss", "信用减值损失", "IS", "other_income", 1130,
       ("信用减值损失（损失以“-”号填列）",)),
    _a("asset_impairment_loss", "资产减值损失", "IS", "other_income", 1140,
       ("资产减值损失（损失以“-”号填列）",)),
    _a("asset_disposal_income", "资产处置收益", "IS", "other_income", 1150,
       ("资产处置损益",)),
    _a("operating_profit", "营业利润", "IS", "operating_profit", 1160),
    _a("nonoperating_income", "营业外收入", "IS", "nonoperating", 1170),
    _a("nonoperating_expense", "营业外支出", "IS", "nonoperating", 1180),
    _a("total_profit", "利润总额", "IS", "total_profit", 1190),
    _a("income_tax_expense", "所得税费用", "IS", "tax", 1200,
       ("所得税", "所得税费")),
    _a("net_profit", "净利润", "IS", "net_profit", 1210,
       ("净利润（净亏损以“-”号填列）",)),
    _a("continuing_net_profit", "持续经营净利润", "IS", "net_profit_detail", 1220),
    _a("discontinued_net_profit", "终止经营净利润", "IS", "net_profit_detail", 1230),
    _a("net_profit_parent", "归属于母公司股东的净利润", "IS",
       "net_profit_parent", 1240,
       ("归属于母公司所有者的净利润", "归属于上市公司股东的净利润",
        "归属于母公司股东的净利润（净亏损以“-”号填列）")),
    _a("minority_profit", "少数股东损益", "IS", "minority_profit", 1250),
    _a("basic_eps", "基本每股收益", "IS", "eps", 1260,
       ("每股收益", "基本每股收益(元/股)")),
    _a("diluted_eps", "稀释每股收益", "IS", "eps", 1270),

    # ---------------------------- cash flow statement ----------------------
    _a("cash_from_sales", "销售商品、提供劳务收到的现金", "CF", "operating_in", 2000,
       ("销售商品提供劳务收到的现金", "销售商品、提供劳务收到的现金净额",
        "销售商品提供收到的现金", "销售商品收到的现金")),
    _a("tax_refund", "收到的税费返还", "CF", "operating_in", 2010),
    _a("other_operating_cash_in", "收到其他与经营活动有关的现金", "CF",
       "operating_in", 2020),
    _a("total_operating_cash_in", "经营活动现金流入小计", "CF",
       "operating_in_total", 2030),
    _a("cash_for_goods", "购买商品、接受劳务支付的现金", "CF", "operating_out", 2040,
       ("购买商品接受劳务支付的现金",)),
    _a("cash_for_employees", "支付给职工以及为职工支付的现金", "CF",
       "operating_out", 2050),
    _a("taxes_paid", "支付的各项税费", "CF", "operating_out", 2060),
    _a("other_operating_cash_out", "支付其他与经营活动有关的现金", "CF",
       "operating_out", 2070),
    _a("total_operating_cash_out", "经营活动现金流出小计", "CF",
       "operating_out_total", 2080),
    _a("net_operating_cash", "经营活动产生的现金流量净额", "CF",
       "operating_net", 2090),
    _a("cash_from_investment_recovery", "收回投资收到的现金", "CF",
       "investing_in", 2100),
    _a("cash_from_investment_income", "取得投资收益收到的现金", "CF",
       "investing_in", 2110),
    _a("cash_from_disposal_assets",
       "处置固定资产、无形资产和其他长期资产收回的现金净额", "CF",
       "investing_in", 2120,
       ("处置固定资产无形资产和其他长期资产收回的现金净额",)),
    _a("cash_for_asset_purchase",
       "购建固定资产、无形资产和其他长期资产支付的现金", "CF",
       "investing_out", 2130,
       ("购建固定资产无形资产和其他长期资产支付的现金",)),
    _a("cash_for_investments", "投资支付的现金", "CF", "investing_out", 2140),
    _a("total_investing_cash_in", "投资活动现金流入小计", "CF",
       "investing_in_total", 2150),
    _a("total_investing_cash_out", "投资活动现金流出小计", "CF",
       "investing_out_total", 2160),
    _a("net_investing_cash", "投资活动产生的现金流量净额", "CF",
       "investing_net", 2170),
    _a("cash_from_capital_contribution", "吸收投资收到的现金", "CF",
       "financing_in", 2180),
    _a("cash_from_borrowings", "取得借款收到的现金", "CF", "financing_in", 2190),
    _a("other_financing_cash_in", "收到其他与筹资活动有关的现金", "CF",
       "financing_in", 2200),
    _a("cash_for_debt_repayment", "偿还债务支付的现金", "CF",
       "financing_out", 2210),
    _a("cash_for_dividends_interest", "分配股利、利润或偿付利息支付的现金", "CF",
       "financing_out", 2220,
       ("分配股利利润或偿付利息支付的现金",)),
    _a("total_financing_cash_in", "筹资活动现金流入小计", "CF",
       "financing_in_total", 2230),
    _a("total_financing_cash_out", "筹资活动现金流出小计", "CF",
       "financing_out_total", 2240),
    _a("net_financing_cash", "筹资活动产生的现金流量净额", "CF",
       "financing_net", 2250),
    _a("fx_effect", "汇率变动对现金及现金等价物的影响", "CF", "fx", 2260),
    _a("net_cash_increase", "现金及现金等价物净增加额", "CF", "cash_change", 2270),
    _a("cash_beginning", "期初现金及现金等价物余额", "CF", "cash_balance", 2280,
       ("年初现金及现金等价物余额",)),
    _a("cash_ending", "期末现金及现金等价物余额", "CF", "cash_balance", 2290,
       ("年末现金及现金等价物余额",)),
]

BY_KEY = {a.key: a for a in ACCOUNTS}
BY_STATEMENT: dict[str, list[Account]] = {"BS": [], "IS": [], "CF": []}
for _acc in ACCOUNTS:
    BY_STATEMENT[_acc.statement].append(_acc)
for _lst in BY_STATEMENT.values():
    _lst.sort(key=lambda a: a.order)

STATEMENT_NAMES = {"BS": "资产负债表", "IS": "利润表", "CF": "现金流量表"}

# sections ---------------------------------------------------------------
SECTION_ACCOUNTS: dict[str, list[str]] = {}
for _acc in ACCOUNTS:
    SECTION_ACCOUNTS.setdefault(_acc.section, []).append(_acc.key)


# ---------------------------------------------------------------------------
# Label normalisation
# ---------------------------------------------------------------------------

_FULLWIDTH = str.maketrans({"（": "(", "）": ")", "：": ":", "，": ",", "．": ".",
                            "－": "-", "—": "-", "–": "-", "　": " "})

# parenthetical annotations that are not part of the account name
_ANNOTATION_RE = re.compile(
    r"\((?:[^()]*?(?:附注|注释|注[一二三四五六七八九十\d]|[一二三四五六七八九十]+[、.]\d*"
    r"|填列|损失以|亏损以|或股东权益|或股本|净额|净亏损)[^()]*?)\)"
)
_NUMBERING_RE = re.compile(
    r"^(?:[一二三四五六七八九十]+[、.．]"
    r"|[（(][一二三四五六七八九十\d]{1,3}[)）]"
    r"|\d{1,2}[、.．](?!\d))"
)
_PREFIX_RE = re.compile(r"^(?:其中|加|减)\s*:\s*")
_TRAIL_RE = re.compile(r"[\s:：,，;；*#①②③④⑤⑥⑦⑧⑨⑩]+$")


def normalize_label(text: str) -> str:
    """Normalise a raw statement line label for matching."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = s.translate(_FULLWIDTH)
    s = s.replace("帐", "账")
    s = re.sub(r"\s+", "", s)
    s = _ANNOTATION_RE.sub("", s)
    # "基本每股收益（元/股）" / "每股收益（元）" carry a unit, not a name
    s = re.sub(r"[(（]\s*元\s*/?\s*股?\s*[)）]$", "", s)
    s = re.sub(r"[(（]\s*元\s*[)）]$", "", s)
    s = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩]", "", s)
    s = re.sub(r"\*+", "", s)
    # drop footnote suffixes like 注1 / 五、1 / 附注五1
    s = re.sub(r"(?:附注|注释|注)?[一二三四五六七八九十]{1,3}[、.]\d{1,3}$", "", s)
    s = re.sub(r"(?:附注|注释|注)\d{1,3}$", "", s)
    # strip numbering and 加:/减:/其中: prefixes (possibly repeated)
    prev = None
    while prev != s:
        prev = s
        s = _NUMBERING_RE.sub("", s)
        s = _PREFIX_RE.sub("", s)
    s = _TRAIL_RE.sub("", s)
    # a leading note reference fused onto the label ("29利息收入注")
    s = re.sub(r"^\d{1,2}(?=[\u4e00-\u9fff])", "", s)
    s = s.lstrip(":：")
    # OCR noise: stray latin/glyph runs appended by a misread note marker
    s = re.sub(r"[A-Za-z]{1,4}[,.;:!|]*$", "", s)
    s = re.sub(r"[^\w\u4e00-\u9fff()（）/、.%（）-]+", "", s)
    # an interposed OCR artifact inside an otherwise clean label
    s = re.sub(r"(?<=[\u4e00-\u9fff])[一二三四五六七八九十A-Za-z](?=\S*净额$)", "", s)
    return s.strip()


# alias index per statement; the first registered name wins
_ALIAS_INDEX: dict[str, dict[str, str]] = {"BS": {}, "IS": {}, "CF": {}}
for _acc in ACCOUNTS:
    for _alias in {_acc.name, *_acc.aliases}:
        _norm = normalize_label(_alias)
        _ALIAS_INDEX[_acc.statement].setdefault(_norm, _acc.key)


def _statement_of(key: str | None) -> str | None:
    return BY_KEY[key].statement if key else None


def match_account(label: str, statement: str | None = None,
                  extra_aliases: dict[str, str] | None = None
                  ) -> tuple[str | None, str, float]:
    """Map a raw label to a standard account key.

    Returns ``(key, method, score)`` where method is one of
    ``exact`` / ``alias`` / ``fuzzy`` / ``None``.
    """
    norm = normalize_label(label)
    if not norm:
        return None, "none", 0.0

    # user supplied aliases always win
    if extra_aliases and norm in extra_aliases:
        key = extra_aliases[norm]
        if key in BY_KEY:
            return key, "user", 1.0

    statements = [statement] if statement else ["BS", "IS", "CF"]
    best: tuple[str | None, str, float] = (None, "none", 0.0)

    # 1. exact standard name
    for st in statements:
        for acc in BY_STATEMENT.get(st, []):
            if norm == normalize_label(acc.name):
                return acc.key, "exact", 1.0
    # 2. exact alias
    for st in statements:
        key = _ALIAS_INDEX.get(st, {}).get(norm)
        if key:
            return key, "alias", 1.0

    # 3. containment: "固定资产净额" -> 固定资产.  Only when the extra text is
    #    short, otherwise "固定资产清理" would be captured by 固定资产.
    if len(norm) >= 4:
        cands = []
        for st in statements:
            for alias, key in _ALIAS_INDEX.get(st, {}).items():
                if len(alias) < 4:
                    continue
                if alias == norm:
                    continue
                if abs(len(norm) - len(alias)) <= 3 and (
                        norm.startswith(alias) or norm.endswith(alias)):
                    cands.append((len(alias), key))
        if cands:
            cands.sort(reverse=True)
            if len(cands) == 1 or cands[0][0] > cands[1][0]:
                return cands[0][1], "alias", 0.95

    # 4. fuzzy over all alias strings
    pool: list[tuple[str, str]] = []
    for st in statements:
        for alias, key in _ALIAS_INDEX.get(st, {}).items():
            if len(alias) >= 4:
                pool.append((alias, key))
    alias_strings = [p[0] for p in pool]
    close = difflib.get_close_matches(norm, alias_strings, n=4, cutoff=0.8)
    if close:
        scored = sorted(
            ((difflib.SequenceMatcher(None, norm, a).ratio(), a) for a in close),
            reverse=True,
        )
        score, alias = scored[0]
        margin = score - (scored[1][0] if len(scored) > 1 else 0.0)
        # OCR can drop or insert a character ("他应收款" for 其他应收款), so a
        # short label may match too, but only by a clear margin
        min_len = 5 if _CJK_COUNT(norm) >= 6 else 4
        min_score = 0.86 if _CJK_COUNT(norm) >= 6 else 0.88
        if len(norm) >= min_len and score >= min_score and margin >= 0.04:
            key = dict(pool)[alias]
            best = (key, "fuzzy", round(score, 3))
    return best


def _CJK_COUNT(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def account_name(key: str) -> str:
    acc = BY_KEY.get(key)
    return acc.name if acc else key


def chart_payload() -> list[dict]:
    """Serialisable chart for the 科目对照 UI."""
    return [
        {
            "key": a.key,
            "name": a.name,
            "statement": a.statement,
            "statement_name": STATEMENT_NAMES[a.statement],
            "section": a.section,
            "order": a.order,
            "aliases": list(a.aliases),
        }
        for a in ACCOUNTS
    ]
