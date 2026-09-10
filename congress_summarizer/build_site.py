"""Stage 5 -- render the static site.

Output is plain HTML + one JSON search index. No build step beyond this
script, nothing to run server-side: the result works opened from disk and
works on GitHub Pages unchanged.

Bills without a generated summary still get a page. The site says so plainly
rather than hiding them, so the gap between "selected" and "summarized" is
always visible.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .db import SITE_DIR, TEMPLATE_DIR, connect

# The CRS assigns one of 32 official policy areas. Readers think in fewer,
# broader buckets -- and "environment" in particular is split across three
# CRS areas, so a single click has to reach all of them.
POLICY_AREA_TO_TOPIC = {
    "Environmental Protection": "Environment & Energy",
    "Energy": "Environment & Energy",
    "Public Lands and Natural Resources": "Environment & Energy",
    "Water Resources Development": "Environment & Energy",
    "Health": "Health",
    "Immigration": "Immigration",
    "Taxation": "Taxes & Economy",
    "Economics and Public Finance": "Taxes & Economy",
    "Finance and Financial Sector": "Taxes & Economy",
    "Commerce": "Taxes & Economy",
    "Labor and Employment": "Taxes & Economy",
    "Armed Forces and National Security": "Defense & Veterans",
    "Education": "Education",
    "Crime and Law Enforcement": "Justice & Civil Rights",
    "Civil Rights and Liberties, Minority Issues": "Justice & Civil Rights",
    "Law": "Justice & Civil Rights",
    "Science, Technology, Communications": "Technology & Privacy",
    "Agriculture and Food": "Agriculture & Food",
    "Transportation and Public Works": "Transportation & Infrastructure",
    "Housing and Community Development": "Transportation & Infrastructure",
    "Government Operations and Politics": "Government Operations",
    "Congress": "Government Operations",
    "International Affairs": "Foreign Policy & Trade",
    "Foreign Trade and International Finance": "Foreign Policy & Trade",
    "Native Americans": "Justice & Civil Rights",
    "Social Welfare": "Health",
    "Families": "Health",
    "Emergency Management": "Government Operations",
    "Arts, Culture, Religion": "Government Operations",
    "Sports and Recreation": "Government Operations",
    "Animals": "Agriculture & Food",
}

TIER_LABEL = {
    "became_law": "Became law",
    "to_president": "Sent to the President",
    "passed_chamber": "Passed a chamber",
    "reported": "Reported out of committee",
    "committee_action": "Committee action",
    "amendment": "Amendment activity",
    "introduced": "Introduced or referred",
    "other": "Other action",
}


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def bill_label(row) -> str:
    pretty = {"hr": "H.R.", "s": "S.", "hres": "H.Res.", "sres": "S.Res.", "hjres": "H.J.Res.",
              "sjres": "S.J.Res.", "hconres": "H.Con.Res.", "sconres": "S.Con.Res."}
    return f"{pretty.get(row['bill_type'], row['bill_type'].upper())} {row['number']}"


def load(conn) -> list[dict]:
    """One record per (bill, month) selection, with summary attached if present."""
    rows = conn.execute(
        """SELECT s.month, s.rank, s.score, s.score_breakdown,
                  b.bill_id, b.bill_type, b.number, b.congress, b.title,
                  b.sponsor_name, b.sponsor_party, b.sponsor_state,
                  b.policy_area, b.introduced_date, b.latest_action_date,
                  b.latest_action_text, b.crs_summary, b.congress_gov_url,
                  m.summary, m.model, m.prompt_version, m.text_version_code
           FROM selections s
           JOIN bills b ON b.bill_id = s.bill_id
           LEFT JOIN summaries m ON m.bill_id = s.bill_id AND m.month = s.month
           ORDER BY s.month DESC, s.rank"""
    ).fetchall()

    records = []
    for row in rows:
        summary = json.loads(row["summary"]) if row["summary"] else None
        breakdown = json.loads(row["score_breakdown"])

        topics = summary["topics"] if summary and summary.get("topics") else []
        if not topics:
            mapped = POLICY_AREA_TO_TOPIC.get(row["policy_area"] or "")
            topics = [mapped] if mapped else []

        records.append({
            "bill_id": row["bill_id"],
            "month": row["month"],
            "month_label": month_label(row["month"]),
            "rank": row["rank"],
            "score": row["score"],
            "breakdown": breakdown,
            "tier_label": TIER_LABEL.get(breakdown.get("top_action_tier"), "Action"),
            "label": bill_label(row),
            "title": row["title"] or "(untitled)",
            "sponsor": row["sponsor_name"],
            "sponsor_party": row["sponsor_party"],
            "policy_area": row["policy_area"],
            "introduced_date": row["introduced_date"],
            "latest_action_date": row["latest_action_date"],
            "latest_action_text": row["latest_action_text"],
            "crs_summary": row["crs_summary"],
            "url": row["congress_gov_url"],
            "summary": summary,
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "text_version_code": row["text_version_code"],
            "topics": topics,
            "page": f"bills/{row['month']}-{row['bill_id']}.html",
        })
    return records


def render(env, template: str, out_path, **context) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(env.get_template(template).render(**context), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the static site into site/.")
    parser.add_argument("--clean", action="store_true", help="Delete site/ before building.")
    args = parser.parse_args()

    conn = connect()
    records = load(conn)
    if not records:
        raise SystemExit("No selections found. Run `rank` first.")

    if args.clean and SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))
    env.globals["now"] = datetime.now(timezone.utc).strftime("%B %d, %Y")

    by_month: dict[str, list] = defaultdict(list)
    by_topic: dict[str, list] = defaultdict(list)
    for record in records:
        by_month[record["month"]].append(record)
        for topic in record["topics"]:
            by_topic[topic].append(record)

    months = sorted(by_month, reverse=True)
    topics = sorted(by_topic)
    topic_slugs = {topic: slugify(topic) for topic in topics}

    nav = {
        "months": [(m, month_label(m)) for m in months],
        "topics": [(t, topic_slugs[t]) for t in topics],
    }
    summarized = sum(1 for r in records if r["summary"])
    stats = {
        "bills": len({r["bill_id"] for r in records}),
        "selections": len(records),
        "months": len(months),
        "summarized": summarized,
        "unsummarized": len(records) - summarized,
    }

    latest = months[0]
    render(env, "month.html", SITE_DIR / "index.html", records=by_month[latest], month=latest,
           month_label=month_label(latest), nav=nav, stats=stats, is_index=True, depth="")

    for month in months:
        render(env, "month.html", SITE_DIR / "months" / f"{month}.html", records=by_month[month],
               month=month, month_label=month_label(month), nav=nav, stats=stats,
               is_index=False, depth="../")

    for topic in topics:
        render(env, "topic.html", SITE_DIR / "topics" / f"{topic_slugs[topic]}.html",
               records=by_topic[topic], topic=topic, nav=nav, stats=stats, depth="../")

    for record in records:
        render(env, "bill.html", SITE_DIR / record["page"], record=record, nav=nav,
               stats=stats, depth="../")

    render(env, "about.html", SITE_DIR / "about.html", nav=nav, stats=stats, depth="")
    render(env, "search.html", SITE_DIR / "search.html", nav=nav, stats=stats, depth="")

    # Search index. Kept deliberately small: title, sponsor, topic, and the
    # takeaway line -- enough to find a bill, not enough to bloat the payload.
    index = [{
        "id": f"{r['month']}-{r['bill_id']}",
        "t": r["title"],
        "l": r["label"],
        "m": r["month"],
        "ml": r["month_label"],
        "tp": r["topics"],
        "s": r["sponsor"] or "",
        "k": (r["summary"]["takeaway"] if r["summary"] else (r["crs_summary"] or "")[:300]),
        "u": r["page"],
        "r": r["rank"],
    } for r in records]
    (SITE_DIR / "data").mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "data" / "search-index.json").write_text(
        json.dumps(index, separators=(",", ":")), encoding="utf-8")

    shutil.copy(TEMPLATE_DIR / "style.css", SITE_DIR / "style.css")

    size = (SITE_DIR / "data" / "search-index.json").stat().st_size
    print(f"Built {len(records)} bill pages across {len(months)} months and {len(topics)} topics.")
    print(f"Search index: {size / 1e6:.2f} MB")
    print(f"Summaries: {summarized} generated, {stats['unsummarized']} pending.")
    print(f"Open: {SITE_DIR / 'index.html'}")


if __name__ == "__main__":
    main()
