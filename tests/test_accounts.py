"""Unit tests for label normalisation, amount parsing and unit handling."""
from decimal import Decimal

import pytest

from finreport.accounts import (
    ACCOUNTS, BY_KEY, match_account, normalize_label,
)
from finreport.values import detect_unit, parse_amount


@pytest.mark.parametrize("raw,expected", [
    ("货币资金", "cash"),
    ("货币资金净额", "cash"),
    ("应收帐款", "accounts_receivable"),
    ("应收账款（净额）", "accounts_receivable"),
    ("其中：对联营企业和合营企业的投资收益", "associate_investment_income"),
    ("减：库存股", "treasury_stock"),
    ("基本每股收益（元/股）", "basic_eps"),
    ("归属于母公司股东的净利润（净亏损以“-”号填列）", "net_profit_parent"),
    ("负债和所有者权益（或股东权益）总计", "total_liabilities_and_equity"),
    ("销售商品、提供劳务收到的现金", "cash_from_sales"),
    ("实收资本（或股本）", "paid_in_capital"),
])
def test_exact_and_alias_matching(raw, expected):
    key, method, score = match_account(raw)
    assert key == expected
    assert method in {"exact", "alias", "fuzzy"}
    assert score >= 0.85


@pytest.mark.parametrize("raw,expected", [
    ("他应收款", "other_receivables"),          # OCR dropped 其
    ("存货二净额", "inventory"),                 # OCR interposed a glyph
    ("筹资活动流出小计", "total_financing_cash_out"),
    ("收回投资收到的现金fi,Ml", "cash_from_investment_recovery"),
])
def test_ocr_noise_matching(raw, expected):
    key, method, _ = match_account(raw, None)
    assert key == expected


def test_statement_scoping_avoids_cross_statement_matches():
    # 营业收入 exists on the income statement only
    assert match_account("营业收入", "BS")[0] is None
    assert match_account("营业收入", "IS")[0] == "revenue"


def test_caption_like_labels_do_not_match():
    for label in ("流动资产", "非流动资产", "一、经营活动产生的现金流量", "二、投资活动产生的现金流量"):
        key, _, _ = match_account(label, None)
        # a caption must never win by fuzziness against a sole line item
        assert key is None or BY_KEY[key].name != normalize_label(label)


def test_normalise_strips_numbering_and_annotations():
    assert normalize_label("（一）货币资金") == "货币资金"
    assert normalize_label("1. 货币资金") == "货币资金"
    assert normalize_label("货币资金 五、1") == "货币资金"
    assert normalize_label("货币资金注12") == "货币资金"
    assert normalize_label("货币资金（附注五）") == "货币资金"


@pytest.mark.parametrize("raw,value,blank", [
    ("1,234,567.89", Decimal("1234567.89"), False),
    ("(1,234.00)", Decimal("-1234"), False),
    ("-1,234", Decimal("-1234"), False),
    ("1,234-", Decimal("-1234"), False),
    ("-", Decimal(0), True),
    ("—", Decimal(0), True),
    ("", None, True),
    ("不适用", Decimal(0), True),
    ("0.00", Decimal(0), False),
])
def test_parse_amount(raw, value, blank):
    v, b = parse_amount(raw)
    assert b is blank
    if value is None:
        assert v is None
    else:
        assert v == value


def test_parse_amount_rejects_text():
    v, blank = parse_amount("货币资金")
    assert v is None and blank is False


@pytest.mark.parametrize("texts,unit,scale", [
    (["单位：人民币元"], "元", 1),
    (["编制单位：X公司    单位：千元"], "千元", 1000),
    (["单位：人民币万元"], "万元", 10000),
    (["金额单位: 百万元"], "百万元", 1000000),
    (["三年内", "单位：亿元"], "亿元", 100000000),
    (["编制单位:某公司”单位:人民币万", "元"], "万元", 10000),
])
def test_detect_unit(texts, unit, scale):
    found = detect_unit(texts)
    assert found == (unit, scale)


def test_detect_unit_defaults_to_none():
    assert detect_unit(["合并资产负债表", "2024 年度"]) is None


def test_chart_is_complete_and_unique():
    keys = [a.key for a in ACCOUNTS]
    assert len(keys) == len(set(keys))
    for a in ACCOUNTS:
        assert a.statement in {"BS", "IS", "CF"}
        assert a.name
