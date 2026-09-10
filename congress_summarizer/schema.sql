-- Congress bill summarizer. One SQLite database, one table per concern.
-- Every stage of the pipeline is re-runnable: writes are upserts keyed on
-- natural keys, so re-ingesting or re-ranking never duplicates rows.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS bills (
    bill_id             TEXT PRIMARY KEY,   -- '119-hr-1234'
    congress            INTEGER NOT NULL,
    bill_type           TEXT NOT NULL,      -- hr, s, hres, sres, hjres, sjres, hconres, sconres
    number              INTEGER NOT NULL,
    title               TEXT,
    sponsor_name        TEXT,
    sponsor_bioguide    TEXT,
    sponsor_party       TEXT,
    sponsor_state       TEXT,
    policy_area         TEXT,               -- official CRS policy area (32 possible values)
    introduced_date     TEXT,
    latest_action_date  TEXT,
    latest_action_text  TEXT,
    crs_summary         TEXT,               -- most recent CRS summary, plain text
    congress_gov_url    TEXT,
    update_date         TEXT
);

CREATE INDEX IF NOT EXISTS idx_bills_policy_area ON bills(policy_area);

-- The table monthly scoring reads. One row per action taken on a bill.
CREATE TABLE IF NOT EXISTS actions (
    bill_id      TEXT NOT NULL,
    action_date  TEXT NOT NULL,             -- YYYY-MM-DD
    action_code  TEXT,
    action_type  TEXT,
    chamber      TEXT,
    text         TEXT,
    PRIMARY KEY (bill_id, action_date, text)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_actions_date ON actions(action_date);
CREATE INDEX IF NOT EXISTS idx_actions_bill ON actions(bill_id);

CREATE TABLE IF NOT EXISTS cosponsors (
    bill_id    TEXT NOT NULL,
    bioguide   TEXT NOT NULL,
    name       TEXT,
    party      TEXT,
    state      TEXT,
    date       TEXT,
    PRIMARY KEY (bill_id, bioguide)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS subjects (
    bill_id  TEXT NOT NULL,
    subject  TEXT NOT NULL,
    PRIMARY KEY (bill_id, subject)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS related_bills (
    bill_id          TEXT NOT NULL,
    related_bill_id  TEXT NOT NULL,
    relationship     TEXT,
    PRIMARY KEY (bill_id, related_bill_id)
) WITHOUT ROWID;

-- Text versions available for a bill, from BILLSTATUS <textVersions>.
-- `text` is NULL until fetch-text pulls it (only for selected bills).
CREATE TABLE IF NOT EXISTS bill_text (
    bill_id       TEXT NOT NULL,
    version_code  TEXT NOT NULL,            -- IH, RH, ENR, ...
    version_date  TEXT,
    url           TEXT,
    format        TEXT,                     -- xml | htm
    text          TEXT,
    char_count    INTEGER,
    PRIMARY KEY (bill_id, version_code)
) WITHOUT ROWID;

-- The top N bills for a month, with the score breakdown that put them there.
-- Storing the breakdown means the site can explain a selection rather than assert it.
CREATE TABLE IF NOT EXISTS selections (
    month            TEXT NOT NULL,         -- 'YYYY-MM'
    bill_id          TEXT NOT NULL,
    rank             INTEGER NOT NULL,
    score            REAL NOT NULL,
    score_breakdown  TEXT NOT NULL,         -- JSON
    PRIMARY KEY (month, bill_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_selections_rank ON selections(month, rank);

-- One summary per (bill, month). Model + prompt_version + text_version_code
-- are recorded so any published summary is reproducible, and so a prompt
-- change can be rolled out selectively instead of by full regeneration.
CREATE TABLE IF NOT EXISTS summaries (
    bill_id            TEXT NOT NULL,
    month              TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    text_version_code  TEXT,
    summary            TEXT NOT NULL,       -- JSON matching the structured-output schema
    created_at         TEXT NOT NULL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    PRIMARY KEY (bill_id, month)
) WITHOUT ROWID;

-- Tracks bulk-data downloads so re-running ingest only refetches what changed.
CREATE TABLE IF NOT EXISTS source_files (
    url            TEXT PRIMARY KEY,
    last_modified  TEXT,
    etag           TEXT,
    fetched_at     TEXT,
    bytes          INTEGER
);
