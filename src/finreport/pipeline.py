"""End-to-end document pipeline: PDF in, structured statements + findings out."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from .accounts import BY_KEY, STATEMENT_NAMES, normalize_label
from .extract import (
    ExtractedStatement, extract_statements, scan_titles, statement_title,
)
from .pageparse import text_lines, words_from_pdf
from .validate import run_checks

OCR_TEXT_THRESHOLD = 40  # characters on a page below this => likely scanned


def _page_text_length(page) -> int:
    return len(re.sub(r"\s+", "", page.get_text("text") or ""))


def detect_scanned(document) -> bool:
    """True when the document has no usable text layer.

    Digital reports sometimes carry sparse pages, so the median page is used
    rather than the mean: a scan has a median near zero, a digital report does
    not.
    """
    if document.page_count == 0:
        return False
    lengths = sorted(_page_text_length(document[i])
                     for i in range(document.page_count))
    return lengths[len(lengths) // 2] < OCR_TEXT_THRESHOLD


def doc_id_for(path: Path, data: bytes | None = None) -> str:
    h = hashlib.sha1()
    h.update(data if data is not None else path.read_bytes())
    return h.hexdigest()[:12]


def _needs_ocr_fallback(document) -> bool:
    """True when a text layer exists but statement titles are image-only."""
    titles = scan_titles(document)
    return len(titles) < 3


def extract_document(path: Path, *, upload_dir: Path | None = None,
                     force_ocr: bool = False, doc_id: str | None = None,
                     title: str | None = None) -> dict:
    import pymupdf

    path = Path(path)
    started = time.time()
    document = pymupdf.open(path)
    scanned = force_ocr or detect_scanned(document)
    ocr_mode = scanned
    if not scanned and _needs_ocr_fallback(document):
        scanned = True
        ocr_mode = True

    imported = extract_statements(document, ocr=ocr_mode)
    statements = imported
    if not statements and not scanned and _needs_ocr_fallback(document):
        scanned = True
        ocr_mode = True
        statements = extract_statements(document, ocr=True)

    confidences = [v.get("confidence", 1.0)
                   for st in statements for r in st.rows
                   for v in r.values.values()]
    low_conf = sum(1 for c in confidences if c < 0.85)

    findings = run_checks(statements)
    payload = {
        "doc_id": doc_id or doc_id_for(path),
        "file_name": title or path.name,
        "company": detect_company(document, ocr_mode),
        "path": str(path),
        "pages": document.page_count,
        "scanned": scanned,
        "ocr_pages": [st.pages for st in statements] if scanned else [],
        "elapsed_ms": int((time.time() - started) * 1000),
        "statements": [_statement_payload(st) for st in statements],
        "findings": findings,
        "confidence_summary": {
            "values": len(confidences),
            "low_confidence_values": low_conf,
            "min_confidence": min(confidences) if confidences else None,
        },
        "warnings": sorted({w for st in statements for w in st.warnings}),
    }
    document.close()
    if upload_dir is not None:
        upload_dir = Path(upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / f"{payload['doc_id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1))
    return payload


_COMPANY_RE = re.compile(r"^[\u4e00-\u9fff（）()A-Za-z·]{4,40}?"
                         r"(?:股份有限公司|有限责任公司|有限公司)$")


def detect_company(document, ocr: bool = False) -> str | None:
    """Company name from the title block of the first statement pages."""
    for pno in range(min(document.page_count, 8)):
        page = document[pno]
        if ocr:
            from .extract import _title_lines
            lines = [t for _, t in _title_lines(page, True)]
        else:
            lines = [t for _, t in text_lines(page)[:8]]
        for text in lines:
            t = re.sub(r"\s+", "", text)
            m = _COMPANY_RE.match(t)
            if m:
                return m.group(0)
    return None


def _statement_payload(st: ExtractedStatement) -> dict:
    return {
        "code": st.code,
        "statement_name": STATEMENT_NAMES.get(st.code, st.code),
        "scope": st.scope,
        "scope_name": {"consolidated": "合并", "parent": "母公司"}.get(
            st.scope, st.scope),
        "title": st.title,
        "pages": st.pages,
        "unit": st.unit,
        "unit_scale": st.unit_scale,
        "scanned": st.scanned,
        "header_text": st.header_text,
        "years": st.years,
        "columns": st.columns,
        "warnings": st.warnings,
        "rows": [
            {
                "key": r.key,
                "canonical": r.canonical,
                "section": BY_KEY[r.key].section,
                "label": r.label,
                "match_method": r.method,
                "match_score": r.score,
                "page": r.page,
                "values": {str(k): v for k, v in r.values.items()},
            }
            for r in st.rows
        ],
    }


def load_extracted(path: Path) -> dict:
    return json.loads(Path(path).read_text())
