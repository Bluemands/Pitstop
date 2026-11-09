"""Folders that don't belong to anything installed.

Heuristic: a folder is "claimed" if it is (or contains, or sits in) a
registered install dir, or its name shares a token with an installed
program / publisher / Store package. Everything unclaimed and big enough is
reported with evidence; deciding whether it's really forgotten is the
conversation with Claude.
"""
from __future__ import annotations

import os
import string

from .. import footprint
from .. import inventory as inv
from ..store import Finding
from ..util import (HOME, LOCAL, LOCALLOW, PF, PF86, PROGRAMDATA, REPARSE, ROAMING,
                    age_days, dir_size, norm_key)
from . import containers, devtools, games, system

MIN_BYTES = 10 * 1024 * 1024

# Folder names that are part of Windows / the profile, per location (lowercase).
_COMMON_SKIP = {"microsoft", "packages", "temp", "windows", "desktop.ini", "comms",
                "connecteddevicesplatform", "crashdumps", "d3dscache", "peerdistrepub",
                "virtualstore", "publishers", "history", "temporary internet files",
                "application data", "microsoft help", "microsoftedge", "packagemanagement",
                "isolatedstorage", "programs", "placeholdertilelogofolder", "sym", "fontcache",
                "ssh", "regid.1991-06.com.microsoft", "usoshared", "usoprivate", "softwaredistribution",
                "package cache", "windowsholographicdevices", "device stage", "oem", "start menu",
                "templates", "desktop", "documents", "favorites", "intel", "nvidia", "nvidia corporation",
                "amd", "realtek", "common files", "windowsapps", "modifiablewindowsapps",
                "windows defender", "windows defender advanced threat protection", "windows mail",
                "windows media player", "windows nt", "windows photo viewer", "windows portable devices",
                "windows security", "windows sidebar", "windowspowershell", "internet explorer",
                "reference assemblies", "msbuild", "dotnet", "uninstall information", "microsoft.net",
                "microsoft update health tools", "windows kits", "application verifier",
                "microsoft sdks", "iis", "iis express", "windows multimedia platform",
                "ssh", "mozilla maintenance service", "lowregistry", "wsl", "hyper-v", "windows sandbox",
                "npm", "uv", "pip"}
_ROOT_SKIP = {"windows", "program files", "program files (x86)", "programdata", "users", "perflogs",
              "recovery", "system volume information", "$recycle.bin", "$winreagent", "config.msi",
              "documents and settings", "onedrivetemp", "$sysreset", "$getcurrent", "msocache",
              "inetpub", "windows.old", "$windows.~bt", "$windows.~ws", "boot", "efi", "$av_asw",
              "pitstop-quarantine", "drivers", "xboxgames", "$extend"}
_HOME_SKIP = {".cache", ".config", ".local", ".ssh", ".gitconfig", ".vscode", ".claude", ".android",
              ".nuget", ".gradle", ".m2", ".cargo", ".rustup", ".dotnet", ".ollama", ".lmstudio",
              ".jdks", ".bun"}  # containers of things other scanners size individually


def _locations() -> list[tuple[str, str, set[str], str]]:
    """(parent dir, label, skip names, risk)"""
    locs = [
        (str(LOCAL), "AppData\\Local", _COMMON_SKIP, "medium"),
        (str(LOCAL / "Programs"), "AppData\\Local\\Programs", _COMMON_SKIP, "medium"),
        (str(ROAMING), "AppData\\Roaming", _COMMON_SKIP, "medium"),
        (str(LOCALLOW), "AppData\\LocalLow", _COMMON_SKIP, "medium"),
        (str(PROGRAMDATA), "ProgramData", _COMMON_SKIP, "medium"),
        (str(PF), "Program Files", _COMMON_SKIP, "medium"),
        (str(PF86), "Program Files (x86)", _COMMON_SKIP, "medium"),
        (str(HOME), "user profile (dot-folders)", _HOME_SKIP, "medium"),
    ]
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.isdir(root):
            locs.append((root, f"{letter}: root", _ROOT_SKIP, "high"))
    return locs


def _claimed_by_install(key: str, installed: set[str]) -> bool:
    for d in installed:
        if d == key or d.startswith(key + "\\") or key.startswith(d + "\\"):
            return True
    return False


def _name_matches(folder: str, names: dict[str, list[str]]) -> bool:
    toks = inv.tokens(folder)
    if any(t in names for t in toks):
        return True
    # "LGHUBData" vs "lghub", "ow-electron" vs "overwolf" won't token-match; allow
    # prefix overlap on the squashed name for reasonably distinctive tokens.
    squashed = "".join(ch for ch in folder.lower() if ch.isalnum())
    if len(squashed) < 5:
        return False
    return any(len(k) >= 5 and (squashed.startswith(k) or k.startswith(squashed)) for k in names)


def scan() -> list[Finding]:
    installed = inv.installed_dirs()
    names = inv.known_names()
    covered = [norm_key(p) for mod in (devtools, containers, games, system, footprint) for p in mod.covered_paths()]
    out: list[Finding] = []

    for parent, label, skip, risk in _locations():
        try:
            entries = list(os.scandir(parent))
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False) or e.stat(follow_symlinks=False).st_file_attributes & REPARSE:
                    continue
            except OSError:
                continue
            lname = e.name.lower()
            if lname in skip:
                continue
            if parent == str(HOME) and not lname.startswith("."):
                continue  # non-dot profile folders are the user's own stuff
            key = norm_key(e.path)
            if key == norm_key(LOCAL / "Programs"):
                continue
            if any(c == key or c.startswith(key + "\\") or key.startswith(c + "\\") for c in covered):
                continue
            if _claimed_by_install(key, installed):
                continue
            if _name_matches(e.name, names):
                continue

            size, files, newest, complete = dir_size(e.path, budget_s=60)
            if size < MIN_BYTES:
                continue
            launched = inv.last_launch_under(e.path)
            out.append(Finding(
                scanner="orphans",
                category="unrecognised-root-folder" if risk == "high" else "orphan-folder",
                title=f"{label}\\{e.name}", path=e.path, size=size, mtime=newest or None, risk=risk,
                suggestion=("Top-level folder on a drive root: could be your own data. Check before touching."
                            if risk == "high" else
                            "No installed program matches this folder name. Likely left behind by an uninstall."),
                evidence={"location": label, "files": files, "size_complete": complete,
                          "last_modified_days_ago": age_days(newest),
                          "last_launch_days_ago": age_days(launched)},
            ))
    return out
