"""Shared database and path helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SITE_DIR = PROJECT_ROOT / "site"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
DB_PATH = DATA_DIR / "bills.db"

BILL_TYPES = ["hr", "s", "hres", "sres", "hjres", "sjres", "hconres", "sconres"]


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the database, creating it from schema.sql if needed."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript((PACKAGE_DIR / "schema.sql").read_text())
    return conn


def bill_id(congress: int | str, bill_type: str, number: int | str) -> str:
    return f"{congress}-{bill_type.lower()}-{number}"


def congress_gov_url(congress: int | str, bill_type: str, number: int | str) -> str:
    """Public congress.gov page for a bill. The URL uses a spelled-out type."""
    slug = {
        "hr": "house-bill",
        "s": "senate-bill",
        "hres": "house-resolution",
        "sres": "senate-resolution",
        "hjres": "house-joint-resolution",
        "sjres": "senate-joint-resolution",
        "hconres": "house-concurrent-resolution",
        "sconres": "senate-concurrent-resolution",
    }[bill_type.lower()]
    return f"https://www.congress.gov/bill/{congress}th-congress/{slug}/{number}"
