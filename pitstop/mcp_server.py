"""MCP server exposing Pitstop to Claude.

Read tools are free to call. Tools that change the machine default to a dry
run and need confirm=True, and Claude must have the user's explicit OK for
that specific item first.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import actions, diskindex, queries, scanners
from .elevate import ElevationError, parse_json_stream, pitstop_elevated
from .util import human

mcp = MCPServer("pitstop", instructions=(
    "Pitstop is a Windows maintenance tool with three sections: Clean, Health (updates, startup, drivers) and Tune (sensors, fan logs; the hw_* tools). Clean inventories forgotten space: leftover folders from uninstalled programs, "
    "old SDKs/toolchains, caches, model stores, WSL/Docker disks, stale games, dead PATH entries. "
    "Workflow: summary -> run_scan if stale -> list_findings per category -> discuss with the user -> "
    "decide() to record keep/remove -> act. Never quarantine, purge or edit PATH without the user's explicit "
    "OK for those specific items. Prefer the finding's own 'command' (the owning tool's uninstall/clean "
    "command) over quarantining files a program still manages. Quarantine frees no space until purge. "
    "Programs: use uninstall() (snapshot -> own uninstaller -> leftovers), then quarantine leftover folders "
    "(elevate=True for Program Files/ProgramData) and remove_leftover() for tasks/startup/right-click entries. "
    "Tune is read-only: Pitstop never sets a fan speed; fan changes are proposals the user makes in FanControl or their cooler's software."
))


def _err(e: Exception) -> dict:
    return {"error": str(e)}


@mcp.tool()
def summary() -> dict:
    """Drive free space, reclaimable bytes per category, decision counts and scan freshness. Start here."""
    return queries.summary()


@mcp.tool()
def run_scan(names: list[str] | None = None) -> dict:
    """Run scanners (all if names omitted). Read-only; takes ~1-2 min for all.
    Scanners: programs, envvars, devtools, containers, games, system, installers, orphans."""
    if names and (bad := [n for n in names if n not in scanners.SCANNERS]):
        return {"error": f"unknown scanners {bad}", "available": list(scanners.SCANNERS)}
    res = scanners.run(names)
    return {k: v | ({"size": human(v["bytes"])} if "bytes" in v else {}) for k, v in res.items()}


@mcp.tool()
def list_findings(category: str | None = None, scanner: str | None = None, status: str | None = None,
                  min_mb: float = 0, search: str | None = None, limit: int = 40, offset: int = 0,
                  order: str = "size") -> list[dict]:
    """List findings, biggest first by default (order: size|age|title).
    Categories include: program, game, orphan-folder, unrecognised-root-folder, cache, sdk, models, vm, wsl,
    docker, system-cache, system-file, junk-file, recycle-bin, installer, archive, disk-image,
    dead-path-entry, duplicate-path-entries, dead-env-var, broken-uninstall-entry.
    Status: new|keep|remove|review|done."""
    return queries.list_findings(category, scanner, status, min_mb, search, limit, offset, order)


@mcp.tool()
def get_finding(id: str) -> dict:
    """Full details of one finding: evidence, suggestion, cleanup command, registry key."""
    return queries.get_finding(id) or {"error": "not found"}


@mcp.tool()
def decide(ids: list[str], status: str, note: str | None = None) -> dict:
    """Record the user's decision on findings: keep | remove | review | new. Decisions survive re-scans,
    so 'keep' hides nothing but stops the item being re-suggested."""
    try:
        return {"updated": queries.decide(ids, status, note)}
    except ValueError as e:
        return _err(e)


@mcp.tool()
def index_drive(root: str) -> dict:
    """Start indexing folder sizes under root (e.g. 'C:\\\\') in the background. ~100 s per 2 TB.
    Poll index_status, then use du / largest_files."""
    return diskindex.build_in_background(root)


@mcp.tool()
def index_status() -> dict:
    """Progress of running index jobs and completed index runs."""
    return diskindex.status()


@mcp.tool()
def du(path: str, top: int = 25) -> dict:
    """Biggest subfolders and files (>=256 MB) of an indexed folder, with the unlisted remainder."""
    r = diskindex.du(path, top)
    if "error" in r:
        return r
    fmt = lambda xs: [{"path": x["path"], "size": human(x["size"])} for x in xs]
    return {"path": r["path"], "size": human(r["size"]), "children": fmt(r["children"]),
            "big_files": fmt(r["big_files"]), "other": human(r["other_bytes"]),
            "more_children": r["more_children"]}


@mcp.tool()
def largest_files(root: str | None = None, top: int = 40) -> list[dict]:
    """Largest individual files (>=256 MB) in the index, optionally under root."""
    return [{"path": f["path"], "size": human(f["size"])} for f in diskindex.largest_files(root, top)]


@mcp.tool()
def quarantine(targets: list[str], reason: str = "", confirm: bool = False, elevate: bool = False) -> list[dict]:
    """Move folders/files (paths or finding ids) into <drive>:\\Pitstop-Quarantine. Reversible via restore.
    Without confirm=True this is a dry run. Only confirm after the user approved these exact items.
    elevate=True (with confirm) runs it as admin behind one UAC prompt: needed for Program Files / ProgramData."""
    if confirm and elevate:
        try:
            return parse_json_stream(pitstop_elevated(["quarantine", *targets, "--reason", reason, "--yes"]))
        except (ElevationError, ValueError) as e:
            return [_err(e)]
    out = []
    reserved: set[str] = set()
    for t in targets:
        try:
            r = (actions.quarantine(t, reason) if confirm
                 else actions.plan_quarantine(t, reserved) | {"dry_run": True})
            out.append(r | {"size": human(r["size"])})
        except actions.ActionError as e:
            out.append({"target": t, "error": str(e)})
    return out


@mcp.tool()
def list_quarantine() -> list[dict]:
    """Items currently in quarantine (restorable, not yet freeing space)."""
    return [q | {"size": human(q["size"])} for q in actions.quarantined()]


@mcp.tool()
def restore(journal_id: int) -> dict:
    """Reverse a journalled action: un-quarantine, restore a PATH value, or re-import a deleted registry key."""
    try:
        return actions.restore(journal_id)
    except (actions.ActionError, OSError) as e:
        return _err(e)


@mcp.tool()
def purge(journal_ids: list[int] | None = None, older_than_days: float | None = None,
          confirm: bool = False) -> dict:
    """PERMANENTLY delete quarantined items (all, specific ids, or older than N days). Irreversible.
    Dry run unless confirm=True; confirm only with the user's explicit go-ahead."""
    if not confirm:
        items = [q for q in actions.quarantined() if (journal_ids is None or q["journal_id"] in journal_ids)
                 and (older_than_days is None or q["age_days"] >= older_than_days)]
        return {"dry_run": True, "would_purge": items, "would_free": human(sum(q["size"] for q in items))}
    r = actions.purge(journal_ids, older_than_days)
    return r | {"freed": human(r["freed_bytes"])}


@mcp.tool()
def path_cleanup(scope: str, remove_entries: list[str] | None = None, dedupe: bool = False,
                 confirm: bool = False) -> dict:
    """Edit PATH for scope 'user' or 'machine' (machine needs admin): remove specific entries and/or
    drop duplicates. Old value is journalled (restore-able). Dry run unless confirm=True."""
    try:
        out = {}
        if remove_entries:
            out["remove"] = actions.path_remove(scope, remove_entries, dry_run=not confirm)
        if dedupe:
            out["dedupe"] = actions.path_dedupe(scope, dry_run=not confirm)
        return out or {"error": "nothing to do: pass remove_entries and/or dedupe=True"}
    except actions.ActionError as e:
        return _err(e)


@mcp.tool()
def remove_broken_uninstall_entry(id: str, confirm: bool = False) -> dict:
    """Delete a broken Apps & Features entry (finding category broken-uninstall-entry) after exporting
    a .reg backup. HKLM entries need admin. Dry run unless confirm=True."""
    try:
        return actions.remove_uninstall_entry(id, dry_run=not confirm)
    except actions.ActionError as e:
        return _err(e)


@mcp.tool()
def uninstall(target: str, confirm: bool = False) -> dict:
    """Full uninstall of a program: a 'program' finding id, its uninstall registry key, or exact name.
    Dry run (default) lists everything it has on the machine: folders, shortcuts, services, drivers,
    tasks, startup entries, right-click handlers, vendor registry keys; shared items are left alone.
    confirm=True runs the program's own uninstaller (the user sees a UAC prompt and its window), waits,
    then records what it left behind as 'uninstall-leftover' findings. Confirm only with the user's
    explicit OK for this program."""
    try:
        return actions.uninstall(target) if confirm else actions.plan_uninstall(target)
    except actions.ActionError as e:
        return _err(e)


@mcp.tool()
def uninstall_leftovers(journal_id: int | None = None) -> dict | list:
    """Without an id: past uninstalls and their state. With one: re-check what that uninstall left
    behind (do this after the restart an uninstaller asked for)."""
    try:
        return actions.uninstall_recheck(journal_id) if journal_id else actions.uninstalls()
    except actions.ActionError as e:
        return _err(e)


@mcp.tool()
def remove_leftover(ids: list[str], confirm: bool = False, elevate: bool = False) -> list[dict]:
    """Remove uninstall-leftover findings that aren't files: scheduled tasks (XML backup), startup
    entries (value backed up), right-click handlers (.reg backup). All restorable. Folders and
    shortcuts use quarantine. Dry run unless confirm=True; elevate=True for HKLM/system items."""
    if confirm and elevate:
        try:
            return parse_json_stream(pitstop_elevated(["leftover-remove", *ids, "--yes"]))
        except (ElevationError, ValueError) as e:
            return [_err(e)]
    out = []
    for i in ids:
        try:
            out.append(actions.remove_leftover(i, dry_run=not confirm))
        except actions.ActionError as e:
            out.append({"id": i, "error": str(e)})
    return out


@mcp.tool()
def update_apps(ids: list[str], confirm: bool = False) -> list[dict]:
    """Update apps (finding ids of category 'app-update') with winget, one at a time. Installers may show a
    UAC prompt, so the user should be at the PC. Not reversible. Dry run unless confirm=True; confirm only
    after the user approved this batch. Apps tagged use-own-installer (Visual Studio, Office) are refused."""
    out = []
    for i in ids:
        try:
            out.append(actions.app_update(i, dry_run=not confirm))
        except actions.ActionError as e:
            out.append({"id": i, "error": str(e)})
    return out


@mcp.tool()
def pin_updates(ids: list[str], confirm: bool = False) -> list[dict]:
    """For app-update findings the user wants to stay on their current version: `winget pin add` so they
    stop being offered. Reversible with restore(journal_id). Dry run unless confirm=True."""
    out = []
    for i in ids:
        try:
            out.append(actions.app_pin(i, dry_run=not confirm))
        except actions.ActionError as e:
            out.append({"id": i, "error": str(e)})
    return out


def _batch(fn, cli: str, ids: list[str], confirm: bool, elevate: bool) -> list[dict]:
    if confirm and elevate:
        try:
            return parse_json_stream(pitstop_elevated([cli, *ids, "--yes"]))
        except (ElevationError, ValueError) as e:
            return [_err(e)]
    out = []
    for i in ids:
        try:
            out.append(fn(i, dry_run=not confirm))
        except actions.ActionError as e:
            out.append({"id": i, "error": str(e)})
    return out


@mcp.tool()
def startup_disable(ids: list[str], confirm: bool = False, elevate: bool = False) -> list[dict]:
    """Stop 'startup' findings starting with Windows: apps get Task Manager's off switch, tasks are disabled,
    services set to Manual (never Disabled). Security software is refused. All reversible with restore().
    All-users entries and services need elevate=True. Dry run unless confirm=True."""
    return _batch(actions.startup_disable, "startup-disable", ids, confirm, elevate)


@mcp.tool()
def startup_fix(ids: list[str], confirm: bool = False, elevate: bool = False) -> list[dict]:
    """Clean up 'startup-broken' findings (entries whose program is gone, leftover Task Manager switches,
    dead services). Each is backed up and restorable. Dry run unless confirm=True; elevate=True for HKLM/services."""
    return _batch(actions.startup_fix, "startup-fix", ids, confirm, elevate)


@mcp.tool()
def open_settings(what: str) -> dict:
    """Open a Windows page for the user: 'optional-updates' (where they tick driver updates themselves),
    'windows-update' or 'device-manager'. Pitstop never installs drivers."""
    try:
        return actions.open_settings(what)
    except actions.ActionError as e:
        return _err(e)


@mcp.tool()
def restore_point(confirm: bool = False) -> dict:
    """Create a System Restore point before driver changes (one admin prompt; Windows allows one per 24 h).
    Dry run unless confirm=True."""
    if not confirm:
        return actions.restore_point(dry_run=True)
    try:
        return (parse_json_stream(pitstop_elevated(["restore-point", "--yes"])) or [{}])[0]
    except (ElevationError, ValueError) as e:
        return _err(e)


@mcp.tool()
def driver_remove(ids: list[str], confirm: bool = False) -> list[dict]:
    """Remove 'driver-superseded' findings: old copies of driver packages that a newer copy replaced.
    Each is exported to quarantine first (restorable until purge); Windows refuses if a device uses it.
    Runs elevated (one admin prompt). Dry run unless confirm=True."""
    return _batch(actions.driver_remove, "driver-remove", ids, confirm, elevate=confirm)


@mcp.tool()
def hw_now(everything: bool = False, elevate: bool = False) -> dict:
    """Current sensor readings (Tune): key temperatures, fans, power and load per part, the fan-header
    table (RPM, %, and what controls each: FanControl curve / BIOS / GPU), and anything running hot.
    Uses the background monitor if it's running (full readings). Otherwise reads without admin (GPU, drives,
    CPU load only) unless elevate=True (one admin prompt). Read-only."""
    from .hw import fancontrol, info, live
    if elevate and not live.monitor_snapshot():
        try:
            live.start_monitor()
        except ElevationError as e:
            return _err(e)
        import time
        for _ in range(20):
            time.sleep(0.5)
            if live.monitor_snapshot():
                break
    return info.report(live.snapshot(), everything, live.names(), fancontrol.read())


@mcp.tool()
def hw_info() -> dict:
    """Hardware inventory: board and BIOS, CPU, RAM sticks, GPUs and drivers, drives with health/wear,
    fan software running (FanControl, iCUE...), and FanControl's active config. Read-only."""
    from .hw import fancontrol, info
    fc = fancontrol.read()
    return info.inventory() | {"fancontrol": fc}


@mcp.tool()
def hw_name(sensor_id: str, label: str) -> dict:
    """Name a sensor or fan header (e.g. '/lpc/nct6798d/0/fan/1' -> 'Top exhaust front'); an empty label
    clears it. Naming a motherboard header names its RPM and control % together. Only changes Pitstop's
    own labels."""
    from .hw import live
    return live.set_name(sensor_id, label)


@mcp.tool()
def hw_log(action: str, label: str = "", note: str = "", max_hours: float = 4, session: int | None = None,
           admin: bool = True) -> dict | list:
    """Sensor logs (Tune). action:
    'start' (label, max_hours): log every second until stopped; starts the admin reader (one UAC prompt)
            unless admin=False (then no CPU temperatures or motherboard fans).
    'stop' | 'mark' (note: 'this is the noise') | 'list' |
    'save' (label, note): save the running reader's last 5 minutes as a log ("that was loud just now") |
    'delete' (session): remove a finished log.
    Logging only reads sensors; nothing on the PC changes."""
    from .hw import live, logdb
    try:
        if action == "start":
            return live.start_log(label, max_hours, admin)
        if action == "stop":
            return logdb.stop()
        if action == "mark":
            return logdb.mark(note)
        if action == "save":
            return live.request_save(label or "Saved moment", note)
        if action == "list":
            return logdb.sessions()
        if action == "delete" and session:
            return logdb.delete(session)
    except ElevationError as e:
        return _err(e)
    return {"error": "action must be start, stop, mark, save, list or delete (with session)"}


@mcp.tool()
def hw_report(session: int | None = None) -> dict:
    """Summary of a sensor log (latest if session omitted): stats per temperature/fan/power, every fan ramp with
    what preceded it (temperatures that rose, power, load, top CPU/GPU programs), the user's marks with
    readings at that moment, limit breaches, busiest programs, and FanControl's curves at the start."""
    from .hw import live, report
    return report.report(session, live.names())


@mcp.tool()
def hw_query(match: list[str], session: int | None = None, start: str | None = None, end: str | None = None,
             points: int = 120) -> dict:
    """Downsampled readings from a log for sensors matching any of `match` (e.g. ['CPU Package', 'Fan #2']),
    optionally between local clock times start/end ('HH:MM' or 'HH:MM:SS'). Each point is 'time avg/max'."""
    from .hw import live, report
    return report.query(session, match, start, end, points, live.names())


@mcp.tool()
def journal(limit: int = 30) -> list[dict]:
    """History of every change made, newest first."""
    return actions.journal(limit)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
