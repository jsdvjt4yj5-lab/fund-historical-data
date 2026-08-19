# fund-historical-data

Feeds the Fund Tracker screen (and the growth/income portfolio builders) in
the HSBC Life presentation app. A scheduled GitHub Action scrapes fund
NAV/returns and commits the result as `data/fund_returns.json`; the app
fetches that file straight from GitHub's raw content CDN, so there's no
backend to host.

## What's live right now

**`scrape_ilp_returns.py`** + **`.github/workflows/scrape_ilp_returns.yml`**
is the active pipeline. It visits each fund's own page on HSBC Life's ILP
Fund Center (`fundprices.insurance.hsbc.com.sg`) by its exact id — no
search, no fuzzy matching — and reads off both the annualised returns
(1yr/3yr/5yr/10yr; the site doesn't expose 3mo/6mo) and the fund's current
NAV, ISIN, risk rating, and 12-month yield. Writes everything straight to
`data/fund_returns.json` and commits it.

- **Fund list:** `fund_ids.csv` (70 funds, each with its fixed ILP Fund
  Center id)
- **Schedule:** every 2 days at 6am SGT — see `scrape_ilp_returns.yml`'s
  cron comment for why it's not literally "every 2 days" every time
- **Manual run / smoke test:** repo → **Actions** → *Scrape ILP fund
  returns* → **Run workflow** (optionally pass a `limit` for a quick
  partial test)
- **Setup / deployment notes:** see `ILP_SCRAPER_SETUP.md`

## Retired pipeline (scripts still present, not wired to any workflow)

`scrape_pdf_returns.py`, `scrape_fund_returns.py`, and `merge_returns.py`
were the original approach: a hardcoded map of official PDF factsheets for
funds with a confirmed URL, falling back to a Playwright-driven search of
FSMOne's site for everything else, then merged into the same
`data/fund_returns.json` output shape (distinguishable by its `source`,
`match_confidence`, and `raw_periods_found` fields — the ILP scraper's
output doesn't have those). Its workflow (`scrape_returns.yml`) has been
removed from `.github/workflows/`, but the three scripts and `targets.csv`
are still sitting in the repo root. Safe to delete once you're confident
the ILP pipeline is reliably keeping `data/fund_returns.json` fresh; until
then they're harmless (nothing schedules them anymore).

**If `data/fund_returns.json` looks like it's still on this schema** (no
`extra.nav` on any fund, `source: "fsmone"`/`"pdf"` present) rather than the
ILP scraper's, it likely just means the ILP workflow hasn't fired yet since
it was added — trigger it manually from the Actions tab to confirm and
refresh immediately rather than waiting on the schedule.

## Files

| Path | What it is |
|---|---|
| `scrape_ilp_returns.py` | Active scraper — ILP Fund Center, by exact id |
| `fund_ids.csv` | Active scraper's fund list + ILP Fund Center ids |
| `.github/workflows/scrape_ilp_returns.yml` | Active scraper's schedule |
| `ILP_SCRAPER_SETUP.md` | Deployment/setup notes for the active scraper |
| `data/fund_returns.json` | Output the app actually fetches |
| `requirements.txt` | Python deps for both pipelines (`playwright`, `pdfplumber`, `beautifulsoup4`, `requests`) |
| `scrape_pdf_returns.py`, `scrape_fund_returns.py`, `merge_returns.py`, `targets.csv` | Retired pipeline — see above |

## Deploying a change

There's no CI/CD wired up to push code here automatically — updating any
of these files means downloading the updated version and re-uploading it
through the repo's **Add file → Upload files** in the GitHub web UI (same
file path each time, e.g. `scrape_ilp_returns.py` at the repo root, or
`scrape_ilp_returns.yml` under `.github/workflows/`), then committing.
