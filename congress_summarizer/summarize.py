"""Stage 4 -- generate structured briefs with Claude.

Uses the Batch API (50% cheaper, and nothing here is latency-sensitive).
Results come back in arbitrary order, so everything is keyed on custom_id.

The output schema is the real neutrality mechanism. Free-form prose drifts
toward advocacy under pressure; fixed fields do not. In particular,
`points_of_contention` requires an `attributed_to` on every entry, which makes
an unattributed opinion structurally impossible to emit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv

from .db import PACKAGE_DIR, connect
from .store import export_month

MODEL = "claude-sonnet-5"
PROMPT_VERSION = "summary_v1"
MAX_TOKENS = 8000

# Roughly 400k characters ~ 100k tokens. Above this we summarize the bill in
# chunks and compose, rather than truncating. Truncation would silently drop
# whole titles of an appropriations bill and the summary would not say so.
LONG_BILL_CHARS = 400_000
CHUNK_CHARS = 300_000

TOPICS = [
    "Environment & Energy", "Health", "Immigration", "Taxes & Economy", "Defense & Veterans",
    "Education", "Justice & Civil Rights", "Technology & Privacy", "Agriculture & Food",
    "Transportation & Infrastructure", "Government Operations", "Foreign Policy & Trade",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "takeaway": {"type": "string", "description": "One sentence: what this bill would do."},
        "what_it_does": {"type": "string", "description": "2-4 short paragraphs in plain English."},
        "key_provisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "provision": {"type": "string"},
                    "plain_english": {"type": "string"},
                },
                "required": ["provision", "plain_english"],
                "additionalProperties": False,
            },
        },
        "who_it_affects": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "description": "Where the bill stands, precisely."},
        "points_of_contention": {
            "type": "array",
            "description": "Empty when the bill is not contested. Never adjudicate.",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "attributed_to": {"type": "string", "description": "Who makes this claim."},
                },
                "required": ["claim", "attributed_to"],
                "additionalProperties": False,
            },
        },
        "topics": {"type": "array", "items": {"type": "string", "enum": TOPICS}},
        "uncertainty": {"type": "string", "description": "What the bill text leaves unspecified."},
    },
    "required": ["takeaway", "what_it_does", "key_provisions", "who_it_affects",
                 "status", "points_of_contention", "topics", "uncertainty"],
    "additionalProperties": False,
}

OUTPUT_CONFIG = {"format": {"type": "json_schema", "schema": SCHEMA}}


def system_prompt() -> str:
    return (PACKAGE_DIR / "prompts" / f"{PROMPT_VERSION}.md").read_text()


def build_user_content(row, bill_text: str) -> str:
    parts = [
        f"# {row['title']}",
        f"Bill: {row['bill_type'].upper()} {row['number']} ({row['congress']}th Congress)",
        f"Sponsor: {row['sponsor_name'] or 'unknown'}",
        f"Policy area: {row['policy_area'] or 'unassigned'}",
        f"Introduced: {row['introduced_date']}",
        f"Latest action ({row['latest_action_date']}): {row['latest_action_text']}",
    ]
    if row["crs_summary"]:
        parts.append(
            "\n## Congressional Research Service summary (non-partisan, for grounding)\n"
            + row["crs_summary"]
        )
    parts.append(f"\n## Bill text (version: {row['version_code']})\n{bill_text}")
    return "\n".join(parts)


def pending(conn, month: str, limit: int | None, regenerate: bool):
    """Selected bills for a month that have text but no current summary."""
    rows = conn.execute(
        """SELECT b.*, t.version_code, t.text AS bill_text, s.rank
           FROM selections s
           JOIN bills b ON b.bill_id = s.bill_id
           JOIN bill_text t ON t.bill_id = s.bill_id AND t.text IS NOT NULL
           LEFT JOIN summaries m ON m.bill_id = s.bill_id AND m.month = s.month
           WHERE s.month = ? AND (m.bill_id IS NULL OR ?)
           GROUP BY b.bill_id
           ORDER BY s.rank""",
        (month, 1 if regenerate else 0),
    ).fetchall()
    return rows[:limit] if limit else rows


def store(conn, bill_id: str, month: str, version_code: str, data: dict, usage) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO summaries
           (bill_id, month, model, prompt_version, text_version_code, summary, created_at, input_tokens, output_tokens)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (bill_id, month, MODEL, PROMPT_VERSION, version_code, json.dumps(data),
         datetime.now(timezone.utc).isoformat(),
         getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)),
    )
    conn.commit()


def condense_long_bill(client, row) -> str:
    """Map-reduce a bill too long to summarize in one pass.

    Appropriations bills and the NDAA run past 700k tokens. Chunking and
    composing keeps every title represented; truncating would drop some
    silently, and the brief would not be able to say which.
    """
    text = row["bill_text"]
    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
    print(f"    long bill ({len(text):,} chars) -> {len(chunks)} chunks")

    digests = []
    for i, chunk in enumerate(chunks, start=1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system="Summarize this portion of a bill factually and completely. List every "
                   "program, requirement, prohibition, and dollar amount. No evaluation.",
            messages=[{"role": "user", "content": f"Part {i} of {len(chunks)} of "
                                                  f"{row['title']}:\n\n{chunk}"}],
        )
        digests.append(next(b.text for b in response.content if b.type == "text"))
    return "\n\n".join(f"## Part {i}\n{d}" for i, d in enumerate(digests, start=1))


def run_live(conn, rows, month: str) -> None:
    """Synchronous path -- for reading a handful of summaries before spending on a batch."""
    client = anthropic.Anthropic()
    for row in rows:
        print(f"  [{row['rank']:3d}] {row['bill_id']}: {(row['title'] or '')[:60]}")
        text = row["bill_text"]
        if len(text) > LONG_BILL_CHARS:
            text = condense_long_bill(client, row)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium", **OUTPUT_CONFIG},
            system=system_prompt(),
            messages=[{"role": "user", "content": build_user_content(row, text)}],
        )
        if response.stop_reason == "refusal":
            print("    ! refused; skipping")
            continue
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
        store(conn, row["bill_id"], month, row["version_code"], data, response.usage)
        print(f"    {data['takeaway'][:90]}")


def run_batch(conn, rows, month: str, poll: int) -> None:
    client = anthropic.Anthropic()

    long_rows = [r for r in rows if len(r["bill_text"]) > LONG_BILL_CHARS]
    condensed: dict[str, str] = {}
    if long_rows:
        print(f"  condensing {len(long_rows)} oversized bill(s) first")
        for row in long_rows:
            condensed[row["bill_id"]] = condense_long_bill(client, row)

    requests = [
        Request(
            custom_id=row["bill_id"],
            params=MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium", **OUTPUT_CONFIG},
                system=system_prompt(),
                messages=[{"role": "user",
                           "content": build_user_content(row, condensed.get(row["bill_id"], row["bill_text"]))}],
            ),
        )
        for row in rows
    ]

    batch = client.messages.batches.create(requests=requests)
    print(f"  batch {batch.id} submitted with {len(requests)} requests")

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        counts = batch.request_counts
        print(f"  {batch.processing_status}: {counts.succeeded} done, {counts.processing} processing")
        time.sleep(poll)

    versions = {r["bill_id"]: r["version_code"] for r in rows}
    ok = bad = 0
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            print(f"  ! {result.custom_id}: {result.result.type}")
            bad += 1
            continue
        message = result.result.message
        if message.stop_reason == "refusal":
            print(f"  ! {result.custom_id}: refused")
            bad += 1
            continue
        try:
            data = json.loads(next(b.text for b in message.content if b.type == "text"))
        except (StopIteration, json.JSONDecodeError) as exc:
            print(f"  ! {result.custom_id}: unparseable ({exc})")
            bad += 1
            continue
        store(conn, result.custom_id, month, versions.get(result.custom_id), data, message.usage)
        ok += 1

    print(f"  stored {ok}, failed {bad}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate structured bill briefs with Claude.")
    parser.add_argument("--month", help="YYYY-MM. Omit with --all-months.")
    parser.add_argument("--all-months", action="store_true")
    parser.add_argument("--limit", type=int, help="Only the top N selected bills (use for a cheap trial run).")
    parser.add_argument("--no-batch", action="store_true", help="Synchronous, full price. Use with --limit to preview quality.")
    parser.add_argument("--regenerate", action="store_true", help="Overwrite existing summaries.")
    parser.add_argument("--poll", type=int, default=30, help="Seconds between batch status checks.")
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent.parent / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example).")

    conn = connect()
    if not args.month and not args.all_months:
        parser.error("pass --month YYYY-MM or --all-months")

    months = ([r[0] for r in conn.execute("SELECT DISTINCT month FROM selections ORDER BY month")]
              if args.all_months else [args.month])

    for month in months:
        rows = pending(conn, month, args.limit, args.regenerate)
        if not rows:
            print(f"{month}: nothing to do")
            continue
        print(f"{month}: {len(rows)} bill(s) to summarize")
        if args.no_batch:
            run_live(conn, rows, month)
        else:
            run_batch(conn, rows, month, args.poll)

        # The database is gitignored, so persist to the committed JSON
        # immediately -- these summaries cost money and are not reproducible.
        count = export_month(conn, month)
        print(f"  exported {count} summaries to data/summaries/{month}.json")


if __name__ == "__main__":
    main()
