"""Steam and Epic libraries with last-played dates."""
from __future__ import annotations

import glob
import json
import os
import re
import winreg

from ..store import Finding
from ..util import PROGRAMDATA, age_days, dir_size


def _vdf_pairs(text: str) -> dict[str, str]:
    """Flat key/value pairs from a Valve KeyValues file (enough for appmanifests)."""
    return {k: v for k, v in re.findall(r'"([^"]+)"\s+"([^"]*)"', text)}


def steam_libraries() -> list[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
    except OSError:
        return []
    libs = {os.path.normcase(steam): steam}
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    try:
        txt = open(vdf, encoding="utf-8", errors="replace").read()
        for p in re.findall(r'"path"\s+"([^"]+)"', txt):
            p = p.replace("\\\\", "\\")
            libs.setdefault(os.path.normcase(p), p)
    except OSError:
        pass
    return list(libs.values())


def covered_paths() -> list[str]:
    return [os.path.join(lib, "steamapps") for lib in steam_libraries()]


def scan() -> list[Finding]:
    out: list[Finding] = []
    for lib in steam_libraries():
        for acf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            try:
                kv = _vdf_pairs(open(acf, encoding="utf-8", errors="replace").read())
            except OSError:
                continue
            path = os.path.join(lib, "steamapps", "common", kv.get("installdir", ""))
            size = int(kv.get("SizeOnDisk", 0) or 0)
            last = int(kv.get("LastPlayed", 0) or 0) or None
            appid = kv.get("appid", "")
            if kv.get("name", "").startswith("Steamworks Common") or appid == "228980":
                continue
            out.append(Finding(
                scanner="games", category="game", title=f"Steam: {kv.get('name', appid)}",
                path=path, size=size, mtime=last, risk="low",
                suggestion="Re-downloadable from Steam; saves are usually in the cloud or Documents.",
                command=f"start steam://uninstall/{appid}",
                evidence={"store": "steam", "appid": appid, "library": lib,
                          "last_played_days_ago": age_days(last) if last else "never"},
            ))

    manifests = PROGRAMDATA / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    for item in glob.glob(str(manifests / "*.item")):
        try:
            m = json.load(open(item, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        loc = m.get("InstallLocation")
        if not loc or not os.path.isdir(loc):
            continue
        size = int(m.get("InstallSize") or 0)
        newest = None
        if not size:
            size, _, newest, _ = dir_size(loc, budget_s=30)
        out.append(Finding(
            scanner="games", category="game", title=f"Epic: {m.get('DisplayName')}",
            path=loc, size=size, mtime=newest, risk="low",
            suggestion="Re-downloadable from Epic. Uninstall through the Epic launcher.",
            evidence={"store": "epic", "app_name": m.get("AppName")},
        ))
    return out
