"""Stage 2 -- pick the bills that actually moved in a given month.

This is the editorial core of the project, and the part most worth arguing
with. The rule is: a bill's score for a month is driven by the single most
consequential thing that happened to it that month -- not by how many times
it was touched. Procedural churn (a dozen referrals and reconsiderations)
should never outrank a floor vote.

Every score is decomposed and stored, so the site can show its work rather
than assert a ranking.

Calibrate with:  uv run rank --month 2026-07 --explain
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import defaultdict

from .db import connect

# --- Action tiers -------------------------------------------------------
#
# Ordered most-consequential first; the first pattern that matches an action
# determines its tier. Matching is on action text because the BILLSTATUS
# `type` field is unreliable (only one row in the 119th is typed BecameLaw,
# though 104 bills actually became law).

ACTION_TIERS: list[tuple[str, int, re.Pattern]] = [
    ("became_law", 100, re.compile(r"became public law|signed by president", re.I)),
    ("to_president", 60, re.compile(r"presented to president|cleared for white house", re.I)),
    ("passed_chamber", 40, re.compile(
        r"passed/agreed to in (house|senate)|"
        r"on passage passed|"
        r"passed (house|senate)(,| without)|"
        r"agreed to (by|in) (the )?(house|senate)|"
        r"agreed to by (recorded vote|yea-nay|voice vote|unanimous consent)",
        re.I)),
    ("reported", 20, re.compile(r"reported by|reported \(amended\)|ordered to be reported|placed on the union calendar|placed on senate legislative calendar", re.I)),
    ("committee_action", 10, re.compile(r"mark-?up|hearings held|subcommittee consideration|committee consideration", re.I)),
    ("amendment", 5, re.compile(r"amendment.{0,40}(offered|agreed to|submitted)", re.I)),
    ("introduced", 3, re.compile(r"^introduced|^referred to|sponsor introductory remarks", re.I)),
]

# Bills whose whole purpose is symbolic. They are legitimate legislative
# output, but they are not what someone scanning "the 100 bills that mattered"
# is looking for, and without a penalty they flood the list -- simple
# resolutions are cheap to pass and pass constantly.
CEREMONIAL_MULTIPLIER = 0.15

CEREMONIAL_TITLE = re.compile(
    r"^(a )?(resolution |concurrent resolution )?"
    r"(recognizing|congratulating|commemorating|honoring|celebrating|mourning|"
    r"expressing (the )?(support|sense|condolences|gratitude|appreciation)|"
    r"supporting the (goals and ideals|designation)|"
    # "designating the week of ... as \"National Farmers Market Week\"".
    # The observance name is usually quoted, so match the `as` clause loosely
    # rather than trying to enumerate names.
    r"designating .{0,140}?\bas\b|"
    r"proclaiming|welcoming|encouraging the people)",
    re.I,
)
# Naming a post office is the canonical example of a bill that becomes law and
# means nothing. These really do get enacted, so the law-tier score alone would
# push them straight to the top. The same applies to VA clinics, courthouses,
# and stretches of highway.
FACILITY_NAMING = re.compile(
    r"^to (designate|name|redesignate)\b.{0,160}?"
    r"\b(post office|postal service|facility|clinic|medical center|health care center|"
    r"building|courthouse|highway|bridge|trail|peak|dam|lock and dam)\b",
    re.I,
)


def classify(action_text: str) -> tuple[str, int]:
    for name, points, pattern in ACTION_TIERS:
        if pattern.search(action_text):
            return name, points
    return "other", 1


def is_ceremonial(bill_type: str, title: str | None) -> bool:
    title = title or ""
    if FACILITY_NAMING.search(title):
        return True
    # Only simple/concurrent resolutions get the title test: an `hr` titled
    # "recognizing..." is unusual enough to judge on its actions instead.
    return bill_type in ("hres", "sres", "hconres", "sconres") and bool(CEREMONIAL_TITLE.search(title))


def score_month(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Score every bill with activity in `month` (YYYY-MM)."""
    rows = conn.execute(
        """SELECT a.bill_id, a.text, b.bill_type, b.title, b.sponsor_party
           FROM actions a JOIN bills b ON b.bill_id = a.bill_id
           WHERE substr(a.action_date, 1, 7) = ?""",
        (month,),
    ).fetchall()
    if not rows:
        return []

    active: set[str] = {r["bill_id"] for r in rows}
    meta: dict[str, sqlite3.Row] = {}
    best: dict[str, tuple[str, int, str]] = {}

    for row in rows:
        meta.setdefault(row["bill_id"], row)
        tier, points = classify(row["text"])
        current = best.get(row["bill_id"])
        if current is None or points > current[1]:
            best[row["bill_id"]] = (tier, points, row["text"])

    # Cosponsor counts and party splits, for the support modifiers.
    cosponsors: dict[str, list[str]] = defaultdict(list)
    for bill_id, party in conn.execute(
        "SELECT bill_id, party FROM cosponsors WHERE bill_id IN (%s)"
        % ",".join("?" * len(active)),
        tuple(active),
    ):
        cosponsors[bill_id].append(party or "")

    # A companion bill moving in the same month is real evidence of momentum
    # that a single bill's own action history misses.
    companions: dict[str, bool] = {}
    for bill_id, related_id in conn.execute(
        "SELECT bill_id, related_bill_id FROM related_bills WHERE bill_id IN (%s)"
        % ",".join("?" * len(active)),
        tuple(active),
    ):
        if related_id in active:
            companions[bill_id] = True

    results = []
    for bill_id in active:
        row = meta[bill_id]
        tier, base, evidence = best[bill_id]

        supporters = cosponsors.get(bill_id, [])
        cosponsor_pts = min(10.0, 3 * math.log10(1 + len(supporters))) if supporters else 0.0

        bipartisan_pts = 0.0
        sponsor_party = row["sponsor_party"]
        if supporters and sponsor_party in ("D", "R"):
            other = sum(1 for p in supporters if p in ("D", "R") and p != sponsor_party)
            if other / len(supporters) >= 0.20:
                bipartisan_pts = 8.0

        companion_pts = 5.0 if companions.get(bill_id) else 0.0

        subtotal = base + cosponsor_pts + bipartisan_pts + companion_pts
        ceremonial = is_ceremonial(row["bill_type"], row["title"])
        score = subtotal * (CEREMONIAL_MULTIPLIER if ceremonial else 1.0)

        results.append({
            "bill_id": bill_id,
            "score": round(score, 2),
            "breakdown": {
                "top_action_tier": tier,
                "top_action_points": base,
                "top_action_text": evidence[:300],
                "cosponsors": len(supporters),
                "cosponsor_points": round(cosponsor_pts, 2),
                "bipartisan_points": bipartisan_pts,
                "companion_points": companion_pts,
                "subtotal": round(subtotal, 2),
                "ceremonial": ceremonial,
                "multiplier": CEREMONIAL_MULTIPLIER if ceremonial else 1.0,
            },
        })

    # Tie-break by bill_id so equal scores order deterministically -- reruns
    # must produce identical output for the same database.
    results.sort(key=lambda r: (-r["score"], r["bill_id"]))
    return results


def months_with_activity(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(action_date,1,7) m FROM actions WHERE action_date >= '2025-01' ORDER BY m"
    )]


def save(conn: sqlite3.Connection, month: str, ranked: list[dict], top_n: int) -> None:
    conn.execute("DELETE FROM selections WHERE month = ?", (month,))
    conn.executemany(
        "INSERT INTO selections (month, bill_id, rank, score, score_breakdown) VALUES (?,?,?,?,?)",
        [(month, r["bill_id"], i, r["score"], json.dumps(r["breakdown"]))
         for i, r in enumerate(ranked[:top_n], start=1)],
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank bills by legislative momentum for a month.")
    parser.add_argument("--month", help="YYYY-MM. Omit with --all-months to score every month.")
    parser.add_argument("--all-months", action="store_true")
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--explain", action="store_true", help="Print the ranking with score breakdowns instead of saving.")
    args = parser.parse_args()

    conn = connect()
    if not args.month and not args.all_months:
        parser.error("pass --month YYYY-MM or --all-months")

    months = months_with_activity(conn) if args.all_months else [args.month]

    for month in months:
        ranked = score_month(conn, month)
        if not ranked:
            print(f"{month}: no activity")
            continue

        if args.explain:
            print(f"\n=== {month}: {len(ranked)} bills with activity, showing top {args.top} ===")
            titles = dict(conn.execute("SELECT bill_id, title FROM bills"))
            for i, r in enumerate(ranked[:args.top], start=1):
                b = r["breakdown"]
                flag = " [ceremonial]" if b["ceremonial"] else ""
                print(f"{i:3d}. {r['score']:7.2f}  {r['bill_id']:<16} {b['top_action_tier']:<16}{flag}")
                print(f"      {(titles.get(r['bill_id']) or '')[:100]}")
        else:
            save(conn, month, ranked, args.top)
            print(f"{month}: scored {len(ranked)} bills, saved top {min(args.top, len(ranked))}")


if __name__ == "__main__":
    main()
