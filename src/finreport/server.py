"""FastAPI server for the review console."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .accounts import BY_KEY, chart_payload, normalize_label
from .pipeline import extract_document
from .validate import run_checks

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
DATA = ROOT / "data"
STORE = DATA / "store"
CORPUS = DATA / "corpus"
EVAL = DATA / "eval"
STORE.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="财报抽取复核台")


# ---------------------------------------------------------------------------
# storage helpers
# ---------------------------------------------------------------------------

def _meta_path(doc_id: str) -> Path:
    return STORE / f"{doc_id}.json"


def _review_path(doc_id: str) -> Path:
    return STORE / f"{doc_id}.review.json"


def load_doc(doc_id: str) -> dict:
    p = _meta_path(doc_id)
    if not p.exists():
        raise HTTPException(404, f"document {doc_id} not found")
    return json.loads(p.read_text())


def save_doc(doc: dict) -> None:
    _meta_path(doc["doc_id"]).write_text(
        json.dumps(doc, ensure_ascii=False, indent=1))


def load_review(doc_id: str) -> dict:
    p = _review_path(doc_id)
    if p.exists():
        return json.loads(p.read_text())
    return {"doc_id": doc_id, "overrides": {}, "status": {},
            "aliases": {}, "updated_at": None}


def save_review(doc_id: str, review: dict) -> None:
    review["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _review_path(doc_id).write_text(
        json.dumps(review, ensure_ascii=False, indent=1))


def _register_pdf(doc_id: str, src: Path) -> Path:
    """Keep a copy of the PDF inside the store so previews never break."""
    dst = STORE / f"{doc_id}.pdf"
    if not dst.exists():
        shutil.copyfile(src, dst)
    return dst


def ingest(path: Path, doc_id: str | None = None,
           title: str | None = None, force_ocr: bool = False) -> dict:
    result = extract_document(path, doc_id=doc_id, title=title,
                              force_ocr=force_ocr)
    doc_id = result["doc_id"]
    result["pdf"] = str(_register_pdf(doc_id, Path(path)))
    result["source"] = "corpus" if "corpus" in str(path) else "upload"
    result["ingested_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    result["revision"] = 0
    save_doc(result)
    return result


# ---------------------------------------------------------------------------
# review-aware recomputation
# ---------------------------------------------------------------------------

def _apply_overrides(doc: dict, review: dict) -> dict:
    """Copy of the document with manual edits applied (never mutates doc)."""
    out = json.loads(json.dumps(doc))
    for st in out["statements"]:
        for row in st["rows"]:
            for col, cell in row["values"].items():
                key = f"{st['code']}.{st['scope']}.{row['key']}.{col}"
                if key in review["overrides"]:
                    ov = review["overrides"][key]
                    cell["value"] = ov.get("value")
                    cell["edited"] = True
                    cell["edit_note"] = ov.get("note", "")
        for row in st["rows"]:
            aliaskey = f"{st['code']}.{normalize_label(row['label'])}"
            if aliaskey in review["aliases"]:
                new_key = review["aliases"][aliaskey]
                if new_key in BY_KEY:
                    row["key"] = new_key
                    row["canonical"] = BY_KEY[new_key].name
                    row["edited"] = True
    return out


def _recompute(doc: dict) -> dict:
    """Re-run cross checks over edited values."""
    from .evalrun import _Shim  # reuse the adapter used by the evaluator

    stmts = [_Shim(st) for st in doc["statements"]]
    findings = run_checks(stmts)
    payload = dict(doc)
    payload["findings"] = findings
    return payload


# ---------------------------------------------------------------------------
# API: documents
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/chart")
def chart() -> dict:
    return {"accounts": chart_payload()}


@app.get("/api/documents")
def list_documents() -> dict:
    docs = []
    for p in sorted(STORE.glob("*.json")):
        if p.name.endswith(".review.json"):
            continue
        d = json.loads(p.read_text())
        review = load_review(d["doc_id"])
        edits = len(review.get("overrides", {}))
        errs = sum(1 for f in d.get("findings", []) if f["status"] == "error")
        warns = sum(1 for f in d.get("findings", []) if f["status"] == "warning")
        docs.append({
            "doc_id": d["doc_id"],
            "file_name": d.get("file_name"),
            "company": d.get("company") or _company_name(d),
            "pages": d.get("pages"),
            "scanned": d.get("scanned"),
            "statements": [
                {"code": s["code"], "scope": s["scope"],
                 "rows": len(s["rows"]), "unit": s["unit"],
                 "pages": s["pages"]}
                for s in d.get("statements", [])
            ],
            "findings": {"error": errs, "warning": warns},
            "edits": edits,
            "confidence_summary": d.get("confidence_summary"),
            "ingested_at": d.get("ingested_at"),
            "source": d.get("source"),
        })
    return {"documents": docs}


def _company_name(doc: dict) -> str:
    for st in doc.get("statements", []):
        title = st.get("header_text") or ""
        for line in re.split(r"[\s]{2,}", title):
            if "公司" in line:
                return line.strip()
    return doc.get("file_name") or doc["doc_id"]


import re  # noqa: E402  (used by _company_name)


@app.post("/api/documents/upload")
async def upload(file: UploadFile = File(...),
                 ocr: bool = Query(False)) -> dict:
    name = file.filename or "upload.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 文件")
    tmp = STORE / f"upload-{int(time.time() * 1000)}.pdf"
    tmp.write_bytes(await file.read())
    result = ingest(tmp, title=name, force_ocr=ocr)
    tmp.unlink(missing_ok=True)
    return result


@app.post("/api/documents/import-corpus")
def import_corpus(limit: int = Query(8), ocr: bool = Query(False)) -> dict:
    manifest_path = CORPUS / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "未找到语料库，请先运行 finreport generate")
    manifest = json.loads(manifest_path.read_text())
    imported = []
    for d in manifest["documents"][:limit]:
        existing = _meta_path(d["doc_id"])
        if existing.exists():
            imported.append(d["doc_id"])
            continue
        pdf = CORPUS / d["pdf"]
        result = ingest(pdf, doc_id=d["doc_id"], force_ocr=ocr)
        imported.append(result["doc_id"])
    return {"imported": imported}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, apply_edits: bool = Query(True)) -> dict:
    doc = load_doc(doc_id)
    review = load_review(doc_id)
    payload = _apply_overrides(doc, review) if apply_edits else doc
    payload = _recompute(payload)
    payload["review"] = review
    return payload


@app.get("/api/documents/{doc_id}/pdf")
def get_pdf(doc_id: str) -> FileResponse:
    doc = load_doc(doc_id)
    path = Path(doc.get("pdf", ""))
    if not path.exists():
        raise HTTPException(404, "PDF 文件不存在")
    return FileResponse(path, media_type="application/pdf",
                        filename=doc.get("file_name") or path.name)


@app.post("/api/documents/{doc_id}/recheck")
def recheck(doc_id: str) -> dict:
    doc = load_doc(doc_id)
    review = load_review(doc_id)
    payload = _recompute(_apply_overrides(doc, review))
    save_doc(_recompute(doc))
    return {"findings": payload["findings"]}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str) -> dict:
    _meta_path(doc_id).unlink(missing_ok=True)
    _review_path(doc_id).unlink(missing_ok=True)
    (STORE / f"{doc_id}.pdf").unlink(missing_ok=True)
    return {"deleted": doc_id}


# ---------------------------------------------------------------------------
# API: review edits
# ---------------------------------------------------------------------------

class OverrideIn(BaseModel):
    code: str
    scope: str
    key: str
    col: str
    value: str | None
    note: str = ""


@app.post("/api/documents/{doc_id}/override")
def set_override(doc_id: str, body: OverrideIn) -> dict:
    load_doc(doc_id)
    review = load_review(doc_id)
    k = f"{body.code}.{body.scope}.{body.key}.{body.col}"
    review["overrides"][k] = {"value": body.value, "note": body.note}
    if body.value is None:
        review["overrides"].pop(k, None)
    save_review(doc_id, review)
    doc = load_doc(doc_id)
    payload = _recompute(_apply_overrides(doc, review))
    payload["review"] = review
    return payload


class RejectIn(BaseModel):
    path: str
    reason: str = ""


@app.post("/api/documents/{doc_id}/reject")
def reject(doc_id: str, body: RejectIn) -> dict:
    review = load_review(doc_id)
    review.setdefault("rejected", {})[body.path] = body.reason
    save_review(doc_id, review)
    return review


class AliasIn(BaseModel):
    code: str
    label: str
    key: str | None


@app.post("/api/documents/{doc_id}/alias")
def set_alias(doc_id: str, body: AliasIn) -> dict:
    load_doc(doc_id)
    review = load_review(doc_id)
    k = f"{body.code}.{normalize_label(body.label)}"
    if body.key:
        review["aliases"][k] = body.key
    else:
        review["aliases"].pop(k, None)
    save_review(doc_id, review)
    return review


class StatusIn(BaseModel):
    state: str  # unreviewed | in_progress | approved


@app.post("/api/documents/{doc_id}/status")
def set_status(doc_id: str, body: StatusIn) -> dict:
    review = load_review(doc_id)
    review["status"] = body.state
    save_review(doc_id, review)
    return review


# ---------------------------------------------------------------------------
# API: evaluation report + exports
# ---------------------------------------------------------------------------

@app.get("/api/eval")
def eval_report() -> dict:
    p = EVAL / "report.json"
    if not p.exists():
        return {"available": False}
    data = json.loads(p.read_text())
    data["available"] = True
    return data


@app.get("/api/documents/{doc_id}/export.csv")
def export_csv(doc_id: str) -> Response:
    import csv
    import io

    doc = load_doc(doc_id)
    review = load_review(doc_id)
    doc = _apply_overrides(doc, review)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["报表", "口径", "科目代码", "标准科目", "原文项目", "列",
                "年度", "金额(元)", "页", "置信度"])
    for st in doc["statements"]:
        for row in st["rows"]:
            for col, cell in row["values"].items():
                w.writerow([
                    st["statement_name"], st["scope_name"], row["key"],
                    row["canonical"], row["label"], col, cell.get("year", ""),
                    cell.get("value", ""), cell.get("page", ""),
                    cell.get("confidence", ""),
                ])
    return Response(buf.getvalue().encode("utf-8-sig"),
                    media_type="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename={doc_id}.csv"})


@app.get("/api/documents/{doc_id}/page/{page_no}.png")
def page_image(doc_id: str, page_no: int, dpi: int = Query(150)) -> Response:
    import pymupdf

    doc = load_doc(doc_id)
    path = Path(doc["pdf"])
    if not path.exists():
        raise HTTPException(404, "PDF 文件不存在")
    with pymupdf.open(path) as pdf:
        if not (1 <= page_no <= pdf.page_count):
            raise HTTPException(404, "页码超出范围")
        pix = pdf[page_no - 1].get_pixmap(dpi=min(dpi, 220))
        return Response(pix.tobytes("png"), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------------------
# static UI
# ---------------------------------------------------------------------------

if WEB.exists():
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")
