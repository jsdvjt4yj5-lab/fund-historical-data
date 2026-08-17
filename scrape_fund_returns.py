#!/usr/bin/env python3
"""
Scrapes annualised historical returns (3mo/6mo/1yr/3yr/5yr/10yr) for
specific funds from their FSMOne (fsm.global) factsheet pages.

FSMOne's factsheet pages are a client-rendered JS app -- there is no
server-rendered HTML and no discoverable JSON API (confirmed by testing
several guessed endpoint patterns, which all just return the app's empty
shell). So this uses Playwright (a real headless browser) to load each
page, let it render, and read the "Annualised Returns" bar-chart section
as visible text.

Starting scope: two funds with DIRECT, KNOWN FSMOne URLs -- PIMCO Income
Fund and Franklin Income Fund, the two funds shown on the app's Income
calculator screen. This intentionally skips FSMOne's own search UI for
now; extending to the full ~70-fund roster in targets.csv would mean
automating that search box per fund, which is a separate, riskier
follow-up once this narrower version is confirmed working end-to-end in
a real GitHub Actions run (this could not be tested locally -- there is
no way to run a real browser in the environment this was written in).

Usage:
    playwright install --with-deps chromium   # once, before first run
    python scrape_fund_returns.py --out data/fund_returns.json

Output: data/fund_returns.json
On any fund not fully parsed, also writes data/debug_returns/<key>.png
(full-page screenshot) and data/debug_returns/<key>.txt (raw extracted
page text) so the actual rendered page can be inspected afterwards.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

# Direct factsheet URLs for the two funds shown on the Income calculator
# screen. No search step needed -- these were found and confirmed by hand
# (the PIMCO one was confirmed against a real screenshot of the rendered
# page's "Annualised Returns" chart).
FUNDS = [
    {
        "key": "pimco",
        "app_label": "PIMCO Income Fund",
        "fsmone_name": "PIMCO Income Fund Cl E Inc SGD-H",
        "url": "https://secure.fundsupermart.com/fsm/funds/factsheet/ALZP06/PIMCO-Income-Fund-Cl-E-Inc-SGD-H",
    },
    {
        "key": "franklin",
        "app_label": "Franklin Income Fund",
        "fsmone_name": "FTIF - Franklin Income A MDIS SGD-H1",
        "url": "https://secure.fundsupermart.com/fsm/funds/factsheet/FTF020/FTIF-Franklin-Income-A-MDIS-SGD-H1",
    },
]

# The periods shown on FSMOne's "Annualised Returns" chart, left-to-right,
# confirmed via a real screenshot of the PIMCO page. "YTD" and "1 WK"/"2 YR"
# are captured (helps the fallback zip strategy line things up) but only
# the periods actually asked for are kept in the final output.
PERIOD_LABELS = ["1 WK", "1 MTH", "3 MTH", "6 MTH", "YTD", "1 YR", "2 YR", "3 YR", "5 YR", "10 YR"]
KEEP_PERIODS = {"3 MTH": "3mo", "6 MTH": "6mo", "1 YR": "1yr", "3 YR": "3yr", "5 YR": "5yr", "10 YR": "10yr"}

PCT_RE = re.compile(r"[-−]?\d+\.\d+%")


def extract_returns(page_text: str) -> dict:
    """Parse the 'Annualised Returns' section out of the page's full
    visible text. Tries two strategies since the exact DOM order of an
    SVG/canvas chart's label-vs-value text nodes can't be confirmed
    without live browser devtools access:
      1. Each label immediately followed by its %, e.g. "3 MTH\\n0.95%".
      2. Fallback: collect all period labels present, in order, and all
         percentages present, in order, and zip them 1:1 -- works if the
         chart's labels and values are each grouped together in the same
         left-to-right order, even if separated in the underlying text.
    Returns {} if neither strategy finds a usable result -- the caller
    treats that as a failure and keeps debug artifacts for inspection.
    """
    idx = page_text.find("Annualised Returns")
    window = page_text[idx: idx + 3000] if idx != -1 else page_text
    idx2 = window.find("Calendar Year Returns")
    if idx2 != -1:
        window = window[:idx2]

    result = {}
    for label in PERIOD_LABELS:
        m = re.search(re.escape(label) + r"\s*\n?\s*(" + PCT_RE.pattern + r")", window)
        if m:
            result[label] = m.group(1)
    if len(result) >= 5:
        return result

    labels_found = [lbl for lbl in PERIOD_LABELS if lbl in window]
    values_found = PCT_RE.findall(window)
    if len(labels_found) == len(values_found) and labels_found:
        return dict(zip(labels_found, values_found))

    return {}


def scrape_fund(page, fund: dict) -> dict:
    page.goto(fund["url"], wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(2000)  # let the chart finish drawing after network idle
    try:
        page.wait_for_selector("text=Annualised Returns", timeout=15000)
    except Exception:
        pass  # fall through -- extract_returns() will just find nothing

    page_text = page.inner_text("body")
    raw = extract_returns(page_text)

    returns = {key: raw.get(label) for label, key in KEEP_PERIODS.items()}

    return {
        "app_label": fund["app_label"],
        "fsmone_name": fund["fsmone_name"],
        "source_url": fund["url"],
        "returns": returns,
        "raw_periods_found": raw,
        "ok": sum(1 for v in returns.values() if v is not None) >= 4,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/fund_returns.json")
    ap.add_argument("--debug-dir", default="data/debug_returns")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    results = []
    any_failed = False

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        for fund in FUNDS:
            print(f"Scraping {fund['app_label']} ({fund['url']}) ...")
            try:
                data = scrape_fund(page, fund)
            except Exception as e:
                print(f"  FAILED: {e}", file=sys.stderr)
                data = {
                    "app_label": fund["app_label"],
                    "fsmone_name": fund["fsmone_name"],
                    "source_url": fund["url"],
                    "returns": {k: None for k in KEEP_PERIODS.values()},
                    "raw_periods_found": {},
                    "ok": False,
                    "error": str(e),
                }
            if not data["ok"]:
                any_failed = True
                os.makedirs(args.debug_dir, exist_ok=True)
                safe_key = fund["key"]
                try:
                    page.screenshot(path=f"{args.debug_dir}/{safe_key}.png", full_page=True)
                except Exception:
                    pass
                try:
                    with open(f"{args.debug_dir}/{safe_key}.txt", "w", encoding="utf-8") as f:
                        f.write(page.inner_text("body"))
                except Exception:
                    pass
                found_n = sum(1 for v in data["returns"].values() if v)
                print(f"  Only found {found_n} of {len(KEEP_PERIODS)} periods -- "
                      f"debug artifacts saved to {args.debug_dir}/{safe_key}.*")
            else:
                print(f"  OK: {data['returns']}")
            results.append(data)
        browser.close()

    output = {
        "last_scraped": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "periods": list(KEEP_PERIODS.values()),
        "funds": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote {args.out}")
    if any_failed:
        print("One or more funds had incomplete data -- check debug artifacts.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
