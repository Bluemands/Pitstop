"""SQLite persistence: findings, decisions, disk index, action journal."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path(os.environ.get("PCC_DB", Path(__file__).resolve().parent.parent / "data" / "pcc.sqlite"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    scanner TEXT, category TEXT, title TEXT, path TEXT,
    size INTEGER, mtime REAL, risk TEXT, suggestion TEXT, command TEXT,
    evidence TEXT,
    first_seen REAL, last_seen REAL,
    status TEXT DEFAULT 'new',          -- new | keep | remove | review | done | gone
    note TEXT
);
CREATE INDEX IF NOT EXISTS findings_cat ON findings(category);

CREATE TABLE IF NOT EXISTS scans (
    scanner TEXT PRIMARY KEY, ts REAL, count INTEGER, seconds REAL, error TEXT
);

-- key/parent are norm_key() forms; path keeps the real casing for display.
CREATE TABLE IF NOT EXISTS dirs (
    key TEXT PRIMARY KEY, path TEXT, parent TEXT, root TEXT,
    size INTEGER, files INTEGER, mtime REAL, depth INTEGER
);
CREATE INDEX IF NOT EXISTS dirs_parent ON dirs(parent);

CREATE TABLE IF NOT EXISTS bigfiles (
    key TEXT PRIMARY KEY, path TEXT, parent TEXT, root TEXT, size INTEGER, mtime REAL
);

CREATE TABLE IF NOT EXISTS index_runs (
    root TEXT PRIMARY KEY, started REAL, finished REAL,
    size INTEGER, files INTEGER, dirs INTEGER, denied INTEGER, state TEXT
);

-- Your names for sensors and fan headers ("Top exhaust"), keyed by LibreHardwareMonitor sensor id.
CREATE TABLE IF NOT EXISTS hw_names (id TEXT PRIMARY KEY, label TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, action TEXT, target TEXT, size INTEGER,
    detail TEXT, reversed REAL
);
"""

_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


@dataclass
class Finding:
    scanner: str
    category: str
    title: str
    path: str | None = None
    size: int | None = None
    mtime: float | None = None
    risk: str = "medium"                # low | medium | high
    suggestion: str = ""
    command: str | None = None          # a command that performs the cleanup properly, if one exists
    evidence: dict = field(default_factory=dict)
    key: str | None = None              # identity override, e.g. one finding per package *version*

    @property
    def id(self) -> str:
        key = f"{self.scanner}|{self.category}|{(self.key or self.path or self.title).lower()}"
        return hashlib.sha1(key.encode()).hexdigest()[:10]


def _upsert(con: sqlite3.Connection, f: Finding, now: float) -> None:
    con.execute(
        """INSERT INTO findings (id, scanner, category, title, path, size, mtime, risk,
                                 suggestion, command, evidence, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, size=excluded.size, mtime=excluded.mtime,
             risk=excluded.risk, suggestion=excluded.suggestion, command=excluded.command,
             evidence=excluded.evidence, last_seen=excluded.last_seen,
             status=CASE WHEN findings.status='gone' THEN 'new' ELSE findings.status END""",
        (f.id, f.scanner, f.category, f.title, f.path, f.size, f.mtime, f.risk,
         f.suggestion, f.command, json.dumps(f.evidence, default=str), now, now),
    )


def add_findings(con: sqlite3.Connection, findings: list[Finding]) -> None:
    """Upsert findings that don't come from a scanner run (e.g. uninstall leftovers),
    without marking anything else as gone."""
    now = time.time()
    with _lock, con:
        for f in findings:
            _upsert(con, f, now)


def save_findings(con: sqlite3.Connection, scanner: str, findings: list[Finding], seconds: float) -> None:
    """Upsert this scanner's findings; ones not seen this run become 'gone'.
    User decisions (status/note) survive re-scans."""
    now = time.time()
    with _lock, con:
        for f in findings:
            _upsert(con, f, now)
        con.execute(
            "UPDATE findings SET status='gone' WHERE scanner=? AND last_seen<? AND status NOT IN ('done')",
            (scanner, now),
        )
        con.execute("INSERT OR REPLACE INTO scans VALUES (?,?,?,?,NULL)", (scanner, now, len(findings), seconds))


def record_scan_error(con: sqlite3.Connection, scanner: str, error: str) -> None:
    with _lock, con:
        con.execute("INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?)", (scanner, time.time(), 0, 0, error))


def row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    if "evidence" in d and d["evidence"]:
        d["evidence"] = json.loads(d["evidence"])
    return d
