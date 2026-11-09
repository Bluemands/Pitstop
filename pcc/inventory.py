"""What's installed, and when things were last launched.

Shared by several scanners, so results are cached for the life of the process.
"""
from __future__ import annotations

import codecs
import functools
import os
import re
import struct
import winreg

from .util import PF, PF86, WINDIR, clean_path, norm_key, powershell_json

UNINSTALL_ROOTS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM32"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
]
HIVE_NAMES = {"HKLM": "HKEY_LOCAL_MACHINE", "HKLM32": "HKEY_LOCAL_MACHINE", "HKCU": "HKEY_CURRENT_USER"}


def _values(key) -> dict:
    out = {}
    i = 0
    while True:
        try:
            name, val, _ = winreg.EnumValue(key, i)
        except OSError:
            return out
        out[name] = val
        i += 1


def _install_dir(v: dict) -> str | None:
    """Best guess at where a program lives on disk."""
    loc = clean_path(v.get("InstallLocation"))
    if loc:
        # Windows components (e.g. mstsc) point at System32; that's not a program folder to size or remove.
        return None if norm_key(loc).startswith(norm_key(WINDIR)) else loc
    for k in ("DisplayIcon", "UninstallString", "QuietUninstallString"):
        p = clean_path(v.get(k))
        if not p or "msiexec" in p.lower():
            continue
        p = re.sub(r",\s*-?\d+$", "", p)  # icon index suffix
        d = os.path.dirname(p)
        dk = norm_key(d) if d else ""
        # Uninstallers often live in shared folders; those tell us nothing.
        if not d or dk.startswith(norm_key(WINDIR)) or dk in (norm_key(PF), norm_key(PF86)):
            continue
        if "package cache" in dk or "\\installer" in dk:
            continue
        return d
    return None


@functools.lru_cache(maxsize=1)
def programs() -> tuple[dict, ...]:
    """Every uninstall entry (including hidden/system ones, flagged)."""
    out = []
    for hive, sub, label in UNINSTALL_ROOTS:
        try:
            root = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, name) as k:
                    v = _values(k)
            except OSError:
                continue
            out.append({
                "key": f"{HIVE_NAMES[label]}\\{sub}\\{name}",
                "hive": label,
                "name": v.get("DisplayName"),
                "publisher": v.get("Publisher"),
                "version": v.get("DisplayVersion"),
                "install_date": v.get("InstallDate"),
                "install_dir": _install_dir(v),
                "install_location_raw": v.get("InstallLocation"),
                "uninstall": v.get("QuietUninstallString") or v.get("UninstallString"),
                "estimated_kb": v.get("EstimatedSize"),
                "system_component": bool(v.get("SystemComponent")),
                "is_update": bool(v.get("ParentKeyName")) or bool(re.search(r"\bKB\d{6,}\b", v.get("DisplayName") or "")),
                "msi": bool(v.get("WindowsInstaller")),
            })
    return tuple(out)


@functools.lru_cache(maxsize=1)
def appx_packages() -> tuple[dict, ...]:
    rows = powershell_json(
        "Get-AppxPackage | Select-Object Name, PackageFamilyName, InstallLocation, IsFramework, SignatureKind, Publisher"
    )
    return tuple(r for r in rows if isinstance(r, dict))


# ---- UserAssist: last GUI launch per executable ------------------------------

_KNOWN_FOLDERS = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": str(PF),
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": str(PF86),
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": str(WINDIR),
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": str(WINDIR / "System32"),
    "{D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27}": str(WINDIR / "SysWOW64"),
    "{0139D44E-6AFE-49F2-8690-3DAFCAE6FFB8}": os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
    "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}": os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
}
_FILETIME_EPOCH = 116444736000000000


@functools.lru_cache(maxsize=1)
def last_launches() -> dict[str, float]:
    """{normalised exe path: unix timestamp of last launch} from UserAssist."""
    out: dict[str, float] = {}
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
    try:
        ua = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
    except OSError:
        return out
    i = 0
    while True:
        try:
            guid = winreg.EnumKey(ua, i)
        except OSError:
            break
        i += 1
        try:
            count = winreg.OpenKey(ua, guid + r"\Count")
        except OSError:
            continue
        j = 0
        while True:
            try:
                name, data, _ = winreg.EnumValue(count, j)
            except OSError:
                break
            j += 1
            if not isinstance(data, bytes) or len(data) < 68:
                continue
            path = codecs.decode(name, "rot13")
            for g, real in _KNOWN_FOLDERS.items():
                if path.upper().startswith(g):
                    path = real + path[len(g):]
                    break
            if not re.match(r"[A-Za-z]:\\", path):
                continue  # AppUserModelIDs, not file paths
            ft = struct.unpack_from("<Q", data, 60)[0]
            if ft > _FILETIME_EPOCH:
                ts = (ft - _FILETIME_EPOCH) / 1e7
                k = norm_key(path)
                out[k] = max(ts, out.get(k, 0))
    return out


def last_launch_under(folder: str) -> float | None:
    """Most recent UserAssist launch of anything inside folder."""
    fk = norm_key(folder) + "\\"
    best = None
    for exe, ts in last_launches().items():
        if exe.startswith(fk) and (best is None or ts > best):
            best = ts
    return best


# ---- name tokens, for matching leftover folders to programs ------------------

_STOP = {"the", "inc", "llc", "ltd", "corporation", "corp", "software", "technologies", "games",
         "studio", "studios", "version", "edition", "x64", "x86", "64bit", "32bit", "for", "and",
         "windows", "microsoft", "update", "runtime", "tools", "app", "client", "setup", "gmbh",
         "co", "limited", "entertainment", "interactive", "data", "files", "common"}


def tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text.lower().replace("&", " "))
    toks = {w for w in words if len(w) >= 3 and w not in _STOP and not w.isdigit()}
    joined = "".join(w for w in words if not w.isdigit())
    if len(joined) >= 4:
        toks.add(joined)
    return toks


@functools.lru_cache(maxsize=1)
def known_names() -> dict[str, list[str]]:
    """token -> program names that produced it. Built from uninstall entries,
    their install folders, and Appx packages."""
    idx: dict[str, list[str]] = {}

    def add(tok_src: str | None, label: str):
        for t in tokens(tok_src):
            idx.setdefault(t, []).append(label)

    for p in programs():
        label = p["name"] or p["key"].rsplit("\\", 1)[-1]
        add(p["name"], label)
        add(p["publisher"], label)
        if p["install_dir"]:
            add(os.path.basename(p["install_dir"]), label)
            add(os.path.basename(os.path.dirname(p["install_dir"])), label)
    for a in appx_packages():
        add(a.get("Name"), a.get("Name"))
        add((a.get("Publisher") or "").split(",")[0].replace("CN=", ""), a.get("Name"))
    return idx


def installed_dirs() -> set[str]:
    return {norm_key(p["install_dir"]) for p in programs() if p["install_dir"]}
