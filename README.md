# Congress Bill Summarizer

Plain-English, non-partisan briefs on the ~100 bills that actually moved in Congress each month.
Searchable by keyword, filterable by topic and by month.

Published as a static site on GitHub Pages.

## Why

Congress introduces roughly 15,000 bills per Congress. Almost none of them matter, a few
matter enormously, and the difference is invisible unless you read the action history. This
project does two things:

1. **Picks the ~100 bills that actually moved each month**, using a published, reproducible
   momentum score based on what happened to each bill — floor votes, committee reports,
   enactment — not on anyone's opinion of what's important.
2. **Explains each one in plain English**, in a fixed structure that separates what a bill
   *does* from what people *claim about it*.

The methodology is published on the site's About page. It is meant to be argued with.

## Status

The pipeline runs end to end and the site builds. Summaries are the one thing not yet
generated — that needs an Anthropic API key.

| Stage | Command | Status |
|---|---|---|
| Ingest bulk bill data | `python -m congress_summarizer.ingest --congress 119` | working — 18,635 bills, 57,577 actions |
| Rank by monthly momentum | `python -m congress_summarizer.rank --all-months` | working — 21 months, 100 bills each |
| Fetch bill text | `python -m congress_summarizer.text --month 2026-07` | working — 100/100 for July 2026 |
| Generate summaries | `python -m congress_summarizer.summarize --month 2026-07` | written, not yet run (needs API key) |
| Build the static site | `python -m congress_summarizer.build_site` | working — 2,100 pages, 12 topics |

Prefix each with `uv run`. Use `python -m` rather than the console scripts: uv ships a
`_virtualenv.pth` that sorts after hatchling's editable-install `.pth` and drops the path it
added, so `uv run ingest` fails intermittently while `uv run python -m ...` always works.

## Run it locally

```bash
uv sync
uv run python -m congress_summarizer.ingest --congress 119
uv run python -m congress_summarizer.rank --all-months
uv run python -m congress_summarizer.build_site --clean
cd site && python3 -m http.server 8000
```

Then open <http://localhost:8000>. Serve over HTTP rather than opening `index.html` from
disk — search fetches a JSON index, which browsers block on `file://` URLs.

Before spending anything on summaries, read a few:

```bash
uv run python -m congress_summarizer.summarize --month 2026-07 --limit 5 --no-batch
```

That runs synchronously at full price for five bills. If they read well, drop `--limit`
and `--no-batch` to run the month through the Batch API at half price.

## Setup

Requires Python 3.10+ (the `anthropic` SDK dropped 3.9). This repo uses
[uv](https://docs.astral.sh/uv/) to manage both the interpreter and the venv.

```bash
uv python install 3.13
uv sync
```

Then copy `.env.example` to `.env` and fill in the keys:

- `ANTHROPIC_API_KEY` — from <https://console.anthropic.com/settings/keys>
- `CONGRESS_API_KEY` — free, from <https://api.congress.gov/sign-up/>.
  Only needed for incremental updates; the bulk-data backfill works without it.

`.env` is gitignored. Never commit it.

## Data sources

- **[GovInfo BILLSTATUS bulk data](https://www.govinfo.gov/bulkdata/BILLSTATUS/119)** — the backbone.
  Per-bill-type ZIPs of XML with complete action histories, sponsors, cosponsors, CRS policy
  areas, subjects, CRS summaries, and text-version URLs. No key, no rate limit, refreshed daily.
- **[Congress.gov API v3](https://api.congress.gov/)** — secondary, for incremental updates.
  5,000 requests/hour, `limit` caps at 250.

Bulk data is the primary source because the Congress.gov API's `fromDateTime`/`toDateTime`
parameters filter on *record update time*, not action date — so the API can't cleanly answer
"what moved in July 2026," and the bulk action histories can.

## Layout

```
src/congress_summarizer/   pipeline stages + schema.sql
templates/                 Jinja2 templates for the static site
data/raw/                  downloaded bulk ZIPs        (gitignored)
data/bills.db              SQLite                      (gitignored, regenerable)
site/                      build output — what gets published
```

`data/` is gitignored on purpose: it is entirely reproducible from the bulk sources, and the
raw XML runs to hundreds of megabytes. Summaries live in SQLite; the published `site/` output
is the durable artifact.

## Cost

Summaries are generated with `claude-sonnet-5` through the Batch API (50% discount).
Backfilling the 119th Congress (~2,000 bills) costs roughly $60 one time; ongoing is about
$3/month.

## License

TBD.
