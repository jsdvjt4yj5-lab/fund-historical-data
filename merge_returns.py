#!/usr/bin/env python3
"""
merge_returns.py

Combines the two scrapers' outputs into the single data/fund_returns.json
that index.html actually reads:

  - scrape_pdf_returns.py  -> data/fund_returns_pdf.json
        Direct fetch from confirmed official HSBC/manager PDFs. No browser
        automation, no fuzzy-match risk. Treated as the trusted source
        whenever it has a successful result for a fund.
  - scrape_fund_returns.py -> data/fund_returns_fsmone.json
        FSMOne Playwright search+scrape, covers the full 70-fund roster as
        a fallback. Used only for funds the PDF pass didn't (successfully)
        cover.

For any fund_name present in both with a successful PDF result, the PDF
result wins outright (matched_url/match_method/returns all replaced) --
this is intentional: a hardcoded, verified URL is strictly more trustworthy
than a fuzzy-matched search result, even a high-confidence one.

Usage:
    python merge_returns.py \\
        --pdf data/fund_returns_pdf.json \\
        --fsmone data/fund_returns_fsmone.json \\
        --out data/fund_returns.json
"""
import argparse
import json
from datetime import datetime, timezone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="data/fund_returns_pdf.json")
    ap.add_argument("--fsmone", default="data/fund_returns_fsmone.json")
    ap.add_argument("--out", default="data/fund_returns.json")
    args = ap.parse_args()

    try:
        with open(args.pdf, encoding="utf-8") as f:
            pdf_data = json.load(f)
    except FileNotFoundError:
        pdf_data = {"funds": []}

    try:
        with open(args.fsmone, encoding="utf-8") as f:
            fsmone_data = json.load(f)
    except FileNotFoundError:
        fsmone_data = {"funds": [], "periods": ["3mo", "6mo", "1yr", "3yr", "5yr", "10yr"], "total_targets": 0}

    pdf_by_name = {f["target_fund_name"]: f for f in pdf_data.get("funds", []) if f.get("ok")}

    merged = []
    pdf_used = 0
    fsmone_used = 0
    failed = 0

    for entry in fsmone_data.get("funds", []):
        name = entry["target_fund_name"]
        if name in pdf_by_name:
            p = pdf_by_name[name]
            merged.append({
                "target_fund_name": name,
                "target_manager": entry.get("target_manager", ""),
                "matched_url": p["matched_url"],
                "match_confidence": p["match_confidence"],
                "match_method": p["match_method"],
                "returns": p["returns"],
                "ok": True,
                "source": "pdf",
            })
            pdf_used += 1
        else:
            entry = dict(entry)
            entry["source"] = "fsmone"
            merged.append(entry)
            if entry.get("ok"):
                fsmone_used += 1
            else:
                failed += 1

    # Funds that are in the PDF map but for some reason weren't in the
    # FSMOne target list at all (shouldn't normally happen, but don't
    # silently drop data if it does).
    fsmone_names = {e["target_fund_name"] for e in fsmone_data.get("funds", [])}
    for name, p in pdf_by_name.items():
        if name not in fsmone_names:
            merged.append({
                "target_fund_name": name,
                "target_manager": "",
                "matched_url": p["matched_url"],
                "match_confidence": p["match_confidence"],
                "match_method": p["match_method"],
                "returns": p["returns"],
                "ok": True,
                "source": "pdf",
            })
            pdf_used += 1

    output = {
        "last_scraped": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "periods": fsmone_data.get("periods", ["3mo", "6mo", "1yr", "3yr", "5yr", "10yr"]),
        "total_targets": len(merged),
        "matched_ok": pdf_used + fsmone_used,
        "matched_via_pdf": pdf_used,
        "matched_via_fsmone": fsmone_used,
        "failed": len(merged) - pdf_used - fsmone_used,
        "funds": merged,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Merged: {pdf_used} via PDF, {fsmone_used} via FSMOne, "
          f"{output['failed']} failed, {len(merged)} total. Wrote {args.out}")


if __name__ == "__main__":
    main()
