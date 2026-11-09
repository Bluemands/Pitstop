"""Shared helpers: path normalisation, folder sizing, safety checks, subprocess."""
from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path

REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
# Cloud placeholders (OneDrive etc.) report a size but occupy nothing locally.
CLOUD_ONLY = getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", 0x400000) | stat.FILE_ATTRIBUTE_OFFLINE

HOME = Path(os.path.expanduser("~"))
LOCAL = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local"))
ROAMING = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming"))
LOCALLOW = HOME / "AppData" / "LocalLow"
PROGRAMDATA = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
PF = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
PF86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))


def clean_path(raw: str | None) -> str | None:
    """Turn a registry/env path value into something Path() can test.

    Handles surrounding quotes, %VARS%, trailing args on exe strings
    ("C:\\x\\uninst.exe" /S), and mixed slashes.
    """
    if not raw:
        return None
    s = os.path.expandvars(raw.strip())
    if s.startswith('"'):
        end = s.find('"', 1)
        s = s[1:end] if end > 0 else s[1:]
    else:
        # Unquoted exe with arguments: cut at the first ".exe"
        m = re.match(r"(.+?\.exe)\b", s, re.IGNORECASE)
        if m:
            s = m.group(1)
    s = s.strip().rstrip("\\/").replace("/", "\\")
    if len(s) == 2 and s[1] == ":":
        s += "\\"
    return s or None


def norm_key(path: str | Path) -> str:
    """Case-insensitive comparable form of a path."""
    return os.path.normcase(os.path.abspath(str(path))).rstrip("\\")


def dir_size(path: str | Path, budget_s: float | None = None) -> tuple[int, int, float, bool]:
    """Return (bytes, files, newest_mtime, complete) for a folder tree.

    Skips junctions/symlinks (so nothing is counted twice) and cloud-only
    placeholders. Stops early if budget_s is exceeded (complete=False).
    """
    total = files = 0
    newest = 0.0
    start = time.monotonic()
    stack = [str(path)]
    while stack:
        if budget_s and time.monotonic() - start > budget_s:
            return total, files, newest, False
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                if attrs & REPARSE:
                    continue
                if st.st_mtime > newest:
                    newest = st.st_mtime
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif not attrs & CLOUD_ONLY:
                    total += st.st_size
                    files += 1
    return total, files, newest, True


def file_size(path: str | Path) -> int:
    """Size of a single file, falling back to the directory listing for locked
    system files like pagefile.sys that can't be opened."""
    p = Path(path)
    try:
        return p.stat().st_size
    except OSError:
        pass
    try:
        with os.scandir(p.parent) as it:
            for e in it:
                if e.name.lower() == p.name.lower():
                    return e.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    return 0


def human(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def age_days(ts: float | None) -> int | None:
    if not ts:
        return None
    return int((time.time() - ts) // 86400)


def run(cmd: list[str], timeout: int = 60) -> str:
    """Run a command and return stdout (decoded leniently). Empty string on failure."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    # wsl.exe and some Windows tools emit UTF-16LE.
    if out[:2] == b"\xff\xfe" or (len(out) > 1 and out[1:2] == b"\x00"):
        return out.decode("utf-16-le", errors="replace").replace("\x00", "")
    return out.decode("utf-8", errors="replace")


def powershell_json(script: str, timeout: int = 120):
    import json
    out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
               f"{script.strip()} | ConvertTo-Json -Depth 4 -Compress"], timeout=timeout)
    try:
        data = json.loads(out) if out.strip() else []
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


# ---- safety -----------------------------------------------------------------

_PROTECTED_EXACT = {
    norm_key(p) for p in [
        HOME, LOCAL, ROAMING, LOCALLOW, PROGRAMDATA, PF, PF86, WINDIR,
        HOME / "Documents", HOME / "Desktop", HOME / "Downloads", HOME / "Pictures",
        HOME / "Videos", HOME / "Music", HOME / "OneDrive",
        Path(os.environ.get("SystemDrive", "C:") + "\\Users"),
        PF / "Common Files", PF86 / "Common Files",
    ]
}
_PROTECTED_TREES = [norm_key(p) for p in [
    WINDIR,
    PROGRAMDATA / "Microsoft",
    PF / "WindowsApps", PF / "Windows Defender",
    LOCAL / "Microsoft" / "Windows",
    ROAMING / "Microsoft" / "Windows",
]]
# Inside Common Files, vendor folders (Adobe, Oracle...) are fair game once their product is gone;
# Microsoft's shared components and system pieces are not.
_COMMON_FILES = [norm_key(PF / "Common Files"), norm_key(PF86 / "Common Files")]
_COMMON_FILES_SYSTEM = ("microsoft", "windows", "system", "services", "speechengines", "designer", "mssoap")
# Start-menu shortcuts live inside protected trees, but a program's own shortcut or folder
# there is fair game once it's uninstalled. Windows' own folders are not.
_START_MENUS = [norm_key(PROGRAMDATA / "Microsoft/Windows/Start Menu/Programs"),
                norm_key(ROAMING / "Microsoft/Windows/Start Menu/Programs")]
_START_MENU_SYSTEM = {"accessibility", "accessories", "administrative tools", "maintenance", "system tools",
                      "windows powershell", "windows tools", "startup"}


def _start_menu_entry(k: str) -> bool:
    for sm in _START_MENUS:
        if k.startswith(sm + "\\"):
            parts = k[len(sm) + 1:].split("\\")
            if parts[0] == "startup":
                return len(parts) == 2  # a shortcut in Startup, not the folder itself
            return parts[0] not in _START_MENU_SYSTEM
    return False


def protection_reason(path: str | Path) -> str | None:
    """Why a path must never be removed, or None if it's eligible."""
    k = norm_key(path)
    if re.fullmatch(r"[a-z]:\\?", k):
        return "drive root"
    if k in _PROTECTED_EXACT:
        return "top-level system/profile folder"
    for t in _PROTECTED_TREES:
        if (k == t or k.startswith(t + "\\")) and not _start_menu_entry(k):
            return f"inside protected tree {t}"
    for cf in _COMMON_FILES:
        if k.startswith(cf + "\\") and k[len(cf) + 1:].split("\\")[0].startswith(_COMMON_FILES_SYSTEM):
            return "Microsoft/system shared component in Common Files"
    if k.count("\\") < 2:
        # Stray temp/dump files at a root are fine; pagefile.sys, bootmgr, folders etc. are not.
        if os.path.isfile(path) and os.path.splitext(k)[1] in (".tmp", ".dmp"):
            return None
        return "too close to the drive root"
    return None
