#!/usr/bin/env python3
"""
scrape_ilp_returns.py

Scrapes annualised historical returns (1yr / 3yr / 5yr / 10yr) plus NAV,
ISIN, risk rating and distribution info for the full HSBC fund roster,
straight from HSBC Life Singapore's OWN "ILP Fund Center"
(fundprices.insurance.hsbc.com.sg) -- one fund at a time, by direct URL.

Why this replaces the old FSMOne search-based scraper
-----------------------------------------------------
The FSMOne scraper (scrape_fund_returns.py) had to *search* for each fund
by name and guess which result was right -- a fuzzy match that silently
picked wrong funds when family names overlapped (e.g. BlackRock "World X
Fund" variants). This scraper has NO matching step at all: every fund's
exact ILP Fund Center page id is hard-listed in fund_ids.csv (all 70 were
confirmed by hand against the live pages), so we just visit
    https://fundprices.insurance.hsbc.com.sg/detail?id=<ID>#performance
and read the numbers off that one page. No search, no wrong-match risk.

The page is a client-rendered Angular SPA, so a plain HTTP GET returns an
empty "Loading data..." shell -- that's why this uses Playwright (a real
headless browser) to let the page render before reading its text. The
"Annualised returns" block on that page exposes 1yr / 3yr / 5yr / 10yr
only (no 3mo / 6mo -- those live in a chart that renders as a line graph,
not extractable text), so those two periods are always left null, matching
the shape the merged data/fund_returns.json already uses.

Output schema is a drop-in for data/fund_returns.json (the file index.html
reads): same top-level keys and same per-fund `returns` object, plus an
`extra` block (nav / isin / risk_rating / etc.) that index.html ignores
but is handy to keep.

Usage:
    playwright install --with-deps chromium      # once, before first run
    python scrape_ilp_returns.py --ids fund_ids.csv --out data/fund_returns.json

On any fund whose page won't render after retries, that fund is written
with ok:false and an error note (never dropped), and a screenshot + raw
page text is saved under data/debug_returns/ for inspection.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import random
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

BASE = "https://fundprices.insurance.hsbc.com.sg/detail?id={id}#performance"

# The four annualised-return rows the ILP Fund Center exposes as plain text,
# mapped to the period keys the merged schema uses. 3mo/6mo are never
# available here (chart-only), so they stay null.
PERIOD_ROWS = [
    ("1 year", "1yr"),
    ("3 years", "3yr"),
    ("5 years", "5yr"),
    ("10 years", "10yr"),
]

# Matches a percentage number, positive or negative, e.g. "25.27%", "-4.70%",
# "+12.15%". The site uses a Unicode minus (−) in places, so allow both.
NUM_AFTER = r"[+\-−]?\d+(?:\.\d+)?"


def num_or_none(s):
    if s is None:
        return None
    s = s.strip().replace("−", "-").rstrip("%").strip()
    if s in ("", "-", "No value"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_block(text):
    """Pull the four annualised-return figures out of the rendered page text.

    The block renders as:
        Annualised returns
        1 year
        25.27%
        3 years
        14.18%
        5 years
        4.20%
        10 years
        4.22%
        Figures shown are annualized ...
    A period shown as "- No value" (fund younger than that period) is read
    as null.
    """
    idx = text.find("Annualised returns")
    window = text[idx: idx + 800] if idx != -1 else text
    out = {"3mo": None, "6mo": None, "1yr": None, "3yr": None, "5yr": None, "10yr": None}
    for label, key in PERIOD_ROWS:
        # label, then (optionally) a newline, then either a percentage or "-\nNo value"
        m = re.search(re.escape(label) + r"\s*\n\s*(" + NUM_AFTER + r"%?|-\s*\n?\s*No value)", window)
        if m:
            out[key] = num_or_none(m.group(1))
    return out


def first(pattern, text, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def extract_extra(text):
    """Best-effort pull of the descriptive fields around the page. All are
    optional -- a missing one is just left out / null, never fatal."""
    extra = {}

    # NAV (as of 14 Aug 2026) : SGD 1.015
    m = re.search(r"NAV \(as of ([^)]+)\)\s*:\s*([A-Z]{3})\s*([\d,]+\.?\d*)", text)
    if m:
        extra["nav"] = num_or_none(m.group(3).replace(",", ""))
        extra["nav_currency"] = m.group(2)
        raw_date = m.group(1).strip()
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                extra["nav_as_of"] = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        else:
            extra["nav_as_of"] = raw_date

    # Product risk rating  \n  3 - Balanced
    extra["risk_rating"] = first(r"Product risk rating\s*\n\s*(\d\s*-\s*[A-Za-z ]+)", text)

    # ISIN \n SG9999009757   (or "- No value")
    isin = first(r"\nISIN\s*\n\s*([A-Z0-9]{12}|-\s*\n?\s*No value)", text)
    extra["isin"] = None if (isin is None or isin.startswith("-")) else isin

    # Asset class + share class type appear as bare lines near the header,
    # e.g. "Equity ... Distributing". Pull the recognised tokens if present.
    for token in ("Equity", "Fixed Income", "Allocation", "Money Market", "Alternative"):
        if re.search(r"\n" + re.escape(token) + r"\n", text):
            extra["asset_class"] = token
            break
    for token in ("Accumulating", "Distributing"):
        if re.search(r"\n" + re.escape(token) + r"\n", text):
            extra["share_class_type"] = token
            break

    # Distribution info (only for distributing funds)
    extra["distribution_frequency"] = first(r"Distribution frequency\s*\n\s*([A-Za-z\-]+)", text)
    extra["twelve_month_yield"] = first(r"12-month yield\s*\n\s*([\d.]+%)", text)

    # Drop empty keys to keep the file tidy
    return {k: v for k, v in extra.items() if v is not None}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def scrape_one(page, fund_id, debug_dir, n, key):
    url = BASE.format(id=fund_id)
    page.goto(url, wait_until="networkidle", timeout=45000)
    # The SPA swaps "Loading data..." for the real content once its XHRs
    # resolve. Wait for the returns heading to actually appear, retrying a
    # few times, before reading the text.
    text = ""
    for attempt in range(4):
        try:
            page.wait_for_selector("text=Annualised returns", timeout=8000)
        except Exception:
            pass
        text = page.inner_text("body")
        if "Annualised returns" in text and "Loading data..." not in text[:200]:
            break
        page.wait_for_timeout(1500 + attempt * 1000)

    if "Annualised returns" not in text:
        # Save debug and signal failure
        os.makedirs(debug_dir, exist_ok=True)
        try:
            page.screenshot(path=f"{debug_dir}/{n:03d}_{key}.png", full_page=True)
        except Exception:
            pass
        try:
            with open(f"{debug_dir}/{n:03d}_{key}.txt", "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        return None

    returns = extract_block(text)
    extra = extract_extra(text)
    return {"returns": returns, "extra": extra, "url": url}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", default="fund_ids.csv",
                    help="CSV with columns: fund_id, fund_name, fund_manager")
    ap.add_argument("--out", default="data/fund_returns.json")
    ap.add_argument("--debug-dir", default="data/debug_returns")
    ap.add_argument("--limit", type=int, default=None, help="Only process first N (testing)")
    args = ap.parse_args()

    with open(args.ids, newline="", encoding="utf-8") as f:
        targets = list(csv.DictReader(f))
    if args.limit:
        targets = targets[: args.limit]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    results = []
    n_ok = 0
    n_fail = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})

        for i, t in enumerate(targets):
            fund_id = t["fund_id"].strip()
            name = t["fund_name"].strip()
            manager = t.get("fund_manager", "").strip()
            key = slugify(name)
            print(f"[{i+1}/{len(targets)}] {name} ({fund_id})")

            entry = {
                "target_fund_name": name,
                "target_manager": manager,
                "matched_url": BASE.format(id=fund_id).replace("#performance", ""),
                "match_confidence": 1.0,          # exact id, no fuzzy matching
                "match_method": "ilp_center_direct_id",
                "returns": {"3mo": None, "6mo": None, "1yr": None,
                            "3yr": None, "5yr": None, "10yr": None},
                "ok": False,
                "source": "ilp_center",
            }

            try:
                scraped = scrape_one(page, fund_id, args.debug_dir, i, key)
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                scraped = None

            if scraped is None:
                n_fail += 1
                entry["extra"] = {"error": "page did not render / returns block not found"}
                print("  FAILED (no data)")
            else:
                entry["returns"] = scraped["returns"]
                entry["extra"] = scraped["extra"]
                have = sum(1 for v in scraped["returns"].values() if v is not None)
                entry["ok"] = have >= 1
                if entry["ok"]:
                    n_ok += 1
                    print(f"  OK: {scraped['returns']}")
                else:
                    n_fail += 1
                    print("  FAILED (block found but no numbers)")

            results.append(entry)
            time.sleep(random.uniform(0.8, 1.8))   # politeness delay

        browser.close()

    output = {
        "last_scraped": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "hsbc_ilp_center",
        "note": ("Scraped fund-by-fund from HSBC Life Singapore's ILP Fund Center "
                 "(fundprices.insurance.hsbc.com.sg) by exact fund id. The "
                 "'Annualised returns' block exposes 1yr/3yr/5yr/10yr only, so "
                 "3mo/6mo are always null."),
        "periods": ["3mo", "6mo", "1yr", "3yr", "5yr", "10yr"],
        "total_targets": len(results),
        "matched_ok": n_ok,
        "failed": n_fail,
        "funds": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nDone: {n_ok}/{len(results)} scraped OK, {n_fail} failed. Wrote {args.out}")
    if n_fail:
        print(f"Debug artifacts for failures under {args.debug_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
