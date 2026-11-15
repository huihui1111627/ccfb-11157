"""Generate the synthetic annual-report corpus and its ground truth."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .corpus import Style, build_document, rasterize
from .data import build_company

COMPANIES = [
    ("600519", "贵州茅台酒股份有限公司", "贵州茅台"),
    ("000001", "平安银行股份有限公司", "平安银行"),
    ("600036", "招商银行股份有限公司", "招商银行"),
    ("000333", "美的集团股份有限公司", "美的集团"),
    ("600276", "江苏恒瑞医药股份有限公司", "恒瑞医药"),
    ("002415", "杭州海康威视数字技术股份有限公司", "海康威视"),
    ("601012", "隆基绿能科技股份有限公司", "隆基绿能"),
    ("600048", "保利发展控股集团股份有限公司", "保利发展"),
    ("002594", "比亚迪股份有限公司", "比亚迪"),
    ("600887", "内蒙古伊利实业集团股份有限公司", "伊利股份"),
    ("000651", "珠海格力电器股份有限公司", "格力电器"),
    ("601166", "兴业银行股份有限公司", "兴业银行"),
    ("600030", "中信证券股份有限公司", "中信证券"),
    ("000858", "宜宾五粮液股份有限公司", "五粮液"),
    ("601899", "紫金矿业集团股份有限公司", "紫金矿业"),
    ("600585", "安徽海螺水泥股份有限公司", "海螺水泥"),
    ("002304", "江苏洋河酒厂股份有限公司", "洋河股份"),
    ("600900", "中国长江电力股份有限公司", "长江电力"),
    ("601318", "中国平安保险（集团）股份有限公司", "中国平安"),
    ("000725", "京东方科技集团股份有限公司", "京东方"),
]

# documents whose statements are deliberately unbalanced, to exercise 勾稽校验
BROKEN = {
    "doc_07": {"balance"},
    "doc_13": {"subtotal"},
    "doc_18": {"operating_profit", "net_profit_parent"},
    "doc_21": {"equity_total"},
    "doc_24": {"cash_recon"},
}


def generate(out_dir: Path, n_docs: int = 24, scanned_every: int = 4,
             clean: bool = True) -> dict:
    out_dir = Path(out_dir)
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "pdf").mkdir(parents=True, exist_ok=True)
    (out_dir / "truth").mkdir(parents=True, exist_ok=True)

    manifest = {"documents": [], "generated_by": "finreport.corpus"}
    scanned_count = 0
    for i in range(n_docs):
        doc_no = i + 1
        doc_id = f"doc_{doc_no:02d}"
        code, name, short = COMPANIES[i % len(COMPANIES)]
        scanned = (doc_no % scanned_every == 0)
        seed = 1000 + doc_no * 37
        style = Style.sample(seed, scanned=scanned)
        broken = BROKEN.get(doc_id, set())
        years = ("2022", "2023") if doc_no % 5 == 0 else ("2023", "2024")
        company = build_company(code, name, short, seed=seed,
                               broken=broken, years=years)
        company.scale = style.unit_scale
        company.places = style.places

        meta = build_document(out_dir / "pdf", doc_id, company, style)
        if scanned:
            src = out_dir / "pdf" / f"{doc_id}.pdf"
            tmp = out_dir / "pdf" / f"{doc_id}.digital.pdf"
            src.rename(tmp)
            rasterize(tmp, src, dpi=190 + (doc_no % 3) * 20,
                      rotate=(-0.4 if doc_no % 8 == 0 else 0.0))
            tmp.unlink()
            scanned_count += 1
        meta["broken"] = sorted(broken)
        (out_dir / "truth" / f"{doc_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1))
        manifest["documents"].append({
            "doc_id": doc_id,
            "pdf": f"pdf/{doc_id}.pdf",
            "truth": f"truth/{doc_id}.json",
            "company": meta["company"]["name"],
            "unit": meta["unit"],
            "scope_mode": meta["scope_mode"],
            "scanned": scanned,
            "pages": meta["pages"],
            "broken": sorted(broken),
        })
    manifest["count"] = len(manifest["documents"])
    manifest["scanned"] = scanned_count
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/corpus")
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--scanned-every", type=int, default=4)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    manifest = generate(Path(args.out), args.n, args.scanned_every,
                        clean=not args.keep)
    print(f"generated {manifest['count']} documents "
          f"({manifest['scanned']} scanned) in {args.out}")


if __name__ == "__main__":
    main()
