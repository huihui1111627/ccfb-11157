"""Tests for the synthetic data engine and the cross-check rules."""
from decimal import Decimal

import pytest

from finreport.accounts import SECTION_ACCOUNTS
from finreport.data import build_company
from finreport.evaluate import aggregate, by_account, score_document
from finreport.validate import _tol, run_checks


@pytest.fixture(scope="module")
def company():
    return build_company("600519", "贵州茅台酒股份有限公司", "贵州茅台", seed=7)


def test_balance_sheet_balances(company):
    for scope in ("consolidated", "parent"):
        bs = company.statement("BS", scope)
        for year in company.years:
            assert bs.get("total_assets", year) == \
                bs.get("total_liabilities_and_equity", year)


def test_subtotals_reconcile(company):
    bs = company.bs
    for year in company.years:
        ca = sum(bs.get(k, year) or 0
                 for k in SECTION_ACCOUNTS["current_assets"])
        assert ca == bs.get("total_current_assets", year)
        nca = sum(bs.get(k, year) or 0
                  for k in SECTION_ACCOUNTS["noncurrent_assets"])
        assert nca == bs.get("total_noncurrent_assets", year)


def test_income_statement_chain(company):
    is_ = company.is_
    for year in company.years:
        net = is_.get("net_profit", year)
        assert net == is_.get("total_profit", year) \
            - is_.get("income_tax_expense", year)
        assert is_.get("net_profit_parent", year) == \
            net - is_.get("minority_profit", year)


def test_cash_flow_links_to_balance_sheet(company):
    cf, bs = company.cf, company.bs
    for year in company.years:
        assert cf.get("cash_ending", year) == bs.get("cash", year)
        assert cf.get("net_cash_increase", year) == \
            cf.get("cash_ending", year) - cf.get("cash_beginning", year)
        parts = sum(cf.get(k, year) or 0 for k in
                    ("net_operating_cash", "net_investing_cash",
                     "net_financing_cash", "fx_effect"))
        assert parts == cf.get("net_cash_increase", year)
    # the current year opens where the prior year closed
    assert cf.get("cash_beginning", company.years[1]) == \
        cf.get("cash_ending", company.years[0])


def test_broken_scenario_breaks_balance():
    c = build_company("000001", "测试股份有限公司", "测试", seed=3,
                      broken={"equity_total"})
    bs = c.bs
    year = c.years[1]
    # an equity subtotal that no longer equals its components
    parent = sum((bs.get(k, year) or 0) for k in
                 SECTION_ACCOUNTS["equity_parent"])
    assert bs.get("total_equity_parent", year) != parent


def test_tolerance_scales_with_unit():
    assert _tol(1) < _tol(1000) < _tol(10000) < _tol(1000000)
    # a 万元 report tolerates rounding within a printed unit
    assert _tol(10000) >= Decimal(10000)


class _Stmt:
    """Minimal stand-in for an extracted statement."""

    def __init__(self, code, scope, unit_scale=1):
        self.code = code
        self.scope = scope
        self.unit_scale = unit_scale
        self.pages = [1]
        self.columns = [{"index": 0, "role": "current", "label": "本期",
                         "year": "2024"}]
        self.rows = []


def _row(stmt, key, value):
    stmt.rows.append({"key": key, "values": {0: {"value": str(value)}}}
                      if False else _R(key, value))


class _R:
    def __init__(self, key, value):
        self.key = key
        self.values = {0: {"value": str(value)}}


def test_checks_flag_an_unbalanced_sheet():
    bs = _Stmt("BS", "consolidated")
    for key, val in {
        "total_assets": 1000, "total_liabilities": 600,
        "total_equity": 300, "total_liabilities_and_equity": 1000,
    }.items():
        _row(bs, key, val)
    findings = run_checks([bs])
    bal = [f for f in findings if f["rule"] == "bs_balance"]
    assert bal and bal[0]["status"] == "error"
    # actual (1,000) minus expected (900)
    assert Decimal(bal[0]["diff"]) == Decimal("100")


def test_checks_pass_a_balanced_sheet():
    bs = _Stmt("BS", "consolidated")
    for key, val in {
        "total_assets": 1000, "total_liabilities": 600,
        "total_equity": 400, "total_liabilities_and_equity": 1000,
    }.items():
        _row(bs, key, val)
    findings = run_checks([bs])
    bal = [f for f in findings if f["rule"] == "bs_balance"]
    assert bal and bal[0]["status"] == "ok"
