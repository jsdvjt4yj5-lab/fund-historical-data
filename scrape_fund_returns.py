#!/usr/bin/env python3
"""
Scrapes annualised historical returns (3mo/6mo/1yr/3yr/5yr/10yr) for the
full HSBC fund roster (targets.csv) from each fund's FSMOne (fsm.global)
factsheet page.

FSMOne's site is a client-rendered JS app with no server-rendered HTML and
no discoverable JSON API (confirmed by testing several guessed endpoint
patterns -- search included -- which all just return the app's empty
shell, even with query-string search terms attached). So this uses
Playwright (a real headless browser) for two things per fund:

  1. SEARCH -- drive FSMOne's own search box with the fund's name, since
     there's no way to jump straight to a fund's page from its name
     otherwise. This is the riskiest part of this script: it depends on
     guessing reasonable selectors for FSMOne's search input and results,
     which could not be verified against the live page beforehand (no
     working browser in the environment this was written in). Falls back
     through a few different selector/interaction strategies before
     giving up on a given fund.
  2. SCRAPE -- once on a fund's factsheet page, read the "Annualised
     Returns" chart as visible text (this part IS verified -- see
     extract_returns(), tested against a real screenshot of PIMCO Income
     Fund's page and confirmed working end-to-end in production for that
     fund plus Franklin Income Fund, both scraped via known direct URLs
     before this search step was added).

v1 of this script (kept working, still used as the model for step 2) only
covered PIMCO Income Fund and Franklin Income Fund via hardcoded direct
URLs -- no search needed. This version extends that to the full roster.

Usage:
    playwright install --with-deps chromium   # once, before first run
    python scrape_fund_returns.py --targets targets.csv --out data/fund_returns.json

Output: data/fund_returns.json -- one entry per target fund, each with
either full returns data, or ok:false plus a `stage` field ("search" or
"scrape") saying which step failed.

On any fund that fails, also writes:
  data/debug_returns/<n>_<key>.png  -- full-page screenshot at the point of failure
  data/debug_returns/<n>_<key>.txt  -- raw extracted page text at that point
so the actual rendered page can be inspected afterwards.
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
from difflib import SequenceMatcher

from playwright.sync_api import sync_playwright

FSM_HOME = "https://secure.fundsupermart.com/fsm/"

# The periods shown on FSMOne's "Annualised Returns" chart, left-to-right,
# confirmed via a real screenshot of the PIMCO Income Fund page.
PERIOD_LABELS = ["1 WK", "1 MTH", "3 MTH", "6 MTH", "YTD", "1 YR", "2 YR", "3 YR", "5 YR", "10 YR"]
KEEP_PERIODS = {"3 MTH": "3mo", "6 MTH": "6mo", "1 YR": "1yr", "3 YR": "3yr", "5 YR": "5yr", "10 YR": "10yr"}

PCT_RE = re.compile(r"[-−]?\d+\.\d+%")
FACTSHEET_HREF_RE = re.compile(r"/funds/factsheet/")

SEARCH_MATCH_THRESHOLD = 0.5   # generous -- FSMOne's own naming style differs
                                # enough from the HSBC target list (abbreviations,
                                # share class suffixes) that a strict threshold
                                # would reject a lot of genuine matches.

# Pixel position of the magnifying-glass search icon in FSM Global's sticky
# header, at the 1400x1000 viewport this script launches with. Read off a
# real debug screenshot from the first full-roster run (see
# search_icon_zoom.png) -- there is no visible <input> on the homepage at
# all, just this icon, which is why every "guess a search input selector"
# strategy failed 100% of the time on the first run. Clicking here reveals
# the actual input.
SEARCH_ICON_XY = (1148, 123)


def normalize(name: str) -> str:
    name = name.upper()
    name = re.sub(r"[-–,]", " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def extract_returns(page_text: str) -> dict:
    """Same logic verified in v1 against a real screenshot -- see module
    docstring. Two strategies to handle either possible DOM text order for
    an SVG/canvas chart's labels vs. values."""
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


def collect_factsheet_links(page) -> list[dict]:
    """Grab every visible link on the current page pointing at a factsheet,
    with its link text -- used both for a search-results page and for a
    dropdown/autocomplete overlay, since both would just be <a> elements
    somewhere in the DOM regardless of visual presentation."""
    try:
        return page.eval_on_selector_all(
            "a[href*='/funds/factsheet/']",
            "els => els.map(e => ({href: e.href, text: (e.innerText || e.textContent || '').trim()}))",
        )
    except Exception:
        return []


def dismiss_cookie_banner(page) -> None:
    """FSM Global shows a 'We use cookies... CLOSE / FIND OUT MORE' banner
    on first load of a fresh (cookie-less) browser context -- confirmed by
    finding that exact text in a debug page-text dump. It visually dims the
    *entire* page (not just the banner corner), which means it's sitting
    behind a page-wide backdrop that swallows clicks anywhere on the page,
    including the header search icon far away from the banner itself. This
    is why the very first coordinate-click attempt at the search icon did
    nothing on every single fund in that run. Must be dismissed before any
    other interaction. Safe no-op if the banner isn't present."""
    try:
        close_btn = page.get_by_text("CLOSE", exact=True).first
        if close_btn.count() > 0:
            close_btn.click(timeout=3000)
            page.wait_for_timeout(500)
    except Exception:
        pass


def find_fund_url(page, fund_name: str) -> tuple[str | None, float, str]:
    """Search FSMOne for a fund by name and return (url, match_score, method).

    Rewritten after inspecting real debug screenshots from the first
    full-roster run. Two things learned from those screenshots that changed
    this function:

      1. The `general-search?q=...` query-string "shortcut" used in v1 of
         this function was a bug, not a working strategy: FSM Global is a
         pure client-rendered SPA that does NOT read that query param at
         all, so it silently rendered the plain homepage every time,
         regardless of the fund searched for. collect_factsheet_links()
         then picked up whatever fixed "Recommended / Trending" fund links
         happened to be on that homepage, and the fuzzy matcher occasionally
         scored one of those unrelated funds above threshold by coincidence
         -- producing confident-looking but WRONG matches (e.g. five
         different BlackRock sector funds all "matching" the same BlackRock
         Gold fund page). This shortcut is removed entirely.
      2. There is no visible <input> on the homepage at all -- only a
         magnifying-glass icon in the header (no surrounding <form>/label
         text visible in the rendered page, which is why every guessed
         input selector found nothing). It has to be clicked first to
         reveal the real search input. Its on-screen position is stable
         (same fixed header on every load) at SEARCH_ICON_XY for the
         1400x1000 viewport this script launches with.

    Returns (None, 0.0, reason) if nothing usable was found, so the caller
    can save debug output explaining why.
    """
    target_norm = normalize(fund_name)

    def best_of(links):
        best, best_score = None, 0.0
        for l in links:
            score = SequenceMatcher(None, target_norm, normalize(l["text"])).ratio()
            if score > best_score:
                best, best_score = l, score
        return best, best_score

    try:
        page.goto(FSM_HOME, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
    except Exception:
        return None, 0.0, "homepage_load_failed"

    dismiss_cookie_banner(page)

    # Open the search input: click the magnifying-glass icon by its known
    # fixed position, then confirm an input actually appeared rather than
    # assuming the click worked. Try a couple of nearby coordinates too, in
    # case the pixel estimate is slightly off, now that the cookie banner's
    # page-wide backdrop (confirmed via a debug page-text dump to have been
    # swallowing every click on the previous run) is out of the way.
    icon_x, icon_y = SEARCH_ICON_XY
    for dx, dy in ((0, 0), (0, -6), (0, 6), (-6, 0), (6, 0)):
        try:
            page.mouse.click(icon_x + dx, icon_y + dy)
            page.wait_for_timeout(500)
        except Exception:
            continue
        if page.locator("input").count() > 0:
            break

    search_box = None
    # Prefer a purpose-named input if one shows up now that the icon's been
    # clicked; otherwise fall back to "whatever visible text input just
    # appeared", since the exact markup couldn't be inspected beforehand.
    named_selectors = [
        "input[type='search']",
        "input[placeholder*='search' i]",
        "input[aria-label*='search' i]",
        "input[name*='search' i]",
    ]
    for sel in named_selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                search_box = loc
                break
        except Exception:
            continue

    if search_box is None:
        try:
            candidates = page.locator("input[type='text'], input:not([type])")
            for idx in range(min(candidates.count(), 8)):
                cand = candidates.nth(idx)
                if cand.is_visible():
                    search_box = cand
                    break
        except Exception:
            pass

    if search_box is None:
        return None, 0.0, "no_search_box_found"

    try:
        search_box.click(timeout=5000)
        search_box.fill(fund_name)
        page.wait_for_timeout(2000)
    except Exception:
        return None, 0.0, "search_box_interaction_failed"

    links = collect_factsheet_links(page)
    if links:
        best, score = best_of(links)
        if best and score >= SEARCH_MATCH_THRESHOLD:
            return best["href"], score, "dropdown"

    try:
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
    except Exception:
        pass

    links = collect_factsheet_links(page)
    if links:
        best, score = best_of(links)
        if best and score >= SEARCH_MATCH_THRESHOLD:
            return best["href"], score, "results_page"
        if best:
            return best["href"], score, "results_page_low_confidence"

    return None, 0.0, "no_links_found"


def scrape_returns_page(page, url: str) -> dict:
    page.goto(url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(2000)
    try:
        page.wait_for_selector("text=Annualised Returns", timeout=15000)
    except Exception:
        pass
    page_text = page.inner_text("body")
    raw = extract_returns(page_text)
    returns = {key: raw.get(label) for label, key in KEEP_PERIODS.items()}
    ok = sum(1 for v in returns.values() if v is not None) >= 4
    return {"returns": returns, "raw_periods_found": raw, "ok": ok, "page_text": page_text}


def save_debug(debug_dir, n, key, page):
    os.makedirs(debug_dir, exist_ok=True)
    try:
        page.screenshot(path=f"{debug_dir}/{n:03d}_{key}.png", full_page=True)
    except Exception:
        pass
    try:
        with open(f"{debug_dir}/{n:03d}_{key}.txt", "w", encoding="utf-8") as f:
            f.write(page.inner_text("body"))
    except Exception:
        pass


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--targets", required=True, help="CSV of target funds (fund_name, fund_manager)")
    ap.add_argument("--out", default="data/fund_returns.json")
    ap.add_argument("--debug-dir", default="data/debug_returns")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N funds (for testing)")
    args = ap.parse_args()

    with open(args.targets, newline="", encoding="utf-8") as f:
        targets = list(csv.DictReader(f))
    if args.limit:
        targets = targets[: args.limit]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    results = []
    n_ok = 0
    n_search_fail = 0
    n_scrape_fail = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})

        for i, t in enumerate(targets):
            fund_name = t["fund_name"]
            key = slugify(fund_name)
            print(f"[{i+1}/{len(targets)}] {fund_name}")

            entry = {
                "target_fund_name": fund_name,
                "target_manager": t.get("fund_manager", ""),
                "matched_url": None,
                "match_confidence": None,
                "match_method": None,
                "returns": {v: None for v in KEEP_PERIODS.values()},
                "ok": False,
            }

            try:
                url, score, method = find_fund_url(page, fund_name)
            except Exception as e:
                print(f"  search error: {e}", file=sys.stderr)
                url, score, method = None, 0.0, f"exception:{e}"

            entry["match_confidence"] = round(score, 3)
            entry["match_method"] = method

            if not url:
                n_search_fail += 1
                print(f"  SEARCH FAILED ({method})")
                save_debug(args.debug_dir, i, key + "_search", page)
                results.append(entry)
                continue

            entry["matched_url"] = url
            try:
                scraped = scrape_returns_page(page, url)
            except Exception as e:
                print(f"  scrape error: {e}", file=sys.stderr)
                n_scrape_fail += 1
                save_debug(args.debug_dir, i, key + "_scrape", page)
                results.append(entry)
                continue

            entry["returns"] = scraped["returns"]
            entry["raw_periods_found"] = scraped["raw_periods_found"]
            entry["ok"] = scraped["ok"]

            if scraped["ok"]:
                n_ok += 1
                print(f"  OK ({method}, confidence {score:.2f}): {scraped['returns']}")
            else:
                n_scrape_fail += 1
                print(f"  SCRAPE INCOMPLETE ({method}, confidence {score:.2f})")
                save_debug(args.debug_dir, i, key + "_scrape", page)

            results.append(entry)

            # Politeness delay between funds -- avoid hammering FSMOne with
            # back-to-back requests, which is both more considerate and
            # less likely to trip any bot-rate-limiting.
            time.sleep(random.uniform(1.0, 2.2))

        browser.close()

    output = {
        "last_scraped": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "periods": list(KEEP_PERIODS.values()),
        "total_targets": len(targets),
        "matched_ok": n_ok,
        "search_failed": n_search_fail,
        "scrape_incomplete": n_scrape_fail,
        "funds": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nDone: {n_ok}/{len(targets)} fully scraped, {n_search_fail} search failures, "
          f"{n_scrape_fail} scrape-incomplete.")
    print(f"Wrote {args.out}")
    if n_search_fail + n_scrape_fail > 0:
        print(f"Debug artifacts for failures saved under {args.debug_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
