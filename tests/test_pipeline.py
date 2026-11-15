"""End-to-end extraction tests on a freshly generated mini-corpus."""
from decimal import Decimal
from pathlib import Path

import pytest

from finreport.corpus import Style, build_document
from finreport.data import build_company
from finreport.evaluate import load_truth, score_document
from finreport.generate import generate
from finreport.pipeline import extract_document


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    out = tmp_path_factory.mktemp("corpus")
    generate(out, n_docs=4, scanned_every=4)
    return out


def _objects(extracted: dict):
    from finreport.evalrun import _as_objects
    return _as_objects(extracted)


@pytest.mark.parametrize("doc_id", ["doc_01", "doc_02", "doc_03"])
def test_digital_document_roundtrip(corpus, doc_id):
    truth = load_truth(corpus / "truth" / f"{doc_id}.json")
    result = extract_document(corpus / "pdf" / f"{doc_id}.pdf", doc_id=doc_id)
    assert result["scanned"] is False
    scores = score_document(truth, _objects(result))
    for s in scores:
        assert s.unit_ok, f"{s.code}/{s.scope} unit {s.actual_unit} != {s.expected_unit}"
    overall = sum(s.matched_values for s in scores) / \
        sum(s.expected_values for s in scores)
    assert overall >= 0.97, f"{doc_id} accuracy {overall:.3f}"


def test_all_three_statements_and_both_scopes_found(corpus):
    result = extract_document(corpus / "pdf" / "doc_01.pdf", doc_id="doc_01")
    found = {(s["code"], s["scope"]) for s in result["statements"]}
    expected = {("BS", "consolidated"), ("BS", "parent"),
                ("IS", "consolidated"), ("IS", "parent"),
                ("CF", "consolidated"), ("CF", "parent")}
    assert expected <= found


def test_scanned_document_uses_ocr(corpus):
    result = extract_document(corpus / "pdf" / "doc_04.pdf", doc_id="doc_04")
    assert result["scanned"] is True
    assert any(s["scanned"] for s in result["statements"])
    values = [v for s in result["statements"] for r in s["rows"]
              for v in r["values"].values()]
    assert values
    # OCR confidence is propagated for review highlighting
    assert any(v["confidence"] < 1 for v in values)


def test_every_value_has_a_source_box(corpus):
    result = extract_document(corpus / "pdf" / "doc_01.pdf", doc_id="doc_01")
    for st in result["statements"]:
        for row in st["rows"]:
            for cell in row["values"].values():
                if cell["blank"]:
                    continue
                assert cell["page"] in st["pages"]
                box = cell["box"]
                assert box and len(box) == 4
                assert box[2] >= box[0] and box[3] >= box[1]


def test_units_are_normalised_to_yuan(tmp_path):
    """The same company printed in 元 and 万元 must yield identical yuan values."""
    company = build_company("600519", "贵州茅台酒股份有限公司", "贵州茅台",
                            seed=11)
    results = {}
    for unit, scale in (("元", 1), ("万元", 10000)):
        style = Style.sample(seed=5)
        style.unit_label = unit
        style.unit_scale = scale
        style.places = 2
        style.scope_mode = "separate"
        meta = build_document(tmp_path, f"unit_{unit}", company, style)
        res = extract_document(tmp_path / f"unit_{unit}.pdf",
                              doc_id=f"unit_{unit}")
        stmt = next(s for s in res["statements"]
                    if s["code"] == "BS" and s["scope"] == "consolidated")
        results[unit] = {r["key"]: r["values"]["0"]["value"]
                         for r in stmt["rows"] if not r["values"]["0"]["blank"]}
    common = set(results["元"]) & set(results["万元"])
    assert common
    for key in common:
        assert abs(Decimal(results["元"][key]) -
                   Decimal(results["万元"][key])) <= Decimal(10000), key


def test_broken_document_reports_a_finding(tmp_path):
    company = build_company("000002", "测试股份有限公司", "测试", seed=21,
                            broken={"balance"})
    style = Style.sample(seed=9)
    style.scope_mode = "separate"
    build_document(tmp_path, "broken", company, style)
    res = extract_document(tmp_path / "broken.pdf", doc_id="broken")
    errors = [f for f in res["findings"] if f["status"] == "error"]
    assert errors
    assert any(f["rule"] == "bs_balance" for f in errors)


def test_combined_table_columns_get_current_and_prior():
    """A four-column combined table must yield current/prior per scope."""
    import tempfile

    from finreport.corpus import Style
    company = build_company("601318", "中国平安保险（集团）股份有限公司", "中国平安",
                            seed=31)
    style = Style.sample(seed=17)
    style.scope_mode = "combined"
    style.unit_label = "元"
    style.unit_scale = 1
    with tempfile.TemporaryDirectory() as tmp:
        build_document(Path(tmp), "combined", company, style)
        res = extract_document(Path(tmp) / "combined.pdf", doc_id="combined")
    for st in res["statements"]:
        roles = sorted(c["role"] for c in st["columns"])
        assert roles == ["current", "prior"], (st["code"], st["scope"], roles)
        assert all(c["year"] for c in st["columns"])
