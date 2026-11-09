"""Installed programs with real on-disk size and usage hints; broken uninstall entries."""
from __future__ import annotations

import os
import re

from .. import inventory as inv
from ..store import Finding
from ..util import age_days, clean_path, dir_size, norm_key


def scan() -> list[Finding]:
    out: list[Finding] = []
    sized: dict[str, tuple[int, float, bool]] = {}

    for p in inv.programs():
        name = p["name"]
        if p["is_update"]:
            continue
        if not name:
            # Hidden components (e.g. Adobe's CCX Process / CoreSync) have no DisplayName and never show
            # in Apps & Features, but they're still installed and outlive their parent product.
            if not (p["install_dir"] or p["uninstall"]) or p["system_component"]:
                continue
            m = re.search(r'--productName="([^"]+)"', p["uninstall"] or "")
            name = f"{m.group(1) if m else p['key'].rsplit(chr(92), 1)[-1]} (hidden, by {p['publisher'] or 'unknown'})"
        d = p["install_dir"]
        exists = bool(d and os.path.isdir(d))

        # --- broken entry: nothing on disk and no working uninstaller
        if not exists and not p["system_component"]:
            unin = clean_path(p["uninstall"])
            unin_ok = bool(unin and (os.path.exists(unin) or "msiexec" in unin.lower()))
            if p["install_location_raw"] and not unin_ok:
                out.append(Finding(
                    scanner="programs", category="broken-uninstall-entry", title=name,
                    path=p["key"], size=0, risk="low",
                    suggestion="Listed in Apps & Features but its folder and uninstaller are gone. "
                               "Safe to remove the registry entry (back it up first).",
                    command=f'reg export "{p["key"]}" backup.reg && reg delete "{p["key"]}" /f',
                    evidence={"install_location": p["install_location_raw"], "uninstall": p["uninstall"]},
                ))
            continue
        if p["system_component"]:
            continue

        size, newest, complete = 0, 0.0, True
        if exists:
            k = norm_key(d)
            if k not in sized:
                sized[k] = (lambda r: (r[0], r[2], r[3]))(dir_size(d, budget_s=20))
            size, newest, complete = sized[k]
        if not size and p["estimated_kb"]:
            size = int(p["estimated_kb"]) * 1024

        launched = inv.last_launch_under(d) if exists else None
        out.append(Finding(
            scanner="programs", category="program", title=name, path=d, size=size,
            mtime=newest or None, risk="medium",
            suggestion="Uninstall with its own uninstaller if no longer used.",
            command=p["uninstall"],
            evidence={
                "publisher": p["publisher"], "version": p["version"],
                "install_date": p["install_date"], "registry_key": p["key"],
                "last_launch_days_ago": age_days(launched),
                "folder_last_modified_days_ago": age_days(newest),
                "size_is_estimate": not exists or not complete,
                "installer": "msi" if p["msi"] else "exe",
            },
        ))
    return out
