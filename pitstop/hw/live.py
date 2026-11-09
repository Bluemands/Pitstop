"""Live readings: the elevated background monitor, and how everything else gets the latest snapshot.

The UI and Claude's shell run without admin, which only sees GPUs, drives and CPU load. Full
readings need admin, so `pitstop hw monitor` runs elevated (one UAC prompt), reads every second and
writes the latest snapshot to data/hw-live.json. It stops by itself once nobody has asked for a
reading for a while: everything that reads the file touches data/hw-wanted first.

Readings go to a file, not the database: the UI reloads itself whenever the database changes, and a
write every second would make it reload constantly.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from pathlib import Path

from ..store import _lock, connect
from .lhm import Reader, SensorError

DATA = Path(__file__).resolve().parents[2] / "data"
LIVE = DATA / "hw-live.json"
WANTED = DATA / "hw-wanted"
SAVE_REQ = DATA / "hw-save.json"        # "save the last few minutes": written by the UI/CLI, consumed by the monitor
SAVE_DONE = DATA / "hw-saved.json"
STOP_REQ = DATA / "hw-stop"             # asks the running reader to exit (an admin one is taking over)
FRESH_S = 4.0
IDLE_EXIT_S = 120.0
BUFFER_S = 300.0                         # what "save the last 5 minutes" can save
PROCS_EVERY_S = 5.0
# Local\ (this sign-in session) works for elevated and unelevated processes alike; creating a Global\ object
# needs a privilege unelevated processes don't have.
MUTEX = "Local\\Pitstop-hw-monitor"

_direct: dict = {"reader": None, "error": None}
_direct_lock = threading.Lock()


# ---- the monitor ---------------------------------------------------------------------------

def _single_instance():
    """A named mutex: a second monitor exits straight away. Returns the handle (keep it alive)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    h = k32.CreateMutexW(None, False, MUTEX)
    err = ctypes.get_last_error()
    if err == 183 or (not h and err == 5):  # ERROR_ALREADY_EXISTS, or an elevated one owns it
        return None
    return h or True


def want() -> None:
    """Ask the monitor to keep running (it exits IDLE_EXIT_S after the last ask)."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        WANTED.touch()
    except OSError:
        pass


def _write_atomic(p: Path, text: str) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(20):  # a reader may have the file open for a moment
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.02)


def request_save(label: str = "", note: str = "") -> dict:
    """Ask the running monitor to save its buffer (the last few minutes) as a log session."""
    if not monitor_snapshot():
        return {"saved": False, "note": "The background reader isn't running, so there's nothing buffered. "
                                        "Open Tune › Sensors with all sensors on, or start a log."}
    _write_atomic(SAVE_REQ, json.dumps({"label": label, "note": note, "ts": time.time()}))
    for _ in range(30):  # the monitor picks it up within a second
        time.sleep(0.2)
        if not SAVE_REQ.exists():
            try:
                return json.loads(SAVE_DONE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
    return {"saved": False, "note": "The background reader didn't respond."}


def monitor(interval: float = 1.0, idle_exit: float = IDLE_EXIT_S) -> dict:
    """Read every `interval` s: publish the latest reading, keep the last BUFFER_S in memory, append to the
    active log session if there is one, and save the buffer when asked. Exits when there's no active log
    and nobody has asked for a reading for `idle_exit` s."""
    from collections import deque
    from . import logdb
    from .procs import Sampler
    handle = _single_instance()
    if handle is None:
        return {"started": False, "note": "A monitor is already running."}
    STOP_REQ.unlink(missing_ok=True)   # left over from a reader that exited before seeing it
    want()
    reader = Reader()
    db = logdb.connect()
    sampler = Sampler()
    first = reader.read()
    cols, meta = logdb.columns(first)
    ring: deque = deque(maxlen=int(BUFFER_S / interval))
    procs_ring: deque = deque(maxlen=int(BUFFER_S / PROCS_EVERY_S) + 2)
    session, session_cols, last_procs, n = None, cols, 0.0, 0
    try:
        while True:
            t0 = time.monotonic()
            snap, first = (first or reader.read()), None
            ts = snap["ts"]
            _write_atomic(LIVE, json.dumps(snap | {"pid": os.getpid(), "interval": interval,
                                                    "logging": session}, separators=(",", ":")))
            n += 1
            ring.append((ts, logdb.pack(snap, cols)))
            new_procs = None
            if ts - last_procs >= PROCS_EVERY_S:
                new_procs, last_procs = sampler.sample(), ts
                procs_ring.append((ts, new_procs))
            row = logdb.active(db)
            if row:
                if row["id"] != session:
                    session = row["id"]
                    session_cols = logdb.attach(db, row, cols, meta, snap)
                with db:
                    db.execute("INSERT INTO samples VALUES (?,?,?)", (session, ts,
                               ring[-1][1] if session_cols == cols else logdb.pack(snap, session_cols)))
                    if new_procs:
                        db.execute("INSERT INTO procs VALUES (?,?,?)", (session, ts, json.dumps(new_procs)))
                if row["max_s"] and ts - row["started"] > row["max_s"]:
                    logdb.stop("time limit reached")
            else:
                session = None
            if SAVE_REQ.exists():
                try:
                    req = json.loads(SAVE_REQ.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    req = {}
                sid = logdb.save_recent(db, list(ring), list(procs_ring), cols, meta, snap,
                                        req.get("label", ""), req.get("note"), req.get("ts", ts))
                _write_atomic(SAVE_DONE, json.dumps({"saved": bool(sid), "session": sid,
                                                     "seconds": round(ring[-1][0] - ring[0][0]) if ring else 0}))
                SAVE_REQ.unlink(missing_ok=True)
            try:
                idle = time.time() - WANTED.stat().st_mtime
            except OSError:
                idle = idle_exit + 1
            if session is None and idle > idle_exit:
                break
            if STOP_REQ.exists():   # handing over to another reader; a running log carries on with it
                STOP_REQ.unlink(missing_ok=True)
                break
            time.sleep(max(0.05, interval - (time.monotonic() - t0)))
    finally:
        reader.close()
    return {"started": True, "readings": n, "stopped": f"no log running and nobody asked for readings for "
                                                       f"{idle_exit:.0f} s"}


def monitor_snapshot() -> dict | None:
    """The monitor's latest reading if it's fresh, else None."""
    try:
        snap = json.loads(LIVE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return snap if time.time() - snap.get("ts", 0) < FRESH_S else None


def start_log(label: str, max_hours: float = 4, admin: bool = True) -> dict:
    """Create a log session and make sure the background reader is running to fill it."""
    from . import logdb
    from ..elevate import ElevationError
    r = logdb.start(label, max_hours)
    if not r["started"]:
        return r
    try:
        m = start_monitor(admin)
    except ElevationError as e:
        logdb.stop("the admin prompt was declined")
        return {"started": False, "error": str(e)}
    snap = None
    for _ in range(40):          # wait for the reader to pick the session up
        snap = monitor_snapshot()
        if snap and snap.get("logging") == r["session"]:
            break
        time.sleep(0.25)
    return r | {"reader": "already running" if not m.get("started") else "started",
                "all_sensors": bool(snap and snap.get("admin")),
                "note": "Logging every second. Stop with `pitstop hw log stop`; mark noisy moments with "
                        "`pitstop hw log mark \"...\"`." + ("" if snap and snap.get("admin") else
                                                       " Without admin: no CPU temperatures or motherboard fans.")}


def start_monitor(admin: bool = True) -> dict:
    """Start the monitor (elevated: one UAC prompt, unless admin=False) and return without waiting for it."""
    from ..elevate import is_admin, run_elevated
    import subprocess
    import sys
    want()
    running = monitor_snapshot()
    if running and (running.get("admin") or not admin):
        return {"started": False, "note": "Already running."}
    if running:   # a limited (no-admin) reader is running: ask it to hand over to an admin one
        STOP_REQ.touch()
        for _ in range(40):
            time.sleep(0.25)
            if not STOP_REQ.exists():
                break
        time.sleep(0.3)
    exe = sys.executable.replace("pythonw.exe", "python.exe")
    root = str(Path(__file__).resolve().parents[2])
    if is_admin() or not admin:
        subprocess.Popen([exe, "-m", "pitstop", "hw", "monitor"], cwd=root,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        run_elevated(exe, "-m pitstop hw monitor", cwd=root, timeout_s=0, show=0)  # raises if UAC declined
    return {"started": True}


# ---- getting a reading ---------------------------------------------------------------------

def snapshot(direct: bool = True) -> dict:
    """Latest readings: the monitor's if it's running, else read here (without admin, unless this
    process is elevated). Always says which it was."""
    want()
    snap = monitor_snapshot()
    if snap:
        _close_direct()
        return snap | {"via": "monitor"}
    if not direct:
        return {"error": "The monitor isn't running."}
    with _direct_lock:
        if _direct["reader"] is None:
            try:
                _direct["reader"] = Reader()
            except SensorError as e:
                return {"error": str(e)}
            _direct["reader"].read()   # load and drive activity need two samples
            time.sleep(0.5)
        return _direct["reader"].read() | {"via": "direct"}


def _close_direct() -> None:
    with _direct_lock:
        if _direct["reader"] is not None:
            _direct["reader"].close()
            _direct["reader"] = None


# ---- names ---------------------------------------------------------------------------------

def names() -> dict:
    return {r["id"]: r["label"] for r in connect().execute("SELECT id, label FROM hw_names")}


def set_name(sensor_id: str, label: str) -> dict:
    """Name a sensor. Naming a motherboard fan header names its RPM and its control % together."""
    import re
    ids = [sensor_id]
    m = re.match(r"^(/lpc/.+)/(fan|control)/(\d+)$", sensor_id)
    if m:
        ids = [f"{m.group(1)}/fan/{m.group(3)}", f"{m.group(1)}/control/{m.group(3)}"]
    con = connect()
    with _lock, con:
        for i in ids:
            if label.strip():
                con.execute("INSERT OR REPLACE INTO hw_names VALUES (?,?,?)", (i, label.strip(), time.time()))
            else:
                con.execute("DELETE FROM hw_names WHERE id=?", (i,))
    return {"named": ids, "label": label.strip() or None}
