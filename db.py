import os
import sqlite3
from contextlib import contextmanager

DATABASE_PATH = os.getenv("DATABASE_PATH", "cali_law.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS firms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    website TEXT,
    domain TEXT,
    city TEXT,
    practice_areas TEXT,
    attorney_count INTEGER,
    linkedin_url TEXT,
    phone TEXT,
    email TEXT,
    source TEXT,
    last_updated TEXT,
    UNIQUE(name, city)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER REFERENCES firms(id),
    title TEXT NOT NULL,
    firm_name TEXT,
    location TEXT,
    work_mode TEXT,
    description TEXT,
    posted_date TEXT,
    source_url TEXT,
    source TEXT,
    active INTEGER DEFAULT 1,
    last_checked TEXT,
    UNIQUE(title, firm_name, source_url)
);

CREATE TABLE IF NOT EXISTS decision_makers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER REFERENCES firms(id),
    name TEXT,
    title TEXT,
    email TEXT,
    email_status TEXT,
    linkedin_url TEXT,
    role_category TEXT,
    source TEXT,
    apollo_contact_id TEXT,
    enrollment_status TEXT DEFAULT 'not_enrolled',
    UNIQUE(firm_id, name, email)
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role_type TEXT,
    state TEXT,
    city TEXT,
    location_preference TEXT,
    practice_areas TEXT,
    years_experience REAL,
    skills TEXT,
    certifications TEXT,
    salary_expectation TEXT,
    availability TEXT,
    status TEXT,
    uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    job_id INTEGER REFERENCES jobs(id),
    firm_id INTEGER REFERENCES firms(id),
    match_score REAL,
    match_reasons TEXT,
    created_at TEXT,
    UNIQUE(candidate_id, job_id)
);

CREATE TABLE IF NOT EXISTS leads (
    firm_id INTEGER PRIMARY KEY REFERENCES firms(id),
    score REAL,
    bucket TEXT,
    components TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS email_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER REFERENCES firms(id),
    job_id INTEGER REFERENCES jobs(id),
    decision_maker_id INTEGER REFERENCES decision_makers(id),
    candidate_id INTEGER REFERENCES candidates(id),
    template_type TEXT,
    subject TEXT,
    body TEXT,
    status TEXT DEFAULT 'pending_approval',
    created_at TEXT
);
"""


MIGRATIONS = [
    "ALTER TABLE decision_makers ADD COLUMN apollo_contact_id TEXT",
    "ALTER TABLE decision_makers ADD COLUMN enrollment_status TEXT DEFAULT 'not_enrolled'",
]


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column already exists
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()
