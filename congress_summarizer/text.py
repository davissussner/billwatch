"""Stage 3 -- fetch the full text of selected bills.

Only bills that made a monthly top-100 get their text pulled, so this touches
roughly 100 bills a month rather than all 18,000.

Which version we summarized matters: a bill as introduced can differ sharply
from the same bill as reported or as enrolled. We always take the most recent
version available and record its code, so the site can say exactly what was
read.
"""

from __future__ import annotations

import argparse
import re
import time

import httpx
from lxml import etree

from .db import connect

# Roughly newest-last. GovInfo version codes: introduced, reported, engrossed,
# received, placed on calendar, agreed to, enrolled.
VERSION_ORDER = [
    "ih", "is", "iph", "ips",              # introduced
    "rh", "rs", "rfh", "rfs", "rch", "rcs",  # reported / referred
    "eh", "es", "eah", "eas",              # engrossed
    "pch", "pcs",                          # placed on calendar
    "ath", "ats",                          # agreed to
    "enr",                                 # enrolled -- the version that became law
]

WS_RE = re.compile(r"[ \t]+")
BLANK_RE = re.compile(r"\n{3,}")


def version_rank(code: str) -> int:
    try:
        return VERSION_ORDER.index(code.lower())
    except ValueError:
        return -1


def xml_to_text(payload: bytes) -> str:
    """Flatten a GovInfo bill XML document to readable plain text.

    Bill XML is deeply nested (sections inside subsections inside quoted
    blocks); itertext() preserves reading order without us having to model
    the schema, which changes between congresses.
    """
    try:
        root = etree.fromstring(payload)
    except etree.XMLSyntaxError:
        return ""

    parts: list[str] = []
    for element in root.iter():
        if element.tag is etree.Comment:
            continue
        tag = etree.QName(element).localname if isinstance(element.tag, str) else ""
        text = (element.text or "").strip()
        if text:
            # Put structural headings on their own line.
            parts.append(f"\n\n{text}" if tag in {"section", "subsection", "header", "enum"} else text)
        tail = (element.tail or "").strip()
        if tail:
            parts.append(tail)

    joined = " ".join(parts)
    joined = WS_RE.sub(" ", joined)
    return BLANK_RE.sub("\n\n", joined).strip()


def html_to_text(payload: bytes) -> str:
    text = re.sub(rb"<(script|style)[^>]*>.*?</\1>", b" ", payload, flags=re.S | re.I)
    text = re.sub(rb"<[^>]+>", b" ", text).decode("utf-8", errors="replace")
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return BLANK_RE.sub("\n\n", WS_RE.sub(" ", text)).strip()


def fetch_for_month(conn, month: str, force: bool, pause: float) -> tuple[int, int]:
    rows = conn.execute(
        """SELECT t.bill_id, t.version_code, t.url, t.format
           FROM bill_text t
           JOIN selections s ON s.bill_id = t.bill_id
           WHERE s.month = ? AND (t.text IS NULL OR ?)""",
        (month, 1 if force else 0),
    ).fetchall()

    # One bill has many versions; keep only the newest per bill.
    newest: dict[str, tuple] = {}
    for row in rows:
        current = newest.get(row["bill_id"])
        if current is None or version_rank(row["version_code"]) > version_rank(current["version_code"]):
            newest[row["bill_id"]] = row

    fetched = failed = 0
    with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "congress-summarizer/0.1"}) as client:
        for i, (bill_id, row) in enumerate(sorted(newest.items()), start=1):
            try:
                response = client.get(row["url"])
                response.raise_for_status()
                body = xml_to_text(response.content) if row["format"] == "xml" else html_to_text(response.content)
                if not body:
                    raise ValueError("extracted no text")
                conn.execute(
                    "UPDATE bill_text SET text = ?, char_count = ? WHERE bill_id = ? AND version_code = ?",
                    (body, len(body), bill_id, row["version_code"]),
                )
                fetched += 1
            except Exception as exc:  # noqa: BLE001 -- one bad bill must not stop the run
                print(f"  ! {bill_id} ({row['version_code']}): {exc}")
                failed += 1
            if i % 25 == 0:
                conn.commit()
                print(f"  {i}/{len(newest)} ...")
            time.sleep(pause)
    conn.commit()
    return fetched, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch full text for selected bills.")
    parser.add_argument("--month", help="YYYY-MM. Omit with --all-months.")
    parser.add_argument("--all-months", action="store_true")
    parser.add_argument("--force", action="store_true", help="Refetch even if text is already cached.")
    parser.add_argument("--pause", type=float, default=0.2, help="Seconds between requests (be polite to GovInfo).")
    args = parser.parse_args()

    conn = connect()
    if not args.month and not args.all_months:
        parser.error("pass --month YYYY-MM or --all-months")

    months = ([r[0] for r in conn.execute("SELECT DISTINCT month FROM selections ORDER BY month")]
              if args.all_months else [args.month])

    total_ok = total_bad = 0
    for month in months:
        print(f"{month}:")
        ok, bad = fetch_for_month(conn, month, args.force, args.pause)
        print(f"  fetched {ok}, failed {bad}")
        total_ok += ok
        total_bad += bad

    print(f"\nDone. {total_ok} texts fetched, {total_bad} failed.")


if __name__ == "__main__":
    main()
