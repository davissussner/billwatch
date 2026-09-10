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

Early. Project scaffolding and the database schema are in place; the pipeline stages are
not written yet.

| Stage | File | Status |
|---|---|---|
| Ingest bulk bill data | `src/congress_summarizer/ingest.py` | not started |
| Rank bills by monthly momentum | `src/congress_summarizer/rank.py` | not started |
| Fetch bill text | `src/congress_summarizer/text.py` | not started |
| Generate summaries | `src/congress_summarizer/summarize.py` | not started |
| Build the static site | `src/congress_summarizer/build_site.py` | not started |

Done: `pyproject.toml`, `schema.sql`, `db.py`.

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
