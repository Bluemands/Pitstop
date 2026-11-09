"""Everything that changes the machine. Every action is journalled and reversible
until purged.

Quarantine = rename into <drive>:\\Pitstop-Quarantine on the same volume. That is
instant and fully restorable, but frees nothing until `purge`.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import winreg
from pathlib import Path

from . import footprint
from . import inventory as inv
from .elevate import ElevationError, run_elevated
from .scanners.envvars import SCOPES, _canon, read_env
from .store import DB_PATH, _lock, add_findings, connect
from .util import dir_size, human, norm_key, protection_reason, run

QUARANTINE_DIR = "Pitstop-Quarantine"
BACKUP_DIR = DB_PATH.parent / "backups"


class ActionError(Exception):
    pass


def _journal(action: str, target: str, size: int, detail: dict) -> int:
    con = connect()
    with _lock, con:
        cur = con.execute("INSERT INTO journal (ts, action, target, size, detail) VALUES (?,?,?,?,?)",
                          (time.time(), action, target, size, json.dumps(detail)))
        return cur.lastrowid


def _journal_update(jid: int, **changes) -> dict:
    con = connect()
    r = con.execute("SELECT detail FROM journal WHERE id=?", (jid,)).fetchone()
    d = json.loads(r["detail"]) | changes
    with _lock, con:
        con.execute("UPDATE journal SET detail=? WHERE id=?", (json.dumps(d), jid))
    return d


def _mark_finding(finding_id: str | None, status: str) -> None:
    if finding_id:
        con = connect()
        with _lock, con:
            con.execute("UPDATE findings SET status=? WHERE id=?", (status, finding_id))


def _mark_by_path(scanner: str, path: str, status: str) -> None:
    """For actions that know what they changed but not which finding it was (PATH edits)."""
    con = connect()
    with _lock, con:
        con.execute("UPDATE findings SET status=? WHERE scanner=? AND path=? AND status != 'gone'",
                    (status, scanner, path))


def resolve_target(target: str) -> tuple[str, str | None]:
    """Accept a finding id or a path; return (path, finding_id)."""
    if re.fullmatch(r"[0-9a-f]{10}", target):
        r = connect().execute("SELECT path FROM findings WHERE id=?", (target,)).fetchone()
        if not r or not r["path"]:
            raise ActionError(f"No finding {target} with a path")
        return r["path"], target
    return target, None


# ---- quarantine --------------------------------------------------------------

def plan_quarantine(target: str, reserved: set[str] | None = None) -> dict:
    """What quarantining `target` would do, without doing it.

    A dry run over several targets moves nothing, so same-named targets would all be
    planned onto one free dest. Pass the same `reserved` set for each target in the
    batch and each plan claims its dest in it.
    """
    path, fid = resolve_target(target)
    p = Path(path)
    if not p.exists():
        raise ActionError(f"{path} does not exist")
    if why := protection_reason(p):
        raise ActionError(f"Refusing {path}: {why}")
    if norm_key(p).startswith(norm_key(Path(__file__).resolve().parent.parent)):
        raise ActionError("Refusing to quarantine Pitstop itself")
    size = dir_size(p, budget_s=30)[0] if p.is_dir() else p.stat().st_size
    drive = p.anchor
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^\w.\- ]", "_", p.name)[:80]
    dest = Path(drive) / QUARANTINE_DIR / f"{stamp}_{safe}"
    taken = reserved if reserved is not None else set()
    n = 2
    # two same-named folders (e.g. "Adobe" from x64 and x86) in one second
    while dest.exists() or str(dest).lower() in taken:
        dest = Path(drive) / QUARANTINE_DIR / f"{stamp}_{safe}_{n}"
        n += 1
    taken.add(str(dest).lower())
    return {"path": str(p), "finding_id": fid, "size": size, "dest": str(dest)}


def quarantine(target: str, reason: str = "") -> dict:
    plan = plan_quarantine(target)
    dest = Path(plan["dest"])
    dest.parent.mkdir(exist_ok=True)
    readme = dest.parent / "README.txt"
    if not readme.exists():
        readme.write_text("Folders moved here by Pitstop. Restore with `python -m pitstop restore <id>`; "
                          "free the space with `python -m pitstop purge`.\n")
    try:
        os.rename(plan["path"], dest)
    except PermissionError as e:
        raise ActionError(f"Could not move {plan['path']}: in use or access denied ({e.strerror}). "
                          "Close the program using it, or run elevated.") from e
    except OSError as e:
        raise ActionError(f"Could not move {plan['path']}: {e}") from e
    # Sidecar so the quarantine is self-describing even without the database.
    Path(str(dest) + ".pitstop.json").write_text(json.dumps({"original": plan["path"], "reason": reason,
                                                         "ts": time.time(), "size": plan["size"]}))
    jid = _journal("quarantine", plan["path"], plan["size"],
                   {"dest": str(dest), "finding_id": plan["finding_id"], "reason": reason})
    _mark_finding(plan["finding_id"], "done")
    return {**plan, "journal_id": jid}


def quarantined() -> list[dict]:
    """Quarantined folders, and exported copies of removed driver packages (both freed on purge)."""
    rows = connect().execute(
        "SELECT * FROM journal WHERE action IN ('quarantine', 'driver-delete') AND reversed IS NULL ORDER BY ts DESC")
    out = []
    for r in rows:
        d = json.loads(r["detail"])
        if d.get("purged"):
            continue
        out.append({"journal_id": r["id"], "original": r["target"], "dest": d["dest"], "size": r["size"],
                    "age_days": round((time.time() - r["ts"]) / 86400, 1), "reason": d.get("reason")})
    return out


def restore(journal_id: int) -> dict:
    con = connect()
    r = con.execute("SELECT * FROM journal WHERE id=?", (journal_id,)).fetchone()
    if not r or r["reversed"]:
        raise ActionError(f"Journal entry {journal_id} not found or already reversed")
    d = json.loads(r["detail"])
    if r["action"] == "quarantine":
        if d.get("purged"):
            raise ActionError("Already purged; cannot restore")
        if os.path.exists(r["target"]):
            raise ActionError(f"{r['target']} exists again; move it aside first")
        os.makedirs(os.path.dirname(r["target"]), exist_ok=True)
        os.rename(d["dest"], r["target"])
        try:
            os.remove(d["dest"] + ".pitstop.json")
        except OSError:
            pass
        _mark_finding(d.get("finding_id"), "new")
    elif r["action"] == "env-set":
        _write_env(d["scope"], d["name"], d["old"], d["type"])
        kept = set((d.get("new") or "").split(";"))
        for e in (d.get("old") or "").split(";"):
            if e and e not in kept:
                _mark_by_path("envvars", f"{d['scope']}:PATH:{e}", "new")
        if d.get("why") == "dedupe":
            _mark_by_path("envvars", f"{d['scope']}:PATH:duplicates", "new")
    elif r["action"] == "reg-delete":
        subprocess.run(["reg", "import", d["backup"]], check=True, capture_output=True)
        _mark_finding(d.get("finding_id"), "new")
    elif r["action"] == "reg-value-delete":
        data = bytes.fromhex(d["hex"]) if d.get("hex") is not None else d["data"]
        hive, sub = footprint._split_key(d["key"])
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
                winreg.SetValueEx(k, d["value"], 0, d["type"], data)
        except PermissionError as e:
            raise ActionError("Restoring an HKLM value needs an elevated terminal (add --elevate)") from e
        _mark_finding(d.get("finding_id"), "new")
    elif r["action"] == "task-delete":
        res = subprocess.run(["schtasks", "/create", "/tn", d["task"], "/xml", d["backup"], "/f"],
                             capture_output=True, text=True)
        if res.returncode:
            raise ActionError(f"Could not re-create task (may need --elevate): {res.stderr.strip() or res.stdout.strip()}")
        _mark_finding(d.get("finding_id"), "new")
    elif r["action"] == "uninstall":
        raise ActionError("An uninstall can't be reversed from here; reinstall the program. "
                          "Its leftovers are restorable individually from quarantine.")
    elif r["action"] == "driver-delete":
        if d.get("purged"):
            raise ActionError("Already purged; the driver package can't be restored")
        inf = next(Path(d["dest"]).rglob(d.get("original") or "*.inf"), None) or next(Path(d["dest"]).rglob("*.inf"), None)
        if not inf:
            raise ActionError(f"No .inf in {d['dest']}")
        res = subprocess.run(["pnputil", "/add-driver", str(inf)], capture_output=True, text=True)
        if res.returncode:
            raise ActionError(f"pnputil /add-driver failed (needs --elevate): {(res.stdout or res.stderr).strip()[-300:]}")
        _force_rmtree(d["dest"])
        _mark_finding(d.get("finding_id"), "new")
    elif r["action"] == "restore-point":
        raise ActionError("A restore point is a safety net, not a change; there's nothing to undo.")
    elif r["action"] == "app-update":
        raise ActionError(f"Updates can't be rolled back automatically. If winget still has the old version: "
                          f"winget install --id {r['target']} --version {d['from']} --force")
    elif r["action"] == "startup-flag":
        hive, sub = footprint._split_key(d["key"])
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
                if d.get("old_hex") is None:
                    try:
                        winreg.DeleteValue(k, d["value"])
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(k, d["value"], 0, winreg.REG_BINARY, bytes.fromhex(d["old_hex"]))
        except PermissionError as e:
            raise ActionError("All-users startup entries need an elevated terminal (add --elevate)") from e
        _mark_finding(d.get("finding_id"), "new")
    elif r["action"] == "task-disable":
        res = subprocess.run(["schtasks", "/change", "/tn", d["task"], "/enable"], capture_output=True, text=True)
        if res.returncode:
            raise ActionError(f"Could not re-enable (may need --elevate): {res.stderr.strip() or res.stdout.strip()}")
        _mark_finding(d.get("finding_id"), "new")
    elif r["action"] == "service-start":
        mode = "delayed-auto" if d.get("old_start") == 2 and d.get("delayed") else _SERVICE_START.get(d.get("old_start"), "auto")
        res = subprocess.run(["sc.exe", "config", d["service"], "start=", mode], capture_output=True, text=True)
        if res.returncode:
            raise ActionError(f"Could not restore {d['service']} (needs --elevate): {res.stdout.strip()}")
        _mark_finding(d.get("finding_id"), "new")
    elif r["action"] == "winget-pin":
        code, tail = _winget(["pin", "remove", "--id", r["target"], "--exact"], timeout=120)
        if code:
            raise ActionError(f"winget pin remove failed: {tail}")
        _mark_finding(d.get("finding_id"), "new")
    else:
        raise ActionError(f"Don't know how to reverse {r['action']}")
    with _lock, con:
        con.execute("UPDATE journal SET reversed=? WHERE id=?", (time.time(), journal_id))
    return {"restored": journal_id, "action": r["action"], "target": r["target"]}


def _force_rmtree(path: str) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=onerror)
    elif os.path.exists(path):
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def purge(journal_ids: list[int] | None = None, older_than_days: float | None = None) -> dict:
    """Permanently delete quarantined items. Irreversible."""
    con = connect()
    freed, done, failed = 0, [], []
    for q in quarantined():
        if journal_ids is not None and q["journal_id"] not in journal_ids:
            continue
        if older_than_days is not None and q["age_days"] < older_than_days:
            continue
        err = None
        try:
            _force_rmtree(q["dest"])
        except OSError as e:  # one locked/admin-owned item must not abort the rest
            err = e.strerror or str(e)
        if os.path.exists(q["dest"]):
            failed.append({"journal_id": q["journal_id"], "dest": q["dest"],
                           "error": err or "some files could not be deleted (in use, or needs an elevated terminal)"})
            continue
        try:
            os.remove(q["dest"] + ".pitstop.json")
        except OSError:
            pass
        r = con.execute("SELECT detail FROM journal WHERE id=?", (q["journal_id"],)).fetchone()
        d = json.loads(r["detail"]) | {"purged": time.time()}
        with _lock, con:
            con.execute("UPDATE journal SET detail=? WHERE id=?", (json.dumps(d), q["journal_id"]))
        freed += q["size"]
        done.append(q["journal_id"])
    return {"purged": done, "freed_bytes": freed, "failed": failed}


# ---- environment variables ---------------------------------------------------

def _read_raw(scope: str, name: str) -> tuple[str, int]:
    hive, sub = SCOPES[scope]
    with winreg.OpenKey(hive, sub) as k:
        return winreg.QueryValueEx(k, name)


def _write_env(scope: str, name: str, value: str, typ: int) -> None:
    hive, sub = SCOPES[scope]
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, typ, value)
    except PermissionError as e:
        raise ActionError(f"Writing {scope} environment needs an elevated (admin) terminal") from e
    # Tell running apps (Explorer etc.) the environment changed.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)


def _set_path(scope: str, new_entries: list[str], why: str) -> dict:
    old, typ = _read_raw(scope, "Path")
    new = ";".join(new_entries)
    if new == old:
        return {"changed": False}
    _write_env(scope, "Path", new, typ)
    jid = _journal("env-set", f"{scope}:Path", 0, {"scope": scope, "name": "Path", "old": old, "new": new,
                                                    "type": typ, "why": why})
    return {"changed": True, "journal_id": jid, "removed": len(old.split(";")) - len(new_entries),
            "note": "Already-open terminals keep the old PATH until restarted."}


def path_remove(scope: str, entries: list[str], dry_run: bool = True) -> dict:
    old, _ = _read_raw(scope, "Path")
    targets = {_canon(e) for e in entries}
    keep = [e for e in old.split(";") if e and _canon(e) not in targets]
    removed = [e for e in old.split(";") if e and _canon(e) in targets]
    if dry_run:
        return {"dry_run": True, "would_remove": removed}
    res = _set_path(scope, keep, f"remove {removed}") | {"removed_entries": removed}
    for e in removed:
        _mark_by_path("envvars", f"{scope}:PATH:{e}", "done")
    return res


def path_dedupe(scope: str, dry_run: bool = True) -> dict:
    """Drop entries already present earlier in the effective PATH (machine before user)."""
    seen = set()
    if scope == "user":
        machine = next((v for k, v in read_env("machine").items() if k.lower() == "path"), "")
        seen = {_canon(e) for e in machine.split(";") if e}
    old, _ = _read_raw(scope, "Path")
    keep, dropped = [], []
    for e in old.split(";"):
        if not e:
            continue
        c = _canon(e)
        (dropped if c in seen else keep).append(e)
        seen.add(c)
    if dry_run:
        return {"dry_run": True, "would_remove": dropped, "would_keep": keep}
    res = _set_path(scope, keep, "dedupe") | {"removed_entries": dropped}
    _mark_by_path("envvars", f"{scope}:PATH:duplicates", "done")
    return res


# ---- broken uninstall entries ------------------------------------------------

def remove_uninstall_entry(finding_id: str, dry_run: bool = True) -> dict:
    r = connect().execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not r or r["category"] != "broken-uninstall-entry":
        raise ActionError("Only findings of category broken-uninstall-entry can be removed this way")
    key = r["path"]
    if dry_run:
        return {"dry_run": True, "would_delete_key": key, "title": r["title"]}
    return _reg_delete_with_backup(key, finding_id)


def _reg_delete_with_backup(key: str, finding_id: str) -> dict:
    """reg export, then delete the whole key. Restorable with `restore` (reg import)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{finding_id}.reg"
    exp = subprocess.run(["reg", "export", key, str(backup), "/y"], capture_output=True, text=True)
    if exp.returncode:
        raise ActionError(f"Backup failed, nothing deleted: {exp.stderr.strip() or exp.stdout.strip()}")
    rm = subprocess.run(["reg", "delete", key, "/f"], capture_output=True, text=True)
    if rm.returncode:
        raise ActionError(f"Delete failed (HKLM keys need --elevate): {rm.stderr.strip() or rm.stdout.strip()}")
    jid = _journal("reg-delete", key, 0, {"backup": str(backup), "finding_id": finding_id})
    _mark_finding(finding_id, "done")
    return {"deleted_key": key, "backup": str(backup), "journal_id": jid}


# ---- full uninstall ------------------------------------------------------------

def _program_for(target: str) -> tuple[dict, str | None]:
    """An installed program from a 'program' finding id, its uninstall registry key, or its exact name."""
    inv.programs.cache_clear()
    progs = [p for p in inv.programs() if not p["is_update"] and p["name"]]
    fid = None
    if re.fullmatch(r"[0-9a-f]{10}", target):
        r = connect().execute("SELECT * FROM findings WHERE id=?", (target,)).fetchone()
        if not r:
            raise ActionError(f"No finding {target}")
        if r["category"] != "program":
            raise ActionError(f"{target} is a {r['category']} finding; only 'program' findings are uninstalled "
                              "this way (games use their store's uninstall)")
        key = json.loads(r["evidence"] or "{}").get("registry_key")
        fid, match = target, [p for p in progs if p["key"] == key]
    elif target.upper().startswith("HKEY_"):
        match = [p for p in progs if p["key"].lower() == target.lower()]
    else:
        match = [p for p in progs if p["name"].lower() == target.lower()]
    if not match:
        raise ActionError(f"No installed program matches {target!r} (already uninstalled? re-run `scan programs`)")
    if len(match) > 1:
        raise ActionError(f"{len(match)} programs match {target!r}; pass a registry key instead: "
                          + ", ".join(p["key"] for p in match))
    p = match[0]
    if not p["uninstall"]:
        raise ActionError(f"{p['name']} has no uninstall command")
    return p, fid


def _split_command(cmd: str) -> tuple[str, str]:
    """'"C:\\x\\unins000.exe" /SILENT' -> (exe, args). MsiExec /I (repair dialog) becomes /X (remove)."""
    s = os.path.expandvars(cmd.strip())
    if s.startswith('"'):
        end = s.find('"', 1)
        exe, args = s[1:end], s[end + 1:].strip()
    else:
        m = re.match(r"(.+?\.exe)\b(.*)$", s, re.IGNORECASE)
        exe, args = (m.group(1), m.group(2).strip()) if m else (s, "")
    if os.path.basename(exe).lower() == "msiexec.exe":
        args = re.sub(r"(?i)/I\s*(\{[0-9A-F-]+\})", r"/X\1", args)
    return exe, args


def plan_uninstall(target: str) -> dict:
    """Dry run: everything the program has on the machine, and the command that would run."""
    p, fid = _program_for(target)
    snap = footprint.snapshot(p)
    exe, args = _split_command(p["uninstall"])
    return {"dry_run": True, "program": p["name"], "finding_id": fid, "registry_key": p["key"],
            "runs": f'"{exe}" {args}'.strip(), **footprint.describe(snap["items"]),
            "note": "Runs the program's own uninstaller (admin prompt + its window), waits for it, "
                    "then reports what it left behind as uninstall-leftover findings."}


def uninstall(target: str, timeout_s: float = 3600) -> dict:
    """Snapshot, run the program's own uninstaller elevated, wait, record leftovers."""
    p, fid = _program_for(target)
    snap = footprint.snapshot(p)
    exe, args = _split_command(p["uninstall"])
    size = sum(i.get("size") or 0 for i in snap["items"] if not i.get("shared_with"))
    jid = _journal("uninstall", p["name"], size, {"finding_id": fid, "registry_key": p["key"],
                                                  "command": p["uninstall"], "snapshot": snap, "state": "running"})
    started = time.time()
    try:
        code = run_elevated(exe, args, cwd=os.path.dirname(exe) or None, timeout_s=timeout_s)
    except ElevationError as e:
        _journal_update(jid, state="not-run", error=str(e))
        raise ActionError(str(e)) from e
    stragglers = footprint.wait_for_uninstaller(snap, started)
    time.sleep(2)  # let the last file deletes land
    return _record_leftovers(jid, exit_code=code, stragglers=stragglers)


def uninstall_recheck(journal_id: int) -> dict:
    """Re-check an earlier uninstall's leftovers, e.g. after the restart it asked for."""
    r = connect().execute("SELECT action FROM journal WHERE id=?", (journal_id,)).fetchone()
    if not r or r["action"] != "uninstall":
        raise ActionError(f"Journal entry {journal_id} is not an uninstall")
    return _record_leftovers(journal_id)


def uninstalls() -> list[dict]:
    rows = connect().execute("SELECT * FROM journal WHERE action='uninstall' ORDER BY id DESC")
    out = []
    for r in rows:
        d = json.loads(r["detail"])
        out.append({"journal_id": r["id"], "program": r["target"], "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])),
                    "state": d.get("state"), "leftovers": len(d.get("leftover_ids") or [])})
    return out


def _record_leftovers(jid: int, **extra) -> dict:
    d = json.loads(connect().execute("SELECT detail FROM journal WHERE id=?", (jid,)).fetchone()["detail"])
    snap = d["snapshot"]
    prog = snap["program"]["name"]
    present = footprint.leftovers(snap)
    still_registered = any(i["kind"] == "uninstall-entry" for i in present)
    left = [i for i in present if i["kind"] != "uninstall-entry" and not i["shared_with"]]
    shared = [i for i in present if i["kind"] != "uninstall-entry" and i["shared_with"]]
    findings = [footprint.to_finding(i, prog, jid) for i in left]
    con = connect()
    add_findings(con, findings)
    # Leftovers from an earlier check of this uninstall that have since gone (e.g. after a restart).
    ids = {f.id for f in findings}
    with _lock, con:
        for row in con.execute("SELECT id, evidence FROM findings WHERE scanner='uninstall' AND status NOT IN ('gone','done')"):
            if row["id"] not in ids and json.loads(row["evidence"] or "{}").get("journal_id") == jid:
                con.execute("UPDATE findings SET status='gone' WHERE id=?", (row["id"],))
    state = "still-installed" if still_registered else "done"
    _journal_update(jid, state=state, checked=time.time(), leftover_ids=sorted(ids), **extra)
    if not still_registered:
        _mark_finding(d.get("finding_id"), "done")
    out = {"journal_id": jid, "program": prog, "state": state}
    if "exit_code" in extra:
        out["exit_code"] = extra["exit_code"]
    if extra.get("stragglers"):
        out["still_running"] = extra["stragglers"]
    if still_registered:
        out["note"] = ("The program is still registered: the uninstaller was cancelled, failed, or finishes "
                       "after a restart. Re-check later with `leftovers <journal_id>`.")
    out["leftovers"] = [{"id": f.id, "kind": f.evidence["kind"], "what": f.title.split(": ", 1)[-1],
                         "size": human(f.size), "suggestion": f.suggestion} for f in findings]
    out["leftover_size"] = human(sum(f.size or 0 for f in findings))
    if shared:
        out["left_alone_shared"] = [{"what": i["label"], "shared_with": i["shared_with"]} for i in shared]
    if any(i.get("pending_delete") for i in left):
        out["restart_needed"] = "Some services/drivers are marked for deletion and go at the next restart."
    return out


# ---- removing non-file leftovers -------------------------------------------------

def remove_leftover(finding_id: str, dry_run: bool = True) -> dict:
    """Delete a registry/task leftover of an uninstall, backed up first. Folders and shortcuts use quarantine."""
    r = connect().execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not r or r["category"] != "uninstall-leftover":
        raise ActionError(f"{finding_id} is not an uninstall-leftover finding")
    ev = json.loads(r["evidence"] or "{}")
    kind = ev.get("kind")
    if kind in ("folder", "shortcut"):
        raise ActionError("Files and folders go through `quarantine` (restorable).")
    if kind == "registry":
        raise ActionError("Vendor settings keys are left alone (no registry cleaning).")
    if kind in ("service", "driver"):
        raise ActionError("Services/drivers: restart first if pending deletion; otherwise use the finding's "
                          "command in an elevated terminal after checking its file is gone.")
    if dry_run:
        return {"dry_run": True, "id": finding_id, "kind": kind, "would_remove": r["path"], "title": r["title"]}
    if kind == "task":
        res = _delete_task(r["path"], finding_id)
    elif kind in ("run", "startup-approved"):
        res = _delete_reg_value(ev["key"], ev["value"], finding_id)
    elif kind in ("shell-ext", "shell-verb"):
        return _reg_delete_with_backup(r["path"], finding_id)
    else:
        raise ActionError(f"Don't know how to remove a {kind} leftover")
    _mark_finding(finding_id, "gone" if "already_gone" in res else "done")
    return res


# ---- shared: delete a registry value / scheduled task, backed up first ----------------

def _delete_reg_value(key: str, value: str, finding_id: str | None) -> dict:
    """Delete one value; its data goes in the journal, so `restore` puts it back."""
    hive, sub = footprint._split_key(key)
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            data, typ = winreg.QueryValueEx(k, value)
            winreg.DeleteValue(k, value)
    except FileNotFoundError:
        return {"already_gone": f"{key} -> {value}"}
    except PermissionError as e:
        raise ActionError(f"{key.split(chr(92))[0]} values need an elevated terminal (add --elevate)") from e
    detail = {"key": key, "value": value, "type": typ, "finding_id": finding_id}
    detail |= {"hex": data.hex()} if isinstance(data, bytes) else {"data": data}
    jid = _journal("reg-value-delete", f"{key}\\{value}", 0, detail)
    return {"deleted_value": f"{key} -> {value}", "journal_id": jid}


def _delete_task(task: str, finding_id: str) -> dict:
    """Export the task's XML, then delete it. `restore` re-creates it from the XML."""
    xml = run(["schtasks", "/query", "/tn", task, "/xml"])
    if "<Task" not in xml:
        raise ActionError(f"Could not back up task {task}; nothing deleted (may need --elevate)")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{finding_id}.xml"
    backup.write_text(xml, encoding="utf-16")
    res = subprocess.run(["schtasks", "/delete", "/tn", task, "/f"], capture_output=True, text=True)
    if res.returncode:
        raise ActionError(f"Delete failed (may need --elevate): {res.stderr.strip() or res.stdout.strip()}")
    jid = _journal("task-delete", task, 0, {"task": task, "backup": str(backup), "finding_id": finding_id})
    return {"deleted_task": task, "backup": str(backup), "journal_id": jid}


# ---- startup ----------------------------------------------------------------------

_SERVICE_START = {2: "auto", 3: "demand", 4: "disabled"}


def _finding_of(finding_id: str, category: str) -> tuple[sqlite3.Row, dict]:
    r = connect().execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not r or r["category"] != category:
        raise ActionError(f"{finding_id} is not a {category} finding")
    return r, json.loads(r["evidence"] or "{}")


def _set_startup_flag(key: str, value: str, finding_id: str) -> dict:
    """Task Manager's 'Disabled': 03 00 00 00 + the time it was switched off."""
    ft = int(time.time() * 10**7) + 116444736000000000
    new = b"\x03\x00\x00\x00" + ft.to_bytes(8, "little")
    hive, sub = footprint._split_key(key)
    try:
        with winreg.CreateKeyEx(hive, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            try:
                old, _ = winreg.QueryValueEx(k, value)
            except FileNotFoundError:
                old = None
            winreg.SetValueEx(k, value, 0, winreg.REG_BINARY, new)
    except PermissionError as e:
        raise ActionError("All-users startup entries need an elevated terminal (add --elevate)") from e
    jid = _journal("startup-flag", f"{key}\\{value}", 0, {"key": key, "value": value, "finding_id": finding_id,
                                                          "old_hex": old.hex() if isinstance(old, bytes) else None})
    return {"switched_off": value, "journal_id": jid, "undo": "Task Manager > Startup apps, or `restore`"}


def _disable_task(task: str, finding_id: str) -> dict:
    res = subprocess.run(["schtasks", "/change", "/tn", task, "/disable"], capture_output=True, text=True)
    if res.returncode:
        raise ActionError(f"Could not disable {task} (may need --elevate): {res.stderr.strip() or res.stdout.strip()}")
    jid = _journal("task-disable", task, 0, {"task": task, "finding_id": finding_id})
    return {"disabled_task": task, "journal_id": jid}


def _service_to_manual(name: str, finding_id: str) -> dict:
    v = footprint.reg_values(f"HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Services\\{name}") or {}
    res = subprocess.run(["sc.exe", "config", name, "start=", "demand"], capture_output=True, text=True)
    if res.returncode:
        hint = " (needs --elevate)" if res.returncode == 5 else ""
        raise ActionError(f"Could not change {name}{hint}: {res.stdout.strip() or res.stderr.strip()}")
    jid = _journal("service-start", name, 0, {"service": name, "old_start": v.get("Start"),
                                              "delayed": v.get("DelayedAutostart") or 0, "finding_id": finding_id})
    return {"service_set_to_manual": name, "journal_id": jid}


def startup_disable(finding_id: str, dry_run: bool = True) -> dict:
    """Stop something starting with Windows: Task Manager's switch for apps, disable for tasks,
    Manual for services. Reversible with `restore`."""
    r, ev = _finding_of(finding_id, "startup")
    if ev.get("protected"):
        raise ActionError(f"{r['title']} is {ev['protected']}.")
    kind = ev.get("kind")
    what = {"app": f"turn off its startup switch ({ev.get('location')}), like Task Manager does",
            "task": f"disable scheduled task {ev.get('task')}",
            "service": f"set service {ev.get('service')} to Manual (starts only when needed)"}.get(kind)
    if not what:
        raise ActionError(f"Don't know how to disable a {kind} startup item")
    if dry_run:
        return {"dry_run": True, "item": r["title"], "would": what, "risk": r["risk"]}
    if kind == "app":
        res = _set_startup_flag(ev["approval_key"], ev["approval_value"], finding_id)
    elif kind == "task":
        res = _disable_task(ev["task"], finding_id)
    else:
        res = _service_to_manual(ev["service"], finding_id)
    _mark_finding(finding_id, "done")
    return {"item": r["title"], **res}


def startup_fix(finding_id: str, dry_run: bool = True) -> dict:
    """Clean up a broken startup entry. Everything is backed up and restorable."""
    r, ev = _finding_of(finding_id, "startup-broken")
    kind = ev.get("kind")
    if kind == "approval-orphans":
        items = ev.get("items") or []
        if dry_run:
            return {"dry_run": True, "item": r["title"], "would_delete": [f"{k} -> {v}" for k, v in items]}
        done, failed = [], []
        for key, value in items:
            try:
                done.append(_delete_reg_value(key, value, finding_id))
            except ActionError as e:
                failed.append({"switch": value, "error": str(e)})
        if not failed:
            _mark_finding(finding_id, "done")
        return {"item": r["title"], "removed": len(done), "failed": failed}
    what = {"run": f"delete the entry {ev.get('value')} ({ev.get('location')}); its value is journalled",
            "link": f"quarantine the shortcut {r['path']}",
            "task": f"delete scheduled task {ev.get('task')} (XML backup kept)",
            "service": f"remove the service registration {ev.get('service')} (registry backup kept)"}.get(kind)
    if not what:
        raise ActionError(f"Don't know how to clean up a {kind} entry")
    if dry_run:
        return {"dry_run": True, "item": r["title"], "would": what}
    if kind == "run":
        res = _delete_reg_value(ev["key"], ev["value"], finding_id)
    elif kind == "link":
        return quarantine(finding_id, reason="Startup shortcut to a program that's gone")
    elif kind == "task":
        res = _delete_task(ev["task"], finding_id)
    else:
        if ev.get("image") and os.path.exists(ev["image"]):
            raise ActionError(f"{ev['image']} exists again; not removing the service")
        key = f"HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Services\\{ev['service']}"
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{finding_id}.reg"
        exp = subprocess.run(["reg", "export", key, str(backup), "/y"], capture_output=True, text=True)
        if exp.returncode:
            raise ActionError(f"Backup failed, nothing removed: {exp.stderr.strip() or exp.stdout.strip()}")
        rm = subprocess.run(["sc.exe", "delete", ev["service"]], capture_output=True, text=True)
        if rm.returncode:
            raise ActionError(f"sc delete failed (needs --elevate): {rm.stdout.strip() or rm.stderr.strip()}")
        jid = _journal("reg-delete", key, 0, {"backup": str(backup), "finding_id": finding_id,
                                              "note": "service registration; a restore takes effect after a restart"})
        res = {"removed_service": ev["service"], "backup": str(backup), "journal_id": jid}
    _mark_finding(finding_id, "gone" if "already_gone" in res else "done")
    return {"item": r["title"], **res}


# ---- app updates (winget) ------------------------------------------------------

def _update_finding(finding_id: str) -> tuple[sqlite3.Row, dict]:
    r = connect().execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not r or r["category"] != "app-update":
        raise ActionError(f"{finding_id} is not an app-update finding")
    return r, json.loads(r["evidence"] or "{}")


def _winget(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    res = subprocess.run(["winget", *args, "--disable-interactivity"], capture_output=True, timeout=timeout,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = res.stdout.decode("utf-8", errors="replace")
    # Drop the progress spinner/bars; keep the lines that say what happened.
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text)
             if ln.strip() and not re.fullmatch(r"[-\\|/ ]+|.*[█▒]+.*", ln.strip())]
    return res.returncode, "\n".join(lines[-8:])


def app_update(finding_id: str, dry_run: bool = True) -> dict:
    """Update one app with winget (its installer may show a UAC prompt). Not reversible."""
    r, ev = _update_finding(finding_id)
    if ev.get("own_installer"):
        raise ActionError(f"Update {r['title']} from {ev['own_installer']} instead of winget.")
    args = ["upgrade", "--id", ev["winget_id"], "--exact", "--silent",
            "--accept-package-agreements", "--accept-source-agreements"]
    if dry_run:
        return {"dry_run": True, "app": r["title"], "from": ev["installed"], "to": ev["available"],
                "would_run": "winget " + " ".join(args)}
    try:
        code, tail = _winget(args)
    except subprocess.TimeoutExpired:
        code, tail = -1, "timed out after 30 minutes"
    ok = code == 0
    jid = _journal("app-update", ev["winget_id"], 0, {"finding_id": finding_id, "app": r["title"],
                                                      "from": ev["installed"], "to": ev["available"],
                                                      "exit_code": code, "output": tail})
    if ok:
        _mark_finding(finding_id, "done")
    out = {"app": r["title"], "from": ev["installed"], "to": ev["available"], "ok": ok, "journal_id": jid}
    if not ok:
        out |= {"exit_code": f"0x{code & 0xFFFFFFFF:08X}", "output": tail,
                "hint": "Often: the app is open (close it and retry), or the installer was cancelled at its UAC prompt."}
    return out


def app_pin(finding_id: str, dry_run: bool = True) -> dict:
    """Stop winget offering updates for an app the user decided to keep at its version. Reversible."""
    r, ev = _update_finding(finding_id)
    args = ["pin", "add", "--id", ev["winget_id"], "--exact", "--accept-source-agreements"]
    if dry_run:
        return {"dry_run": True, "app": r["title"], "would_run": "winget " + " ".join(args)}
    code, tail = _winget(args, timeout=120)
    if code:
        raise ActionError(f"winget pin failed for {r['title']} (0x{code & 0xFFFFFFFF:08X}): {tail}")
    jid = _journal("winget-pin", ev["winget_id"], 0, {"finding_id": finding_id, "app": r["title"],
                                                      "version": ev["installed"]})
    _mark_finding(finding_id, "keep")
    return {"pinned": r["title"], "at": ev["installed"], "journal_id": jid}


# ---- drivers ------------------------------------------------------------------------
# Pitstop never installs drivers. It opens the right Windows page, makes a restore point
# first, and clears out old copies of driver packages that a newer copy has replaced.

SETTINGS_PAGES = {"optional-updates": "ms-settings:windowsupdate-optionalupdates",
                  "windows-update": "ms-settings:windowsupdate", "device-manager": "devmgmt.msc"}


def open_settings(what: str) -> dict:
    """Open a fixed Windows page for the user (nothing changes until they act there)."""
    if what not in SETTINGS_PAGES:
        raise ActionError(f"Unknown page {what!r}; choose from {list(SETTINGS_PAGES)}")
    os.startfile(SETTINGS_PAGES[what])
    return {"opened": what}


def restore_point(description: str = "Pitstop: before driver changes", dry_run: bool = True) -> dict:
    """System Restore point before driver changes. Needs admin; Windows allows one per 24 hours."""
    if dry_run:
        return {"dry_run": True, "would": f"create a System Restore point '{description}' (needs --elevate)"}
    from .elevate import is_admin
    if not is_admin():
        raise ActionError("Creating a restore point needs admin: add --elevate")
    desc = description.replace("'", "''")
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"Checkpoint-Computer -Description '{desc}' -RestorePointType MODIFY_SETTINGS "
                          "-WarningAction Stop -ErrorAction Stop"], capture_output=True, text=True, timeout=600)
    msg = (res.stderr or res.stdout).strip()
    if res.returncode == 0:
        jid = _journal("restore-point", description, 0, {"description": description})
        return {"created": description, "journal_id": jid}
    if "1440" in msg or "already been created" in msg:
        return {"created": False, "note": "Windows allows one restore point per 24 hours and there's already one "
                                          "from the last day, so that one covers these changes."}
    if re.search(r"disabled|turned off|0x80042306|service cannot be started", msg, re.I):
        raise ActionError("System Restore is turned off for C:. Turn it on in System Properties > System "
                          "Protection > C: > Configure, then try again.")
    raise ActionError(f"Restore point failed: {msg[-400:]}")


def driver_remove(finding_id: str, dry_run: bool = True) -> dict:
    """Remove an old copy of a driver package. It's exported to quarantine first (restorable until purge),
    and pnputil refuses (no /force) if any device still uses it. Needs admin."""
    r, ev = _finding_of(finding_id, "driver-superseded")
    inf = ev["inf"]
    if not re.fullmatch(r"oem\d+\.inf", inf):
        raise ActionError(f"Refusing {inf}: only third-party (oemNN.inf) packages")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(os.environ.get("SystemDrive", "C:") + "\\") / QUARANTINE_DIR / f"{stamp}_driver_{inf[:-4]}_{ev['original']}"
    if dry_run:
        return {"dry_run": True, "item": r["title"], "would": f"export {inf} to {dest}, then `pnputil /delete-driver {inf}`",
                "size": human(r["size"])}
    from .elevate import is_admin
    if not is_admin():
        raise ActionError("Removing driver packages needs admin: add --elevate")
    dest.mkdir(parents=True, exist_ok=True)
    exp = subprocess.run(["pnputil", "/export-driver", inf, str(dest)], capture_output=True, text=True)
    if exp.returncode or not any(dest.rglob("*.inf")):
        _force_rmtree(str(dest))
        raise ActionError(f"Export failed, nothing removed: {(exp.stdout or exp.stderr).strip()[-300:]}")
    rm = subprocess.run(["pnputil", "/delete-driver", inf], capture_output=True, text=True)
    if rm.returncode:
        _force_rmtree(str(dest))
        out = (rm.stdout or rm.stderr).strip()
        hint = " A device still uses it, so Windows kept it." if re.search(r"in use|presently installed", out, re.I) else ""
        raise ActionError(f"pnputil kept {inf}.{hint} {out[-300:]}")
    size = dir_size(dest)[0]
    jid = _journal("driver-delete", f"{inf} ({ev['original']} {ev['version']})", size,
                   {"dest": str(dest), "inf": inf, "original": ev["original"], "finding_id": finding_id})
    _mark_finding(finding_id, "done")
    return {"removed": f"{inf} ({ev['original']} {ev['version']})", "quarantined_copy": str(dest),
            "size": human(size), "journal_id": jid, "note": "Frees its space now; the exported copy is freed on purge."}


def journal(limit: int = 50) -> list[dict]:
    rows = connect().execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = json.loads(r["detail"])
        if "snapshot" in d:  # hundreds of lines; `leftovers <id>` is the readable view
            d["snapshot"] = f"{len(d['snapshot']['items'])} items"
        out.append(dict(r) | {"detail": d})
    return out
