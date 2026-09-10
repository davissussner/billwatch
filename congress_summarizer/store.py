"""Durable, version-controlled storage for generated summaries.

Everything else in this project is reproducible: bill records come from bulk
data, rankings are a deterministic function of those records, and the site is
rendered from both. Summaries are the exception -- they cost money and are not
reproducible byte-for-byte even at temperature zero.

So they get written to `data/summaries/YYYY-MM.json` and committed, while the
SQLite database stays gitignored. The database is a cache; these files are the
record. They also make prompt changes reviewable as ordinary diffs.
"""

from __future__ import annotations

import argparse
import json

from .db import DATA_DIR, connect

SUMMARY_DIR = DATA_DIR / "summaries"


def export_month(conn, month: str) -> int:
    rows = conn.execute(
        """SELECT bill_id, model, prompt_version, text_version_code, summary,
                  created_at, input_tokens, output_tokens
           FROM summaries WHERE month = ? ORDER BY bill_id""",
        (month,),
    ).fetchall()
    if not rows:
        return 0

    payload = {
        "month": month,
        "summaries": [
            {
                "bill_id": r["bill_id"],
                "model": r["model"],
                "prompt_version": r["prompt_version"],
                "text_version_code": r["text_version_code"],
                "created_at": r["created_at"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "summary": json.loads(r["summary"]),
            }
            for r in rows
        ],
    }

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    # sort_keys + indent so a regenerated month produces a readable diff
    # rather than one giant changed line.
    (SUMMARY_DIR / f"{month}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return len(rows)


def import_all(conn) -> int:
    """Rehydrate the database from committed summary files."""
    if not SUMMARY_DIR.exists():
        return 0
    total = 0
    for path in sorted(SUMMARY_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        month = payload["month"]
        conn.executemany(
            """INSERT OR REPLACE INTO summaries
               (bill_id, month, model, prompt_version, text_version_code, summary,
                created_at, input_tokens, output_tokens)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(s["bill_id"], month, s["model"], s["prompt_version"], s["text_version_code"],
              json.dumps(s["summary"]), s["created_at"], s.get("input_tokens"), s.get("output_tokens"))
             for s in payload["summaries"]],
        )
        total += len(payload["summaries"])
    conn.commit()
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Move summaries between SQLite and committed JSON.")
    parser.add_argument("direction", choices=["export", "import"])
    parser.add_argument("--month", help="Export a single month (default: all).")
    args = parser.parse_args()

    conn = connect()
    if args.direction == "import":
        print(f"Imported {import_all(conn)} summaries from {SUMMARY_DIR}")
        return

    months = ([args.month] if args.month
              else [r[0] for r in conn.execute("SELECT DISTINCT month FROM summaries ORDER BY month")])
    total = sum(export_month(conn, m) for m in months)
    print(f"Exported {total} summaries across {len(months)} month(s) to {SUMMARY_DIR}")


if __name__ == "__main__":
    main()
