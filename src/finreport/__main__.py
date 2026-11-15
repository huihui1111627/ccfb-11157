"""CLI entry point: generate | extract | evaluate | serve."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_generate(args) -> None:
    from .generate import generate
    manifest = generate(Path(args.out), args.n, args.scanned_every,
                        clean=not args.keep)
    print(f"generated {manifest['count']} documents "
          f"({manifest['scanned']} scanned) in {args.out}")


def cmd_extract(args) -> None:
    from .pipeline import extract_document
    src = Path(args.pdf)
    ocr = args.ocr
    result = extract_document(src, upload_dir=Path(args.workdir), force_ocr=ocr)
    out = Path(args.out) if args.out else src.with_suffix(".extracted.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"{src.name}: {len(result['statements'])} statements, "
          f"{len(result['findings'])} findings -> {out}")


def cmd_evaluate(args) -> None:
    from .evalrun import run_evaluation
    result = run_evaluation(Path(args.corpus), ocr=args.ocr,
                            limit=args.limit, workers=args.workers,
                            doc_ids=args.doc or None)
    out = Path(args.out) if args.out else Path("data/eval/report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    ov = result["summary"]["_overall"]
    print(f"documents={ov['documents']} statements={ov['statements']} "
          f"value_accuracy={ov['value_accuracy']:.4f} "
          f"unit_accuracy={ov['unit_accuracy']:.4f} "
          f"page_accuracy={ov['page_accuracy']:.4f} -> {out}")


def cmd_serve(args) -> None:
    import uvicorn
    uvicorn.run("finreport.server:app", host=args.host, port=args.port,
                reload=args.reload)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="finreport")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="build the synthetic corpus")
    g.add_argument("--out", default="data/corpus")
    g.add_argument("--n", type=int, default=24)
    g.add_argument("--scanned-every", type=int, default=4)
    g.add_argument("--keep", action="store_true")
    g.set_defaults(func=cmd_generate)

    e = sub.add_parser("extract", help="extract one PDF")
    e.add_argument("pdf")
    e.add_argument("--out")
    e.add_argument("--ocr", action="store_true")
    e.add_argument("--workdir", default="data/work")
    e.set_defaults(func=cmd_extract)

    v = sub.add_parser("evaluate", help="score against corpus truth")
    v.add_argument("--corpus", default="data/corpus")
    v.add_argument("--out", default="data/eval/report.json")
    v.add_argument("--ocr", action="store_true")
    v.add_argument("--limit", type=int, default=None)
    v.add_argument("--doc", action="append")
    v.add_argument("--workers", type=int, default=1)
    v.set_defaults(func=cmd_evaluate)

    s = sub.add_parser("serve", help="run the review UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
