"""PATH entries and environment variables that point at nothing."""
from __future__ import annotations

import os
import re
import winreg

from ..store import Finding

SCOPES = {
    "user": (winreg.HKEY_CURRENT_USER, r"Environment"),
    "machine": (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
}


def read_env(scope: str) -> dict[str, str]:
    hive, sub = SCOPES[scope]
    out = {}
    try:
        with winreg.OpenKey(hive, sub) as k:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if isinstance(val, str):
                    out[name] = val
    except OSError:
        pass
    return out


def _canon(entry: str) -> str:
    return os.path.normcase(os.path.expandvars(entry).replace("/", "\\").rstrip("\\"))


def scan() -> list[Finding]:
    out: list[Finding] = []
    seen: dict[str, str] = {}  # canonical entry -> scope where first seen
    # Machine PATH comes first in the effective PATH, so check it first.
    for scope in ("machine", "user"):
        env = read_env(scope)
        path_val = next((v for k, v in env.items() if k.lower() == "path"), "")
        dupes = []
        for entry in path_val.split(";"):
            if not entry.strip():
                continue
            c = _canon(entry)
            if c in seen:
                dupes.append({"entry": entry, "first_in": seen[c]})
                continue
            seen[c] = scope
            if not os.path.isdir(os.path.expandvars(entry)):
                out.append(Finding(
                    scanner="envvars", category="dead-path-entry", title=f"Dead PATH entry ({scope}): {entry}",
                    path=f"{scope}:PATH:{entry}", size=0, risk="low",
                    suggestion="Folder no longer exists; usually left by an uninstalled SDK or tool.",
                    command=f'python -m pitstop path-remove --scope {scope} "{entry}"',
                    evidence={"scope": scope, "entry": entry}))
        if dupes:
            out.append(Finding(
                scanner="envvars", category="duplicate-path-entries",
                title=f"{len(dupes)} duplicate PATH entries ({scope})", path=f"{scope}:PATH:duplicates",
                size=0, risk="low",
                suggestion="These directories are already earlier in the effective PATH, so these copies do nothing "
                           "except push PATH toward its length limit. Removing keeps the first occurrence.",
                command=f"python -m pitstop path-dedupe --scope {scope}",
                evidence={"scope": scope, "duplicates": dupes, "path_length": len(path_val)}))

        for name, val in env.items():
            if name.lower() in ("path", "pathext", "psmodulepath", "temp", "tmp"):
                continue
            # Only judge values that are a single absolute path.
            v = os.path.expandvars(val.strip().strip('"'))
            if ";" in v or not re.match(r"^[A-Za-z]:\\", v):
                continue
            if not os.path.exists(v):
                out.append(Finding(
                    scanner="envvars", category="dead-env-var", title=f"{name} ({scope}) -> {val}",
                    path=f"{scope}:{name}", size=0, risk="low",
                    suggestion="Points at a path that no longer exists. Tools may still read it and misbehave.",
                    evidence={"scope": scope, "name": name, "value": val},
                ))
    return out
