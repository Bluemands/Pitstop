"""Sensor logs: sessions, samples, process snapshots and marks, in data/hw.sqlite.

Kept apart from pcc.sqlite on purpose: the UI reloads itself whenever pcc.sqlite changes, and a log
writes every second. Each sample is one row holding every sensor's value as packed float32 in the
session's column order (NaN = no reading), so a 4-hour log at 1 s is under 10 MB.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from array import array
from pathlib import Path

from .lhm import all_sensors

DB = Path(__file__).resolve().parents[2] / "data" / "hw.sqlite"
NAN = float("nan")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT, kind TEXT,                 -- kind: log (started and stopped) | recent (saved from the buffer)
    started REAL, ended REAL, max_s REAL, interval REAL,
    stop_reason TEXT, admin INTEGER, source TEXT,
    columns TEXT, meta TEXT,               -- sensor ids in sample order; per-sensor name/type/part/limits
    fan_config TEXT                        -- FanControl curves at the time, for before/after comparisons
);
CREATE TABLE IF NOT EXISTS samples (session INTEGER, ts REAL, vals BLOB);
CREATE INDEX IF NOT EXISTS samples_s ON samples(session, ts);
CREATE TABLE IF NOT EXISTS procs (session INTEGER, ts REAL, data TEXT);
CREATE INDEX IF NOT EXISTS procs_s ON procs(session, ts);
CREATE TABLE IF NOT EXISTS events (session INTEGER, ts REAL, kind TEXT, text TEXT);
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


# ---- columns and packing -------------------------------------------------------------------

def columns(snap: dict) -> tuple[list[str], dict]:
    """Every visible sensor, in a stable order, with what a report needs to describe it."""
    cols, meta = [], {}
    for h, s in all_sensors(snap):
        if s["hidden"] or s["id"] in meta:
            continue
        if not snap.get("admin") and h["type"] == "Cpu" and s["type"] != "Load":
            continue   # without admin these read as 0 or nothing
        cols.append(s["id"])
        meta[s["id"]] = {"name": s["name"], "type": s["type"], "part": h["name"], "part_type": h["type"],
                         **{k: s[k] for k in ("warn", "crit") if s.get(k) is not None}}
    return cols, meta


def pack(snap: dict, cols: list[str]) -> bytes:
    vals = {s["id"]: s["value"] for _, s in all_sensors(snap)}
    return array("f", [NAN if vals.get(c) is None else vals[c] for c in cols]).tobytes()


def unpack(blob: bytes) -> list[float | None]:
    a = array("f")
    a.frombytes(blob)
    return [None if math.isnan(v) else round(v, 2) for v in a]


# ---- sessions ------------------------------------------------------------------------------

def active(con) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM sessions WHERE ended IS NULL AND kind='log' ORDER BY id DESC LIMIT 1").fetchone()


def start(label: str, max_hours: float = 4, interval: float = 1.0) -> dict:
    con = connect()
    if (a := active(con)):
        return {"started": False, "session": a["id"], "note": f"Already logging “{a['label']}”."}
    from . import fancontrol
    fc = fancontrol.read()
    with con:
        cur = con.execute("INSERT INTO sessions (label, kind, started, max_s, interval, fan_config) "
                          "VALUES (?,?,?,?,?,?)",
                          (label or "Untitled log", "log", time.time(), max_hours * 3600, interval,
                           json.dumps({"controls": fc.get("controls"), "curves": fc.get("curves")})))
    return {"started": True, "session": cur.lastrowid, "label": label or "Untitled log"}


def stop(reason: str = "stopped") -> dict:
    con = connect()
    a = active(con)
    if not a:
        return {"stopped": False, "note": "Nothing is being logged."}
    with con:
        con.execute("UPDATE sessions SET ended=?, stop_reason=? WHERE id=?", (time.time(), reason, a["id"]))
    return {"stopped": True, "session": a["id"], "label": a["label"]}


def attach(con, row, cols, meta, snap) -> list[str]:
    """The monitor picks up a session: fill in its columns the first time. Returns the session's columns."""
    if row["columns"]:
        return json.loads(row["columns"])
    with con:
        con.execute("UPDATE sessions SET columns=?, meta=?, admin=?, source=? WHERE id=?",
                    (json.dumps(cols), json.dumps(meta), int(bool(snap.get("admin"))), snap.get("source"), row["id"]))
    return cols


def mark(text: str, ts: float | None = None, session: int | None = None) -> dict:
    con = connect()
    sid = session or (active(con) or {"id": None})["id"]
    if not sid:
        return {"marked": False, "note": "Nothing is being logged. Start a log, or save the last few minutes."}
    with con:
        con.execute("INSERT INTO events VALUES (?,?,?,?)", (sid, ts or time.time(), "mark", text or "Marked"))
    return {"marked": True, "session": sid}


def save_recent(con, ring, procs_ring, cols, meta, snap, label: str, note: str | None, ts: float) -> int:
    """Write the monitor's in-memory buffer (the last few minutes) as a finished session."""
    if not ring:
        return 0
    with con:
        cur = con.execute("INSERT INTO sessions (label, kind, started, ended, interval, stop_reason, admin, source, "
                          "columns, meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (label or "Saved moment", "recent", ring[0][0], ring[-1][0], None, "saved from buffer",
                           int(bool(snap.get("admin"))), snap.get("source"), json.dumps(cols), json.dumps(meta)))
        sid = cur.lastrowid
        con.executemany("INSERT INTO samples VALUES (?,?,?)", [(sid, t, v) for t, v in ring])
        con.executemany("INSERT INTO procs VALUES (?,?,?)", [(sid, t, json.dumps(p)) for t, p in procs_ring
                                                              if t >= ring[0][0]])
        if note:
            con.execute("INSERT INTO events VALUES (?,?,?,?)", (sid, ts, "mark", note))
    return sid


def sessions(limit: int = 30) -> list[dict]:
    con = connect()
    out = []
    for r in con.execute("SELECT s.*, (SELECT COUNT(*) FROM samples WHERE session=s.id) n, "
                         "(SELECT COUNT(*) FROM events WHERE session=s.id AND kind='mark') marks "
                         "FROM sessions s ORDER BY id DESC LIMIT ?", (limit,)):
        end = r["ended"] or time.time()
        out.append({"id": r["id"], "label": r["label"], "kind": r["kind"], "started": r["started"],
                    "ended": r["ended"], "active": r["ended"] is None and r["kind"] == "log",
                    "minutes": round((end - r["started"]) / 60, 1), "samples": r["n"], "marks": r["marks"],
                    "admin": bool(r["admin"]) if r["admin"] is not None else None, "stop_reason": r["stop_reason"]})
    return out


def delete(session: int) -> dict:
    con = connect()
    with con:
        for t in ("samples", "procs", "events"):
            con.execute(f"DELETE FROM {t} WHERE session=?", (session,))
        n = con.execute("DELETE FROM sessions WHERE id=? AND ended IS NOT NULL", (session,)).rowcount
    return {"deleted": bool(n), "session": session}


def load(session: int | None = None) -> dict | None:
    """A session with all its samples decoded: {meta row, cols, meta, ts[], rows[][], procs[], events[]}."""
    con = connect()
    row = (con.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone() if session
           else con.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone())
    if not row or not row["columns"]:
        return {"session": dict(row) if row else None, "empty": True} if row else None
    ts, rows = [], []
    for r in con.execute("SELECT ts, vals FROM samples WHERE session=? ORDER BY ts", (row["id"],)):
        ts.append(r["ts"])
        rows.append(unpack(r["vals"]))
    return {"session": dict(row), "cols": json.loads(row["columns"]), "meta": json.loads(row["meta"]),
            "ts": ts, "rows": rows,
            "procs": [(r["ts"], json.loads(r["data"])) for r in
                      con.execute("SELECT ts, data FROM procs WHERE session=? ORDER BY ts", (row["id"],))],
            "events": [dict(r) for r in con.execute("SELECT ts, kind, text FROM events WHERE session=? ORDER BY ts",
                                                    (row["id"],))]}
