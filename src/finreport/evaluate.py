"""Score extractions against corpus ground truth, per account and per document."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass
class ValueResult:
    key: str
    code: str
    scope: str
    year: str
    expected: str
    actual: str | None
    matched: bool
    printed: str | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key, "code": self.code, "scope": self.scope,
            "year": self.year, "expected": self.expected,
            "actual": self.actual, "matched": self.matched,
            "printed": self.printed,
        }


@dataclass
class StatementScore:
    doc_id: str
    code: str
    scope: str
    expected_unit: str = ""
    actual_unit: str = ""
    unit_ok: bool = False
    expected_pages: list[int] = field(default_factory=list)
    actual_pages: list[int] = field(default_factory=list)
    pages_ok: bool = False
    results: list[ValueResult] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    extra_keys: list[str] = field(default_factory=list)
    label_mismatches: list[dict] = field(default_factory=list)

    @property
    def expected_values(self) -> int:
        return len(self.results)

    @property
    def matched_values(self) -> int:
        return sum(1 for r in self.results if r.matched)

    @property
    def value_accuracy(self) -> float:
        return self.matched_values / self.expected_values if self.results else 0.0

    @property
    def wrong_values(self) -> list[ValueResult]:
        return [r for r in self.results if not r.matched]

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id, "code": self.code, "scope": self.scope,
            "expected_values": self.expected_values,
            "matched_values": self.matched_values,
            "value_accuracy": round(self.value_accuracy, 4),
            "missing_keys": self.missing_keys,
            "extra_keys": self.extra_keys,
            "wrong_values": [w.as_dict() for w in self.wrong_values],
            "label_mismatches": self.label_mismatches,
            "unit_ok": self.unit_ok,
            "expected_unit": self.expected_unit,
            "actual_unit": self.actual_unit,
            "pages_ok": self.pages_ok,
            "expected_pages": self.expected_pages,
            "actual_pages": self.actual_pages,
        }


def load_truth(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def score_document(meta: dict, extracted: list) -> list[StatementScore]:
    by_key: dict[tuple[str, str], list] = {}
    for st in extracted:
        by_key.setdefault((st.code, st.scope), []).append(st)
    scores: list[StatementScore] = []
    for truth_stmt in meta["statements"]:
        code = truth_stmt["code"]
        truth_by_scope = truth_stmt["truth"]
        for scope, truth in truth_by_scope.items():
            candidates = by_key.get((code, scope), [])
            scores.append(_score_one(meta, truth_stmt, scope, truth,
                                     candidates))
    return scores


def _score_one(meta, truth_stmt, scope, truth, candidates) -> StatementScore:
    pages = list(range(truth_stmt["page_start"], truth_stmt["page_end"] + 1))
    sc = StatementScore(doc_id=meta["doc_id"], code=truth_stmt["code"],
                        scope=scope, expected_unit=meta["unit"],
                        expected_pages=pages)
    if not candidates:
        for key, years in truth.items():
            for year, expected in years.items():
                sc.results.append(ValueResult(key, sc.code, scope, year,
                                              expected, None, False))
        sc.missing_keys = sorted(truth)
        return sc

    def overlap(st):
        return len(set(st.pages) & set(pages))

    st = max(candidates, key=overlap)
    sc.actual_unit = st.unit
    sc.unit_ok = st.unit == meta["unit"]
    sc.actual_pages = list(st.pages)
    sc.pages_ok = set(pages).issubset(set(st.pages))
    rows = {r.key: r for r in st.rows}
    sc.extra_keys = sorted(set(rows) - set(truth))
    sc.missing_keys = sorted(set(truth) - set(rows))
    year_role = {}
    for col in st.columns:
        year_role[str(col.get("year"))] = col["role"]
    for key, years in truth.items():
        row = rows.get(key)
        acc_name = None
        if row is not None and row.label:
            acc_name = row.label
        for year, expected in years.items():
            cell = None
            actual = None
            printed = None
            if row is not None:
                role = year_role.get(str(year))
                if role is None:
                    role = "current" if year == max(years) else "prior"
                col_idx = next((c["index"] for c in st.columns
                                if c["role"] == role), None)
                cell = row.values.get(col_idx) if col_idx is not None else None
                actual = cell.get("value") if cell else None
                printed = cell.get("printed") if cell else None
            exp_d, act_d = _dec(expected), _dec(actual)
            matched = exp_d is not None and act_d is not None \
                and exp_d == act_d
            sc.results.append(ValueResult(key, sc.code, scope, year,
                                          expected, actual, matched, printed))
    return sc


def aggregate(scores: list[StatementScore]) -> dict:
    def blank():
        return {"statements": 0, "expected": 0, "matched": 0,
                "unit_ok": 0, "pages_ok": 0, "missing": 0, "extra": 0}

    by_code: dict[str, dict] = {}
    for s in scores:
        d = by_code.setdefault(s.code, blank())
        d["statements"] += 1
        d["expected"] += s.expected_values
        d["matched"] += s.matched_values
        d["unit_ok"] += int(s.unit_ok)
        d["pages_ok"] += int(s.pages_ok)
        d["missing"] += len(s.missing_keys)
        d["extra"] += len(s.extra_keys)
    out = {}
    for code, d in by_code.items():
        n = d["statements"]
        out[code] = {
            "statements": n,
            "expected_values": d["expected"],
            "matched_values": d["matched"],
            "value_accuracy": round(d["matched"] / d["expected"], 4)
            if d["expected"] else 0.0,
            "unit_accuracy": round(d["unit_ok"] / n, 4) if n else 0.0,
            "page_accuracy": round(d["pages_ok"] / n, 4) if n else 0.0,
            "missing_keys": d["missing"],
            "extra_keys": d["extra"],
        }
    exp = sum(s.expected_values for s in scores)
    mat = sum(s.matched_values for s in scores)
    docs = {s.doc_id for s in scores}
    out["_overall"] = {
        "documents": len(docs),
        "statements": len(scores),
        "expected_values": exp,
        "matched_values": mat,
        "value_accuracy": round(mat / exp, 4) if exp else 0.0,
        "unit_accuracy": round(sum(1 for s in scores if s.unit_ok)
                               / len(scores), 4) if scores else 0.0,
        "page_accuracy": round(sum(1 for s in scores if s.pages_ok)
                               / len(scores), 4) if scores else 0.0,
        "statements_with_errors": sum(1 for s in scores
                                      if s.matched_values < s.expected_values),
    }
    return out


def by_account(scores: list[StatementScore]) -> list[dict]:
    acc: dict[str, dict] = {}
    for s in scores:
        for r in s.results:
            d = acc.setdefault(r.key, {"key": r.key, "code": r.code,
                                       "expected": 0, "matched": 0,
                                       "examples": []})
            d["expected"] += 1
            if r.matched:
                d["matched"] += 1
            elif len(d["examples"]) < 3:
                d["examples"].append({
                    "doc_id": s.doc_id, "scope": s.scope, "year": r.year,
                    "expected": r.expected, "actual": r.actual,
                    "printed": r.printed,
                })
    rows = []
    for d in acc.values():
        rows.append({
            "key": d["key"], "code": d["code"],
            "expected": d["expected"], "matched": d["matched"],
            "accuracy": round(d["matched"] / d["expected"], 4),
            "errors": d["expected"] - d["matched"],
            "examples": d["examples"],
        })
    rows.sort(key=lambda x: (x["accuracy"], x["code"], x["key"]))
    return rows
