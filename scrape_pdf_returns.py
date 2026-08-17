#!/usr/bin/env python3
"""
scrape_pdf_returns.py

Fetches fund historical returns directly from official static PDF documents
hosted on www.insurance.hsbc.com.sg, instead of scraping FSMOne's fragile
Angular search UI.

This is the "PDF" half of the hybrid approach the user picked:
  "Hybrid: PDFs + fix FSMOne search" -- for the funds below we have found a
  confirmed, official HSBC Life / fund-manager PDF that contains a clean,
  structured returns table (no browser automation needed, no wrong-match
  risk from fuzzy search matching).

Three PDF families are handled, each with a different table layout:

  TYPE_A "Fund Summary" (HSBC Life's own doc, e.g. section
    "10. Performance of the ILP Sub-Fund"):
      Average Annual Compounded Returns
      3mths 6mths 1yr 3yrs* 5yrs* 10yrs* Since Inception**
    The fund's own row is always the FIRST numeric row for a given
    currency block (benchmark row(s) follow). Periods map 1:1 to our
    schema (drop "Since Inception").

  TYPE_B "Factsheet" (manager-branded, e.g. BlackRock's BGF factsheets):
      CUMULATIVE & ANNUALISED PERFORMANCE
      1m 3m 6m YTD 1y 3y 5y S.I.
    No 10y column -- left as null. 3m/6m/1y come from the same row as
    3y/5y (single combined row), unlike TYPE_C below.

  TYPE_C "table-one / table-three" (older-style "HSBC Insurance ___ Fund"
    one-pagers, used for the internally-branded "HSBC Life ___" funds
    inherited from AXA/Premium-era rebrand):
      Cumulative Total Returns:            3mth 6mth 1yr 3yr 5yr 10yr SI  (7 numbers)
      Average Annual Compounded Returns:                     3yr 5yr 10yr SI  (4 numbers)
    We take 3mo/6mo/1yr from the CUMULATIVE row and 3yr/5yr/10yr from the
    ANNUALISED row, matching the convention used everywhere else in this
    pipeline (short periods = point-to-point %, long periods = annualised).

Only funds with a CONFIRMED, verified PDF URL are hardcoded in FUND_PDF_MAP
below. Everything else is deliberately left out so it falls through to the
FSMOne Playwright scraper (scrape_fund_returns.py) as a fallback -- we do
NOT guess at unverified URLs, since a wrong guess is worse than no data
(this is exactly the class of bug that broke the FSMOne scraper earlier:
silent wrong-fund matches).

Usage:
    python scrape_pdf_returns.py --out data/fund_returns_pdf.json
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

import requests

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. pip install pdfplumber", file=sys.stderr)
    raise

HSBC_DAM = "https://www.insurance.hsbc.com.sg/content/dam/hsbc/insn/documents"

# ---------------------------------------------------------------------------
# Confirmed fund -> PDF mapping.
# Keys MUST exactly match `fund_name` in targets.csv.
# "currency" tells the extractor which currency block to prefer when a PDF
# contains both SGD and USD rows for the same fund (based on the currency
# suffix in the fund's own name / share class).
# ---------------------------------------------------------------------------
FUND_PDF_MAP = {
    "Franklin Income Fund A(Mdis)SGD-H1": {
        "url": f"{HSBC_DAM}/funds/ilpfund/franklin/us-income-fund/factsheet.pdf",
        "type": "B",
        "currency": "SGD",
    },
    "Franklin Technology Fund A (Acc) USD": {
        "url": f"{HSBC_DAM}/funds/ilpfund/franklin/technology-fund/factsheet/franklin-technology-ffs-sgdusd.pdf",
        "type": "B",
        "currency": "USD",
    },
    "BlackRock Global Funds - Global Allocation Fund A2 SGD Hedged": {
        "url": f"{HSBC_DAM}/funds/ilpfund/blackrock/global-allocation-fund/fund-summary/bgf-globalallocation-fs.pdf",
        "type": "A",
        "currency": "SGD",
    },
    "BlackRock Global Funds - World Gold Fund A2 SGD Hedged": {
        "url": f"{HSBC_DAM}/funds/ilpfund/blackrock/world-gold-fund/fund-summary/bgf-worldgold-fs.pdf",
        "type": "A",
        "currency": "SGD",
    },
    "PIMCO GIS Income Fund Administrative SGD (Hedged) Income": {
        "url": f"{HSBC_DAM}/funds/ilpfund/pimco/gobal-gis-bond/summary.pdf",
        "type": "A",
        "currency": "SGD",
    },
    "HSBC Global Investment Funds - Asia Pacific ex Japan Equity High Dividend S48M2SGD": {
        "url": f"{HSBC_DAM}/funds/ilpfund/hgif/asiapac-exjap-hidiv/factsheet/hgif-asiapac-exjap-hidiv-ffs-sgdusd.pdf",
        "type": "B",
        "currency": "SGD",
    },
    "HSBC Global Investment Funds - Global Short Duration Bond ACHSGD": {
        "url": f"{HSBC_DAM}/funds/ilpfund/hgif/global-short-duration-bond/fund-summary/hgif-globalshortduration-fs.pdf",
        "type": "A",
        "currency": "SGD",
    },
    "Schroder Asian Growth Fund Class SGD A Distribution Units": {
        "url": f"{HSBC_DAM}/funds/ilpfund/schroder/asian-grow-fund/fund-summary/schroder-asiangrow-fs.pdf",
        "type": "A",
        "currency": "SGD",
    },
    "Schroder Global Emerging Market Opportunities Fund": {
        "url": f"{HSBC_DAM}/funds/ilpfund/schroder/isf-global-emerging-market-opportunities/fund-summary/schroder-isf-globalemoppo-fs.pdf",
        "type": "A",
        "currency": "SGD",
    },
    "Schroder Singapore Trust SGD A Dis": {
        "url": f"{HSBC_DAM}/funds/ilpfund/schroder/singapore-trust/fund-summary/schroder-sgtrust-fs.pdf",
        "type": "A",
        "currency": "SGD",
    },
    "HSBC Life Singapore Bond Fund": {
        "url": f"{HSBC_DAM}/funds/table-three/hsbc-insurance-singapore-bond-fund.pdf",
        "type": "C",
        "currency": "SGD",
    },
    "HSBC Life Singapore Equity Fund": {
        "url": f"{HSBC_DAM}/funds/table-one/hsbc-insurance-singapore-equity-fund.pdf",
        "type": "C",
        "currency": "SGD",
    },
}

NUM_RE = r"(?:N/A|n/a|-?\d{1,3}(?:,\d{3})*\.\d{1,2}%?|-?\d{1,3}(?:,\d{3})*%?)"


def _clean_num(tok):
    if tok is None:
        return None
    t = tok.strip().rstrip("%")
    if t.upper() == "N/A":
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def fetch_pdf_text(url, timeout=30):
    headers = {"User-Agent": "Mozilla/5.0 (fund-returns-scraper/1.0)"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    with open("_tmp_fetch.pdf", "wb") as f:
        f.write(resp.content)
    pages_text = []
    with pdfplumber.open("_tmp_fetch.pdf") as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            pages_text.append(t)
    return "\n".join(pages_text)


def extract_type_a(text, currency):
    """
    'Performance of the ILP Sub-Fund' section.
    Fund's own row is the first numeric row within the currency block that
    matches the requested currency (SGD)/(USD) tag; benchmark row(s) follow.
    Returns dict {3mo,6mo,1yr,3yr,5yr,10yr} or None.
    """
    idx = text.find("Performance of the ILP Sub-Fund")
    if idx == -1:
        idx = text.find("PERFORMANCE OF THE ILP SUB-FUND")
    if idx == -1:
        return None
    section = text[idx: idx + 4000]

    # Split into lines, find lines that contain >=6 numeric/N-A tokens
    lines = section.split("\n")
    candidate_rows = []
    for line in lines:
        toks = re.findall(NUM_RE, line)
        # need at least 6 numeric-looking tokens on the line to be a data row
        numeric_toks = [t for t in toks if t.upper() == "N/A" or re.match(r"^-?\d", t)]
        if len(numeric_toks) >= 6:
            candidate_rows.append((line, numeric_toks))

    if not candidate_rows:
        return None

    tag = f"({currency})"
    chosen = None
    for line, toks in candidate_rows:
        if tag in line:
            chosen = toks
            break
    if chosen is None:
        # fall back to first candidate row (single-currency doc)
        chosen = candidate_rows[0][1]

    vals = [_clean_num(t) for t in chosen[:7]]
    while len(vals) < 7:
        vals.append(None)
    return {
        "3mo": vals[0], "6mo": vals[1], "1yr": vals[2],
        "3yr": vals[3], "5yr": vals[4], "10yr": vals[5],
    }


def extract_type_b(text, currency):
    """
    'CUMULATIVE & ANNUALISED PERFORMANCE' manager factsheet.
    Periods: 1m 3m 6m YTD 1y 3y 5y S.I. (no 10y).
    """
    idx = text.upper().find("CUMULATIVE")
    if idx == -1 or "PERFORMANCE" not in text[idx: idx + 60].upper():
        idx = text.upper().find("CUMULATIVE & ANNUALISED PERFORMANCE")
    if idx == -1:
        return None
    section = text[idx: idx + 4000]
    lines = section.split("\n")
    candidate_rows = []
    for line in lines:
        toks = re.findall(NUM_RE, line)
        numeric_toks = [t for t in toks if t.upper() == "N/A" or re.match(r"^-?\d", t)]
        if len(numeric_toks) >= 6:
            candidate_rows.append((line, numeric_toks))
    if not candidate_rows:
        return None
    tag = f"({currency})"
    chosen = None
    for line, toks in candidate_rows:
        if tag in line:
            chosen = toks
            break
    if chosen is None:
        chosen = candidate_rows[0][1]
    # order: 1m 3m 6m YTD 1y 3y 5y SI
    vals = [_clean_num(t) for t in chosen[:8]]
    while len(vals) < 8:
        vals.append(None)
    return {
        "3mo": vals[1], "6mo": vals[2], "1yr": vals[4],
        "3yr": vals[5], "5yr": vals[6], "10yr": None,
    }


def extract_type_c(text, currency):
    """
    Old-style 'HSBC Insurance ___ Fund' one-pager (table-one / table-three).
    Two rows: Cumulative Total Returns (7 numbers: 3mo 6mo 1yr 3yr 5yr 10yr SI)
    and Average Annual Compounded Returns (4 numbers: 3yr 5yr 10yr SI).
    We take 3mo/6mo/1yr from the cumulative row and 3yr/5yr/10yr from the
    annualised row.
    """
    idx = text.find("Cumulative Total Returns")
    if idx == -1:
        return None
    section = text[idx: idx + 3000]
    lines = [l for l in section.split("\n") if l.strip()]

    cumulative_row = None
    annualised_row = None
    for line in lines:
        toks = re.findall(NUM_RE, line)
        numeric_toks = [t for t in toks if t.upper() == "N/A" or re.match(r"^-?\d", t)]
        if cumulative_row is None and len(numeric_toks) == 7:
            cumulative_row = numeric_toks
        elif annualised_row is None and len(numeric_toks) == 4 and cumulative_row is not None:
            annualised_row = numeric_toks
        if cumulative_row and annualised_row:
            break

    if not cumulative_row:
        return None

    cvals = [_clean_num(t) for t in cumulative_row[:7]]
    avals = [_clean_num(t) for t in annualised_row[:4]] if annualised_row else [None, None, None, None]

    return {
        "3mo": cvals[0], "6mo": cvals[1], "1yr": cvals[2],
        "3yr": avals[0], "5yr": avals[1], "10yr": avals[2],
    }


EXTRACTORS = {"A": extract_type_a, "B": extract_type_b, "C": extract_type_c}


def scrape_one(fund_name, cfg):
    url = cfg["url"]
    doc_type = cfg["type"]
    currency = cfg.get("currency", "SGD")
    try:
        text = fetch_pdf_text(url)
    except Exception as e:
        return {
            "target_fund_name": fund_name,
            "matched_url": url,
            "match_confidence": 1.0,
            "match_method": "hardcoded_pdf",
            "returns": None,
            "ok": False,
            "error": f"fetch_failed: {e}",
        }

    extractor = EXTRACTORS[doc_type]
    returns = extractor(text, currency)

    if returns is None or all(v is None for v in returns.values()):
        return {
            "target_fund_name": fund_name,
            "matched_url": url,
            "match_confidence": 1.0,
            "match_method": "hardcoded_pdf",
            "returns": None,
            "ok": False,
            "error": f"extraction_failed_type_{doc_type}",
        }

    return {
        "target_fund_name": fund_name,
        "matched_url": url,
        "match_confidence": 1.0,
        "match_method": f"hardcoded_pdf_type_{doc_type}",
        "returns": returns,
        "ok": True,
        "error": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/fund_returns_pdf.json")
    args = ap.parse_args()

    results = []
    for fund_name, cfg in FUND_PDF_MAP.items():
        print(f"Fetching: {fund_name} -> {cfg['url']}")
        r = scrape_one(fund_name, cfg)
        print(f"  ok={r['ok']} returns={r.get('returns')}")
        results.append(r)
        time.sleep(1)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "hsbc_pdf",
        "total": len(results),
        "matched_ok": sum(1 for r in results if r["ok"]),
        "funds": results,
    }

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\nDone: {out['matched_ok']}/{out['total']} matched. Wrote {args.out}")


if __name__ == "__main__":
    main()
