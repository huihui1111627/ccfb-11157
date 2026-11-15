"""Batch evaluation over the synthetic corpus."""
from __future__ import annotations

import json
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .evaluate import aggregate, by_account, load_truth, score_document
from .pipeline import extract_document


def evaluate_one(job: tuple[str, str, bool]) -> dict:
    doc_id, pdf_path, ocr = job
    try:
        result = extract_document(Path(pdf_path), force_ocr=ocr, doc_id=doc_id)
        return {"doc_id": doc_id, "ok": True, "extracted": result}
    except Exception as exc:  # keep the batch going, report the failure
        return {"doc_id": doc_id, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=4)}


def run_evaluation(corpus: Path, *, ocr: bool = False, limit: int | None = None,
                   workers: int = 1, doc_ids: list[str] | None = None) -> dict:
    corpus = Path(corpus)
    manifest = json.loads((corpus / "manifest.json").read_text())
    docs = manifest["documents"]
    if doc_ids:
        wanted = set(doc_ids)
        docs = [d for d in docs if d["doc_id"] in wanted]
    if limit:
        docs = docs[:limit]

    jobs = [(d["doc_id"], str(corpus / d["pdf"]), ocr) for d in docs]
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(evaluate_one, jobs))
    else:
        results = [evaluate_one(j) for j in jobs]

    per_document = []
    all_scores = []
    failures = []
    for doc_meta, res in zip(docs, results):
        if not res["ok"]:
            failures.append({"doc_id": res["doc_id"], "error": res["error"],
                             "trace": res.get("trace")})
            continue
        truth = load_truth(corpus / doc_meta["truth"])
        scores = score_document(truth, _as_objects(res["extracted"]))
        all_scores.extend(scores)
        per_document.append({
            "doc_id": res["doc_id"],
            "truth_unit": truth["unit"],
            "scanned": truth["scanned"],
            "scope_mode": truth["scope_mode"],
            "statements": [s.as_dict() for s in scores],
            "detected_scanned": res["extracted"]["scanned"],
            "elapsed_ms": res["extracted"]["elapsed_ms"],
        })

    summary = aggregate(all_scores)
    accounts = by_account(all_scores)
    return {
        "corpus": str(corpus),
        "ocr_forced": ocr,
        "summary": summary,
        "per_account": accounts,
        "per_document": per_document,
        "failures": failures,
    }


class _Shim:
    """Adapter: turn the serialised statement dicts back into objects."""

    def __init__(self, d: dict):
        self.code = d["code"]
        self.scope = d["scope"]
        self.unit = d["unit"]
        self.pages = d["pages"]
        self.columns = d["columns"]
        self.rows = [_ShimRow(r) for r in d["rows"]]


class _ShimRow:
    def __init__(self, d: dict):
        self.key = d["key"]
        self.label = d["label"]
        self.values = {int(k): v for k, v in d["values"].items()}


def _as_objects(extracted: dict) -> list:
    return [_Shim(s) for s in extracted["statements"]]
