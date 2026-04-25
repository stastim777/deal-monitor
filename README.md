# Deal Monitor

A personal deal-watching service that runs on GitHub Actions for **$0/year**, reads its rules from a Google Sheet, and pushes alerts to your phone via [ntfy.sh](https://ntfy.sh/).

Designed for a config-light, edit-from-anywhere workflow: change a row in your sheet, the next hourly run picks it up automatically. No app deploy, no rebuild.

## What it does

Two rule types cover most use cases:

| Type | Use it for |
| --- | --- |
| `url_price` | Watch a specific product/booking page; alert when the price drops at or below your threshold. |
| `rss_keyword` | Subscribe to deal aggregator feeds (Slickdeals, subreddits) and alert on any new item whose title matches your keywords. |

Combine them: targeted scrapers for the products you care about, plus aggregator filters for serendipitous price-mistake style deals.

## Setup (one-time, ~30 minutes)

### 1. Get the code into your GitHub account

Create a new repo (public or private both work) and copy these files in. Public repos get unlimited free Actions minutes; private repos get 2,000/month, plenty for hourly runs.

### 2. Create the rules Google Sheet

Make a new Google Sheet with this header row:

```
enabled | name | type | url | selector_or_keywords | threshold_price | cooldown_hours | notes
```

Add one row per thing you want to monitor. See `sample_sheet.csv` for examples.

**Column reference:**

- `enabled` — `TRUE` or `FALSE`. Lets you pause a rule without deleting the row.
- `name` — Human-readable name. Becomes the rule's stable ID, so don't rename casually (a rename starts the rule's state fresh).
- `type` — `url_price` or `rss_keyword`.
- `url` — Page URL (for `url_price`) or RSS feed URL (for `rss_keyword`).
- `selector_or_keywords` — A CSS selector for the price element (`url_price`), or a comma-separated list of keywords (`rss_keyword`). Keywords are case-insensitive substring matches against item titles.
- `threshold_price` — Used by `url_price` only. Alert when the scraped price is at or below this value.
- `cooldown_hours` — Minimum hours between alerts for the same rule. `0` disables the cooldown. Defaults to 12 if blank.
- `notes` — Free text. The script ignores this; it's for you.

### 3. Publish the sheet as CSV

In Google Sheets: **File → Share → Publish to web** → pick the right tab → format **Comma-separated values (.csv)** → **Publish**. Copy the resulting URL.

> Publishing only exposes that tab read-only. The rest of the document, including edit access, stays private.

### 4. Pick an ntfy.sh topic

Topics are URL paths with no signup. Pick something **unguessable** — like `stas-deals-7f3k2vQ`. Anyone who knows your topic name can spam you, so don't use generic names like `deals` or `prices`.

Install the ntfy app on your phone (iOS or Android) and subscribe to your topic.

### 5. Add GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**:

- `SHEET_CSV_URL` — the published CSV URL from step 3
- `NTFY_TOPIC` — your topic slug from step 4 (just the slug, not the full URL)

### 6. Test it

Go to the **Actions** tab → **Check Deals** workflow → **Run workflow**. Watch the logs. If you see "No alerts to send this run" without errors, you're set. The hourly schedule takes over from there.

## How it works

Every hour:

1. Workflow triggers, runs `python -m deal_monitor.main`.
2. Script fetches your sheet as CSV.
3. For each enabled rule:
   - `url_price`: fetch the page, run the CSS selector, parse the first price, compare to threshold.
   - `rss_keyword`: fetch the feed, alert on new items matching any keyword.
4. Alerts are POSTed to ntfy.sh; push notification arrives on your phone.
5. Updated state (last alert times, seen RSS item IDs) is committed back to the repo.

## File layout

```
.
├── .github/workflows/check.yml   # Hourly cron schedule
├── deal_monitor/
│   ├── __init__.py
│   ├── main.py                   # Entry point and orchestration
│   ├── config_loader.py          # Reads/parses the sheet
│   ├── checkers.py               # url_price + rss_keyword logic
│   ├── notifier.py               # ntfy.sh push
│   └── state.py                  # state.json read/write
├── requirements.txt
├── sample_sheet.csv              # Example rules
├── state.json                    # Auto-updated; commit it once empty
└── README.md
```

## Cost

| Item | Cost |
| --- | --- |
| GitHub Actions (public repo) | Free, unlimited |
| GitHub Actions (private repo) | Free up to 2,000 min/month — uses ~30 |
| ntfy.sh public server | Free, no account |
| Google Sheets | Free |
| **Total** | **$0/year** |

Optional add-ons within a $30/year budget:

- **Keepa Premium** ($19/year) for proper Amazon price tracking with historical baselines. Needed if Amazon is a major source — Amazon actively blocks DIY scrapers.
- **Anthropic API credits** (a few dollars) for Haiku-powered "is this actually a deal?" filtering on noisy aggregator feeds. Drop-in extension; not built in by default.

## Limitations

- **JavaScript-heavy sites** (like the Innsbrook booking page) won't work with `url_price` because we use plain HTTP. For those: open Chrome DevTools → Network tab on the page, find the underlying JSON API the page calls, and point your rule at that instead. If there's no API, you'd need to add Playwright to `requirements.txt` and a checker variant that uses it (~30 extra seconds per check).
- **Amazon** actively blocks scrapers. Use Keepa.
- **First run for an `rss_keyword` rule does not alert** — it seeds the "already seen" snapshot from the current feed contents so you don't get a flood of historical matches. Going forward, only items that appear *after* the first run can trigger.
- **Renaming a rule** changes its `rule_id` (derived from `name`). The renamed rule's state starts fresh. To preserve state across a rename, edit `state.json` manually.

## Adding a new rule type

1. Add a function in `checkers.py` that takes a `Rule` (and any per-type state) and returns `List[Alert]`.
2. Add a dispatch branch in `main.py` next to the existing `url_price` / `rss_keyword` cases.
3. Document the new value of the `type` column in this README.

## Local testing

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export SHEET_CSV_URL='https://docs.google.com/.../pub?output=csv'
export NTFY_TOPIC='your-topic-slug'

python -m deal_monitor.main
```

The script writes `state.json` in the working directory; delete it to reset all cooldowns and seen-IDs.
