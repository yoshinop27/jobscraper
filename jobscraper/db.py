"""SQLite store: every posting we've seen, when we first saw it, and when the lists (Simplify etc.) caught up."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  key TEXT PRIMARY KEY,            -- source:board:job_id
  source TEXT, board TEXT, company TEXT, tier TEXT,
  title TEXT, locations TEXT, url TEXT, url_key TEXT, title_key TEXT,
  posted_at REAL, first_seen REAL, last_seen REAL, closed_at REAL,
  matched INTEGER, roles TEXT, cities TEXT, remote INTEGER, reasons TEXT,
  pay_lo REAL, pay_hi REAL, pay_est REAL,  -- posted range; reputation estimate when nothing is posted
  terms TEXT, degrees TEXT,                -- JSON, list postings only
  list_seen_at REAL, list_source TEXT,   -- when a community list first had this posting
  status TEXT DEFAULT 'new',             -- new | applied | hidden
  notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_url ON jobs(url_key);
CREATE INDEX IF NOT EXISTS jobs_title ON jobs(title_key);
CREATE INDEX IF NOT EXISTS jobs_board ON jobs(board);
CREATE TABLE IF NOT EXISTS boards (
  key TEXT PRIMARY KEY, last_poll REAL, last_ok REAL, last_error TEXT, n_jobs INTEGER, fails INTEGER DEFAULT 0,
  last_full REAL   -- last complete listing (incremental checks can't detect closed postings)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns that databases created by older versions lack."""
    jobs_cols = _columns(con, "jobs")
    for col, typ in (("pay_est", "REAL"), ("terms", "TEXT"), ("degrees", "TEXT")):
        if col not in jobs_cols:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    if "last_full" not in _columns(con, "boards"):
        con.execute("ALTER TABLE boards ADD COLUMN last_full REAL")
        con.execute("UPDATE boards SET last_full = last_ok")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL: commits are cheap, so we can commit per board
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def url_key(url: str | None) -> str:
    """Normalize a posting URL so the same job matches across sources (keeps gh_jid, drops tracking params)."""
    if not url:
        return ""
    u = urlparse(url.strip())
    gh_jid = parse_qs(u.query).get("gh_jid")
    if gh_jid:
        return f"gh:{gh_jid[0]}"
    host = u.netloc.lower().replace("www.", "").replace("job-boards.", "boards.")
    path = re.sub(r"/(apply|application)/?$", "", u.path.rstrip("/"))
    m = re.search(r"greenhouse\.io/[\w-]+/jobs/(\d+)", host + path)
    return f"gh:{m.group(1)}" if m else (host + path).lower()


def title_key(company_norm: str, title: str) -> str:
    return company_norm + "|" + re.sub(r"[^a-z0-9]", "", title.lower())


def get_meta(con, k: str, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def set_meta(con, k: str, v) -> None:
    con.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def now() -> float:
    return time.time()
