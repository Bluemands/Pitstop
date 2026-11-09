"""Read-side helpers shared by the CLI and the MCP server."""
from __future__ import annotations

import os
import shutil
import string
import time

from .store import _lock, connect, row_to_dict
from .util import age_days, human, norm_key

STATUSES = ("new", "keep", "remove", "review", "done", "gone")


def exclusive_sizes(rows: list[dict]) -> dict[str, int]:
    """{finding id: bytes not already counted by a finding nested inside it}.

    The Steam folder and a game inside it are both findings; the game keeps its
    size and Steam is credited only with the rest. Same-path duplicates count once.
    """
    # On identical paths the specific finding (a game, an SDK) beats the generic "program".
    with_path = sorted((r for r in rows if r.get("path") and r["path"][1:3] == ":\\"),
                       key=lambda r: (norm_key(r["path"]), r["scanner"] == "programs"))
    out: dict[str, int] = {}
    stack: list[tuple[str, str]] = []   # (key, id) of enclosing findings
    seen: set[str] = set()
    for r in with_path:
        k = norm_key(r["path"])
        if k in seen:
            continue
        seen.add(k)
        while stack and not k.startswith(stack[-1][0] + "\\"):
            stack.pop()
        size = r["size"] or 0
        out[r["id"]] = size
        if stack:
            pid = stack[-1][1]
            out[pid] = max(0, out[pid] - size)
        stack.append((k, r["id"]))
    return out


def list_findings(category: str | None = None, scanner: str | None = None, status: str | None = None,
                  min_mb: float = 0, search: str | None = None, limit: int = 50, offset: int = 0,
                  order: str = "size", drive: str | None = None) -> list[dict]:
    sql = "SELECT * FROM findings WHERE status != 'gone'"
    args: list = []
    if drive:
        # Findings that aren't on a drive (updates, PATH entries, registry keys) show in every drive view,
        # matching how summary() counts them.
        sql += " AND (path IS NULL OR substr(path, 2, 2) != ':\\' OR path LIKE ?)"
        args.append(drive[0] + ":%")
    for col, val in (("category", category), ("scanner", scanner), ("status", status)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    if min_mb:
        sql += " AND size >= ?"
        args.append(int(min_mb * 1024 * 1024))
    if search:
        sql += " AND (title LIKE ? OR path LIKE ?)"
        args += [f"%{search}%"] * 2
    sql += {"size": " ORDER BY size DESC", "age": " ORDER BY mtime ASC", "title": " ORDER BY title"}[order]
    sql += " LIMIT ? OFFSET ?"
    args += [limit, offset]
    rows = [row_to_dict(r) for r in connect().execute(sql, args)]
    if not category and not scanner:
        # A game or SDK is usually also an Apps & Features entry at the same path; show it once.
        specific = {norm_key(r["path"]) for r in rows if r["scanner"] != "programs" and r["path"]}
        rows = [r for r in rows if not (r["scanner"] == "programs" and r["path"] and norm_key(r["path"]) in specific)]
    return [compact(r) for r in rows]


def compact(f: dict) -> dict:
    """Trim a finding for listing: what Claude needs to triage, not every field."""
    ev = f.get("evidence") or {}
    out = {"id": f["id"], "category": f["category"], "title": f["title"], "path": f["path"],
           "size": human(f["size"]), "size_bytes": f["size"] or 0, "risk": f["risk"], "status": f["status"]}
    if f.get("mtime"):
        out["last_modified_days_ago"] = age_days(f["mtime"])
    for k in ("last_launch_days_ago", "last_played_days_ago", "install_date", "publisher",
              "program", "kind", "pending_reboot", "installed", "available", "tags", "end_of_support",
              "location", "ram_mb", "running", "hardware", "protected", "duplicate_of", "class", "problem"):
        if ev.get(k) is not None:
            out[k] = ev[k]
    if f.get("note"):
        out["note"] = f["note"]
    return out


def get_finding(fid: str) -> dict | None:
    r = connect().execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not r:
        return None
    d = row_to_dict(r)
    d["size_human"] = human(d["size"])
    return d


def decide(ids: list[str], status: str, note: str | None = None) -> int:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    con = connect()
    with _lock, con:
        n = 0
        for i in ids:
            n += con.execute("UPDATE findings SET status=?, note=COALESCE(?, note) WHERE id=?",
                             (status, note, i)).rowcount
    return n


def drives() -> list[dict]:
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            try:
                u = shutil.disk_usage(root)
            except OSError:
                continue
            out.append({"drive": root, "total": human(u.total), "used": human(u.used), "free": human(u.free),
                        "free_pct": round(100 * u.free / u.total, 1),
                        "total_bytes": u.total, "free_bytes": u.free})
    return out


def summary(drive: str | None = None) -> dict:
    con = connect()
    # 'done' findings keep their pre-cleanup size for the record; they're no longer on disk.
    rows = [row_to_dict(r) for r in con.execute("SELECT * FROM findings WHERE status NOT IN ('gone', 'done')")]
    if drive:
        # Path-less findings (PATH entries, registry) aren't on any drive; keep them in every view.
        rows = [r for r in rows if not (r["path"] or "")[1:3] == ":\\" or r["path"][0].upper() == drive[0].upper()]
    by_cat: dict[str, dict] = {}
    excl = exclusive_sizes(rows)
    for r in rows:
        c = by_cat.setdefault(r["category"], {"count": 0, "bytes": 0, "scanner": r["scanner"]})
        c["count"] += 1
        c["bytes"] += excl.get(r["id"], 0)
    cats = sorted(({"category": k, **v, "size": human(v["bytes"])} for k, v in by_cat.items()),
                  key=lambda x: -x["bytes"])
    marked = [r for r in rows if r["status"] == "remove"]
    scans = {r["scanner"]: {"age_hours": round((time.time() - r["ts"]) / 3600, 1), "count": r["count"],
                            "error": r["error"]} for r in con.execute("SELECT * FROM scans")}
    return {
        "drives": drives(),
        "categories": cats,
        "note": "A folder containing other findings is credited only with what they don't account for.",
        "marked_for_removal": {"count": len(marked), "bytes": sum(exclusive_sizes(marked).values()),
                               "size": human(sum(exclusive_sizes(marked).values()))},
        "decisions": {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM findings WHERE status != 'gone' GROUP BY status")},
        "scans": scans,
    }
