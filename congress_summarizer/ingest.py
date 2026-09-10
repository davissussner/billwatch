"""Stage 1 -- load GovInfo BILLSTATUS bulk data into SQLite.

BILLSTATUS is the backbone of this project. Each ZIP holds one XML file per
bill with the complete action history, sponsors, cosponsors, CRS policy area
and subjects, CRS summaries, and text-version URLs.

We use bulk data rather than the Congress.gov API because the API's
fromDateTime/toDateTime parameters filter on *record update time*, not action
date -- so the API cannot cleanly answer "what moved in July 2026". The bulk
action histories can.

Re-running is cheap and idempotent: ZIPs are re-downloaded only when the
server reports a newer Last-Modified, and every insert is an upsert.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from datetime import datetime, timezone

import httpx
from lxml import etree

from .db import BILL_TYPES, RAW_DIR, bill_id as make_bill_id, congress_gov_url, connect

BULK_URL = "https://www.govinfo.gov/bulkdata/BILLSTATUS/{congress}/{bill_type}/BILLSTATUS-{congress}-{bill_type}.zip"

# Text-version URLs look like .../BILLS-119hr1234rh/xml/BILLS-119hr1234rh.xml
# The trailing letters after the bill number are the version code (ih, rh, enr...).
VERSION_CODE_RE = re.compile(r"BILLS-\d+[a-z]+\d+([a-z]+)", re.IGNORECASE)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(raw: str | None) -> str | None:
    """CRS summaries arrive as HTML inside a CDATA block."""
    if not raw:
        return None
    text = TAG_RE.sub(" ", raw)
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return WS_RE.sub(" ", text).strip() or None


def text_of(node, path: str) -> str | None:
    """findtext() that normalizes empty strings to None."""
    value = node.findtext(path)
    if value is None:
        return None
    value = value.strip()
    return value or None


def download(url: str, conn, force: bool = False) -> bytes | None:
    """Fetch a bulk ZIP, skipping the download if our cached copy is current.

    Returns the ZIP bytes, or None when the cached file is already up to date.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    local = RAW_DIR / url.rsplit("/", 1)[-1]
    row = conn.execute("SELECT last_modified FROM source_files WHERE url = ?", (url,)).fetchone()

    if not force and local.exists() and row and row["last_modified"]:
        head = httpx.head(url, follow_redirects=True, timeout=30)
        remote_modified = head.headers.get("last-modified")
        if remote_modified and remote_modified == row["last_modified"]:
            print(f"  cached (unchanged): {local.name}")
            return None

    print(f"  downloading {local.name} ...", end="", flush=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        payload = response.read()
        last_modified = response.headers.get("last-modified")

    local.write_bytes(payload)
    print(f" {len(payload) / 1e6:.1f} MB")
    conn.execute(
        "INSERT OR REPLACE INTO source_files (url, last_modified, fetched_at, bytes) VALUES (?, ?, ?, ?)",
        (url, last_modified, datetime.now(timezone.utc).isoformat(), len(payload)),
    )
    conn.commit()
    return payload


def parse_bill(xml_bytes: bytes) -> dict | None:
    """Turn one BILLSTATUS XML document into rows for every table.

    Parses defensively: GovInfo changes its schema occasionally, and a missing
    optional element must never drop an otherwise-good bill.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return None

    bill = root.find("bill")
    if bill is None:
        return None

    congress = text_of(bill, "congress")
    bill_type = text_of(bill, "type")
    number = text_of(bill, "number")
    if not (congress and bill_type and number):
        return None

    bid = make_bill_id(congress, bill_type, number)
    sponsor = bill.find("sponsors/item")

    # CRS writes several summaries over a bill's life; keep the most recent.
    crs_summary = None
    summaries = bill.findall("summaries/summary")
    if summaries:
        latest = max(summaries, key=lambda s: (s.findtext("actionDate") or "", s.findtext("updateDate") or ""))
        crs_summary = strip_html(latest.findtext("cdata/text") or latest.findtext("text"))

    record = {
        "bill": (
            bid,
            int(congress),
            bill_type.lower(),
            int(number),
            text_of(bill, "title"),
            text_of(sponsor, "fullName") if sponsor is not None else None,
            text_of(sponsor, "bioguideId") if sponsor is not None else None,
            text_of(sponsor, "party") if sponsor is not None else None,
            text_of(sponsor, "state") if sponsor is not None else None,
            text_of(bill, "policyArea/name"),
            text_of(bill, "introducedDate"),
            text_of(bill, "latestAction/actionDate"),
            text_of(bill, "latestAction/text"),
            crs_summary,
            congress_gov_url(congress, bill_type, number),
            text_of(bill, "updateDate"),
        ),
        "actions": [],
        "cosponsors": [],
        "subjects": [],
        "related": [],
        "text_versions": [],
    }

    for item in bill.findall("actions/item"):
        action_date = text_of(item, "actionDate")
        action_text = text_of(item, "text")
        if not action_date or not action_text:
            continue
        record["actions"].append((
            bid,
            action_date[:10],
            text_of(item, "actionCode"),
            text_of(item, "type"),
            text_of(item, "sourceSystem/name"),
            action_text,
        ))

    for item in bill.findall("cosponsors/item"):
        bioguide = text_of(item, "bioguideId")
        if not bioguide:
            continue
        record["cosponsors"].append((
            bid,
            bioguide,
            text_of(item, "fullName"),
            text_of(item, "party"),
            text_of(item, "state"),
            text_of(item, "sponsorshipDate"),
        ))

    for item in bill.findall("subjects/legislativeSubjects/item"):
        name = text_of(item, "name")
        if name:
            record["subjects"].append((bid, name))

    for item in bill.findall("relatedBills/item"):
        r_congress = text_of(item, "congress")
        r_type = text_of(item, "type")
        r_number = text_of(item, "number")
        if not (r_congress and r_type and r_number):
            continue
        record["related"].append((
            bid,
            make_bill_id(r_congress, r_type, r_number),
            text_of(item, "relationshipDetails/item/type"),
        ))

    for item in bill.findall("textVersions/item"):
        url = text_of(item, "formats/item/url")
        if not url:
            continue
        match = VERSION_CODE_RE.search(url)
        record["text_versions"].append((
            bid,
            (match.group(1) if match else "unknown").lower(),
            (text_of(item, "date") or "")[:10] or None,
            url,
            "xml" if url.endswith(".xml") else "htm",
        ))

    return record


def write(conn, records: list[dict]) -> None:
    """Upsert a batch of parsed bills."""
    conn.executemany(
        "INSERT OR REPLACE INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [r["bill"] for r in records],
    )
    # Identical (date, text) actions are genuine duplicates across source
    # systems; collapsing them is correct for momentum scoring.
    conn.executemany(
        "INSERT OR IGNORE INTO actions (bill_id, action_date, action_code, action_type, chamber, text) VALUES (?,?,?,?,?,?)",
        [row for r in records for row in r["actions"]],
    )
    conn.executemany("INSERT OR REPLACE INTO cosponsors VALUES (?,?,?,?,?,?)", [row for r in records for row in r["cosponsors"]])
    conn.executemany("INSERT OR IGNORE INTO subjects VALUES (?,?)", [row for r in records for row in r["subjects"]])
    conn.executemany("INSERT OR REPLACE INTO related_bills VALUES (?,?,?)", [row for r in records for row in r["related"]])
    # Preserve any bill text already fetched; only refresh the metadata.
    conn.executemany(
        """INSERT INTO bill_text (bill_id, version_code, version_date, url, format)
           VALUES (?,?,?,?,?)
           ON CONFLICT(bill_id, version_code) DO UPDATE SET
             version_date = excluded.version_date,
             url = excluded.url,
             format = excluded.format""",
        [row for r in records for row in r["text_versions"]],
    )
    conn.commit()


def ingest_type(conn, congress: int, bill_type: str, force: bool) -> int:
    url = BULK_URL.format(congress=congress, bill_type=bill_type)
    local = RAW_DIR / url.rsplit("/", 1)[-1]

    payload = download(url, conn, force=force)
    if payload is None:
        if not local.exists():
            print(f"  no local copy of {local.name}; skipping")
            return 0
        payload = local.read_bytes()

    count = 0
    batch: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".xml"):
                continue
            record = parse_bill(archive.read(name))
            if record is None:
                print(f"  warning: could not parse {name}", file=sys.stderr)
                continue
            batch.append(record)
            count += 1
            if len(batch) >= 500:
                write(conn, batch)
                batch = []
    if batch:
        write(conn, batch)
    print(f"  {bill_type}: {count} bills")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Load BILLSTATUS bulk data into SQLite.")
    parser.add_argument("--congress", type=int, default=119)
    parser.add_argument("--bill-type", choices=BILL_TYPES, help="Ingest only one bill type (default: all eight).")
    parser.add_argument("--force", action="store_true", help="Re-download even if the cached ZIP looks current.")
    args = parser.parse_args()

    conn = connect()
    types = [args.bill_type] if args.bill_type else BILL_TYPES

    print(f"Ingesting Congress {args.congress}")
    total = sum(ingest_type(conn, args.congress, bill_type, args.force) for bill_type in types)

    bills, actions = conn.execute("SELECT (SELECT COUNT(*) FROM bills), (SELECT COUNT(*) FROM actions)").fetchone()
    print(f"\nDone. {total} bills this run. Database now holds {bills} bills and {actions} actions.")


if __name__ == "__main__":
    main()
