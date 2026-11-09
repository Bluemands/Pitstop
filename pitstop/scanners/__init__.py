"""Scanner registry. Each scanner is `scan() -> list[Finding]` and is read-only."""
from __future__ import annotations

import json
import time
import traceback
from typing import Callable

from ..store import Finding, connect, record_scan_error, save_findings
from . import (containers, devtools, drivers, envvars, games, installers, orphans, programs, startup, system,
               updates)

HEALTH = ["updates", "startup", "drivers"]  # quick and change often: the UI runs these when it opens (at most hourly)

SCANNERS: dict[str, tuple[Callable[[], list[Finding]], str]] = {
    "programs":   (programs.scan,   "Installed programs (size, last use) and broken uninstall entries"),
    "envvars":    (envvars.scan,    "Dead / duplicate PATH entries and env vars pointing at missing paths"),
    "devtools":   (devtools.scan,   "Package caches, SDKs, toolchains, AI model stores"),
    "containers": (containers.scan, "WSL distros, Docker and VM disks"),
    "games":      (games.scan,      "Steam / Epic libraries with last-played dates"),
    "system":     (system.scan,     "Temp, crash dumps, update caches, hibernation file, Windows.old"),
    "installers": (installers.scan, "Old installers, ISOs and archives in Downloads / Desktop"),
    "updates":    (updates.scan,    "Apps with a newer version (winget) and runtimes past end of support"),
    "startup":    (startup.scan,    "Startup apps, sign-in/boot tasks and automatic services"),
    "drivers":    (drivers.scan,    "Driver updates from Windows Update, devices without a working driver, old driver copies"),
    # Last: it skips folders the other scanners already account for.
    "orphans":    (orphans.scan,    "AppData / ProgramData / Program Files folders that match no installed program"),
}


def _recheck_uninstalls() -> dict:
    """Not a scanner: leftovers come from `pitstop uninstall`'s before/after snapshot, so "rescanning" them
    means re-checking each uninstall that still has open leftovers (same as `pitstop leftovers <id>`)."""
    from .. import actions  # late: actions imports from this package
    con = connect()
    jids = {json.loads(r["evidence"] or "{}").get("journal_id") for r in con.execute(
        "SELECT evidence FROM findings WHERE scanner='uninstall' AND status NOT IN ('gone', 'done')")}
    left = sum(len(actions.uninstall_recheck(j)["leftovers"]) for j in sorted(j for j in jids if j))
    return {"findings": left, "bytes": 0, "uninstalls_checked": len([j for j in jids if j])}


# Things a rescan can name that aren't scanners. "uninstall" is the scanner name on leftover findings.
EXTRA = {"uninstall": (_recheck_uninstalls, "Re-check what earlier full uninstalls left behind")}


def run(names: list[str] | None = None, progress: Callable[[str], None] | None = None) -> dict:
    con = connect()
    summary = {}
    for name in names or [*SCANNERS, *EXTRA]:
        if progress:
            progress(name)
        if name in EXTRA:
            t = time.monotonic()
            try:
                summary[name] = EXTRA[name][0]() | {"seconds": round(time.monotonic() - t, 1)}
            except Exception as e:
                summary[name] = {"error": str(e)}
            continue
        if name not in SCANNERS:  # e.g. an old page asking for something renamed; don't sink the rest
            summary[name] = {"error": f"unknown scanner {name!r}"}
            continue
        fn, _ = SCANNERS[name]
        t = time.monotonic()
        try:
            found = fn()
        except Exception as e:  # one broken scanner shouldn't sink the rest
            record_scan_error(con, name, "".join(traceback.format_exception_only(type(e), e)).strip())
            summary[name] = {"error": str(e)}
            continue
        secs = round(time.monotonic() - t, 1)
        save_findings(con, name, found, secs)
        summary[name] = {"findings": len(found), "bytes": sum(f.size or 0 for f in found), "seconds": secs}
    return summary
