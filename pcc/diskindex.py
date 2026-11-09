"""Walk a drive once, keep per-folder totals in SQLite, answer drill-downs instantly.

Only folders >= DIR_MIN and files >= FILE_MIN are stored; everything smaller is
still counted in its parent's total, so `du` reports the remainder as "other".
"""
from __future__ import annotations

import os
import sys
import threading
import time

from .store import _lock, connect
from .util import CLOUD_ONLY, REPARSE, norm_key

DIR_MIN = 50 * 1024 * 1024
FILE_MIN = 256 * 1024 * 1024

_status: dict[str, dict] = {}   # root key -> live progress for background runs


def build(root: str, progress=None) -> dict:
    root = os.path.abspath(root)
    rk = norm_key(root)
    con = connect()
    started = time.time()
    st = _status[rk] = {"root": root, "state": "running", "started": started, "files": 0, "dirs": 0,
                        "bytes": 0, "denied": 0, "current": root}
    dirs: list[tuple] = []
    bigfiles: list[tuple] = []
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    last_report = [0.0]

    def walk(path: str, depth: int) -> tuple[int, int, float]:
        size = files = 0
        newest = 0.0
        try:
            it = os.scandir(path)
        except OSError:
            st["denied"] += 1
            return 0, 0, 0.0
        subdirs = []
        with it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                attrs = getattr(s, "st_file_attributes", 0)
                if attrs & REPARSE:
                    continue
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(e.path)
                    continue
                if attrs & CLOUD_ONLY:
                    continue
                size += s.st_size
                files += 1
                newest = max(newest, s.st_mtime)
                if s.st_size >= FILE_MIN:
                    bigfiles.append((norm_key(e.path), e.path, norm_key(path), rk, s.st_size, s.st_mtime))
        st["files"] += files
        st["bytes"] += size
        for sd in subdirs:
            a, b, c = walk(sd, depth + 1)
            size += a
            files += b
            newest = max(newest, c)
        st["dirs"] += 1
        if size >= DIR_MIN or depth == 0:
            dirs.append((norm_key(path), path, norm_key(os.path.dirname(path)) if depth else None,
                         rk, size, files, newest, depth))
        now = time.monotonic()
        if now - last_report[0] > 2:
            last_report[0] = now
            st["current"] = path
            if progress:
                progress(st)
        return size, files, newest

    try:
        total, nfiles, _ = walk(root, 0)
    except Exception as e:
        st["state"] = f"error: {e}"
        raise
    with _lock, con:
        con.execute("DELETE FROM dirs WHERE root=?", (rk,))
        con.execute("DELETE FROM bigfiles WHERE root=?", (rk,))
        con.executemany("INSERT OR REPLACE INTO dirs VALUES (?,?,?,?,?,?,?,?)", dirs)
        con.executemany("INSERT OR REPLACE INTO bigfiles VALUES (?,?,?,?,?,?)", bigfiles)
        con.execute("INSERT OR REPLACE INTO index_runs VALUES (?,?,?,?,?,?,?,?)",
                    (rk, started, time.time(), total, nfiles, st["dirs"], st["denied"], "done"))
    st.update(state="done", bytes=total, files=nfiles, seconds=round(time.time() - started, 1))
    return st


def build_in_background(root: str) -> dict:
    rk = norm_key(os.path.abspath(root))
    if _status.get(rk, {}).get("state") == "running":
        return _status[rk]
    threading.Thread(target=build, args=(root,), daemon=True).start()
    time.sleep(0.2)
    return _status.get(rk, {"state": "starting"})


def status() -> dict:
    con = connect()
    runs = {r["root"]: dict(r) for r in con.execute("SELECT * FROM index_runs")}
    for k, s in _status.items():
        if s.get("state") == "running":
            runs[k] = dict(s)
    return runs


def du(path: str, top: int = 25) -> dict:
    """Children of `path` from the index, biggest first, plus the unindexed remainder."""
    con = connect()
    k = norm_key(path)
    me = con.execute("SELECT * FROM dirs WHERE key=?", (k,)).fetchone()
    if not me:
        return {"error": f"{path} is not in the index (not indexed yet, or smaller than {DIR_MIN // 2**20} MB)."}
    kids = [dict(r) for r in con.execute(
        "SELECT path, size, files, mtime FROM dirs WHERE parent=? ORDER BY size DESC", (k,))]
    files = [dict(r) for r in con.execute(
        "SELECT path, size, mtime FROM bigfiles WHERE parent=? ORDER BY size DESC", (k,))]
    shown = sum(c["size"] for c in kids) + sum(f["size"] for f in files)
    return {"path": me["path"], "size": me["size"], "files": me["files"], "newest_mtime": me["mtime"],
            "children": kids[:top], "big_files": files[:top],
            "other_bytes": me["size"] - shown, "more_children": max(0, len(kids) - top)}


def largest_files(root: str | None = None, top: int = 50) -> list[dict]:
    con = connect()
    if root:
        k = norm_key(root)
        rows = con.execute("SELECT path, size, mtime FROM bigfiles WHERE key LIKE ? ORDER BY size DESC LIMIT ?",
                           (k + "\\%", top))
    else:
        rows = con.execute("SELECT path, size, mtime FROM bigfiles ORDER BY size DESC LIMIT ?", (top,))
    return [dict(r) for r in rows]
