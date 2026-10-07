# Using the Workbench

A practical guide: how to start it, how to research a name, and what each part of the dashboard is for.

There are two halves. The **pipeline** (`cli.py`) does the work: it fetches data, calls Claude, and writes results into `data/`. The **dashboard** (`dashboard.py`) only reads `data/` and lays it out. The dashboard never triggers a run, so if a name is missing or stale, run the pipeline and refresh the page.

---

## 1. First-time setup

```bash
git clone https://github.com/danieldrewgold/investment-workbench.git
cd investment-workbench
pip install -r requirements.txt
playwright install chromium        # only needed for JS-heavy investor-relations sites
cp .env.example .env               # then open .env and paste in your keys
```

Only `ANTHROPIC_API_KEY` is required. Put a real name and email in `SEC_USER_AGENT`, because EDGAR throttles anonymous requests. Every other key is optional, and its step is skipped when the key is missing.

## 2. Start the dashboard

```bash
python dashboard.py
```

It opens http://127.0.0.1:8765 in your browser. On Windows you can double-click `start_dashboard.bat` instead. Useful flags:

- `--no-open` starts the server without opening a browser tab.
- `--port 8800` uses a different port.

It runs only while that terminal window is open. Press Ctrl+C to stop it. The server listens on localhost only, so nobody else on the network can reach it.

A fresh clone has no data, so the dashboard starts empty. Run a ticker first (step 3).

## 3. Research a ticker

```bash
python -u cli.py research WDC --verbose
```

- **Cost and time:** a cold run on a new name takes about 10–15 minutes and roughly $5–8 of Claude API usage. A re-run on a cached name is fast and nearly free, because each fetch step caches under `data/dag_cache/<TICKER>/`.
- **`--verbose`** prints each step as it runs. Without it a long run looks frozen.
- **`--force`** continues even when the brief comes back "thin" (rich prose but few modelable drivers). Medical devices and other unusual business models often need it.
- **One at a time:** run names one after another, not in parallel. Several cold runs at once can exhaust memory (the investor-deck vision step is heavy) and die silently.
- **Output:** results land in `data/results/`, with a Word report and an Excel workbook in `data/reports/` and `data/exports/`. All three appear in the dashboard.

Other commands:

| Command | What it does |
|---|---|
| `python cli.py scan WDC,STX,MU` | Run several names back to back |
| `python cli.py dag WDC --verbose` | Run only the fetch + analysis steps, with no brief or model |
| `python cli.py dag WDC --force` | Same, but ignore the cache and re-fetch everything |
| `python cli.py refresh-13f` | Pull the last 4 quarters of 13F filings for the tracked fund universe. Feeds Ownership and crowding; run it quarterly |
| `python cli.py research WDC --format word` | Write only the Word report (`excel` and `both` are the other options) |

## 4. Keep the dashboard current

A research run doesn't refresh everything the overview page shows. These scripts fill in or update individual panels without re-running the whole pipeline. Pass the tickers you want refreshed as arguments.

**The quick way to freshen everything without new analysis** (no Claude calls, so free):

```bash
python refresh_data.py --light        # estimates, market data, press, layoffs, credit: ~30s per name
python refresh_data.py                # also ownership, insiders, comps, quarterly history: ~5 min per name
python refresh_price_history.py       # price charts for every name
python refresh_valuation.py           # market cap / EV / multiples for every name
python refresh_research.py            # new newsletter emails from your inbox (Claude: ~2 cents per email + ~15 cents per name summarized)
```

`refresh_data.py` checks each refreshed panel against what was there before and keeps the old version if the new pull came back empty or thinner, so it can't make the dashboard worse. It never touches transcripts or the research briefs. To publish the refreshed dashboard, see section 7.


| Script | Refreshes | Cost |
|---|---|---|
| `python refresh_price_history.py WDC` | The interactive price chart (1D through MAX) | free |
| `python refresh_valuation.py WDC` | Market cap / EV strip, headline multiple, average price target | free |
| `python rerun_news.py WDC` | News feed | free |
| `python rerun_press.py WDC` | Press tab: SEC 8-K earnings releases | free |
| `python rerun_ir_press.py WDC` | Press tab: links from the company's own IR / newsroom site | free |
| `python rerun_qfin.py WDC` | Quarterly history behind the estimates matrix | free, but Polygon's free tier is slow |
| `python rerun_ownership.py WDC` | Ownership tab: all-holders breakdown | free |
| `python rerun_decks.py` | Decks tab: investor presentations | uses Claude (vision) |
| `python refresh_warn.py` | State WARN layoff notices (run weekly) | free |

If a new name's price chart or valuation strip is blank, run the first two.

## 5. Finding your way around the dashboard

**Top bar.** Press `/` anywhere to jump to the search box, then type a ticker or a function name. The ticker tape shows live quotes during market hours.

**Left sidebar.** Views (Screener, Functions, Compare, Research, Macro), then every ticker you have run, grouped by theme. Use the filter box to narrow the list.

**Screener (home).** One row per ticker: edge score, action, upside, model EPS vs. consensus EPS, the decision-gate verdict, and the date of the latest run. Click a column header to sort. A news feed across all your names sits above it.

**Company page** (`/co/WDC`). The tabs across the top:

| Tab | What's there |
|---|---|
| Overview | Price chart, market cap / EV, the research brief, valuation, peer comps, insiders, and any restructuring alert. Each panel's corner link names the module that produced it |
| Estimates | Consensus revenue and EPS by quarter and year. Click a cell for the high/low range, revisions, and the model's number |
| Ownership | 13F holders, 13D/13G stakes, holder-type pies, float breakdown, crowding |
| Transcripts | Earnings calls split into prepared remarks and Q&A. **Mostly empty now** that the transcript subscription has lapsed |
| Press | Earnings releases and company news by quarter, linked to SEC or the company's IR site |
| Decks | Investor presentations with the vision-model takeaways |
| Research | Newsletter and Substack pieces from your inbox that mention the name (needs the Gmail setup in `refresh_research.py`) |
| Search | Keyword search across everything collected on the name. **Ask AI** answers a question from that material with citations (one Claude call, a few seconds) |

The **Files** row near the top of the Overview tab downloads the Word report and Excel workbook. **Full result JSON** at the bottom shows the raw pipeline output.

**Functions** (`/fn`). Every pipeline step, with how many tickers it has produced output for. Click one to see its output for every name side by side. This is the fastest way to spot a broken data source: one step showing nothing, or one ticker's numbers looking off against the rest.

**Compare.** Put names side by side: click tickers on the Compare page, or build the URL yourself (`/compare?t=WDC&t=STX`).

**Research** (`/research`). The inbox research feed across all names, weighted by author tier.

**Macro** (`/macro`). FRED rates, inflation, labor and credit series with hover charts, prediction-market odds, and an AI summary.

## 6. When something looks wrong

- **The page won't load.** The server stopped; start it again (step 2). A crashed page shows a Python traceback instead of a blank screen, which is the thing to paste into Claude.
- **A run seems stuck.** Use `-u --verbose` so output isn't buffered. Long silences are usually Polygon's free-tier rate limit (about 5 requests a minute).
- **A number looks off** (a multiple or EPS growth rate far out of line): check for unit, currency, or stock-split artifacts. Foreign ADRs are converted to USD, and a split can make one period look wildly cheap. The comps panels footnote the adjustments they have made.
- **Transcripts are blank.** That's expected without `ECALL_API_KEY`.

## 7. Update the public demo

The demo at https://danieldrewgold.github.io/investment-workbench/ is a frozen export. To refresh it, start the dashboard, then:

```bash
python scripts/export_static.py site
```

Push the `site` folder to the `gh-pages` branch as a single fresh commit (force-push, so old snapshots don't pile up in the repo). GitHub rebuilds the site in about a minute.
