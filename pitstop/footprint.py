"""Everything an installed program has spread across the machine, for a clean uninstall.

`snapshot()` runs before the program's own uninstaller and records each folder, shortcut,
service, driver, scheduled task, startup entry, right-click handler and vendor registry key
that belongs to it. `leftovers()` re-checks that list afterwards: whatever is still there is
what the uninstaller left behind. A snapshot taken first beats guessing by name later; the
orphans scanner only sees folders over 10 MB and skips Common Files.

Matching uses the program's own names (install folder and its parent, display name,
publisher, uninstall key), plus "points into one of its folders" for anything that runs an
executable. Something that also fits another installed program is marked shared and never
offered for removal. Read-only: acting on leftovers goes through actions.py.
"""
from __future__ import annotations

import ctypes
import os
import re
import tempfile
import time
import winreg
from ctypes import wintypes
from pathlib import Path

from . import inventory as inv
from .store import Finding, connect
from .util import (HOME, LOCAL, LOCALLOW, PF, PF86, PROGRAMDATA, REPARSE, ROAMING, WINDIR,
                   clean_path, dir_size, norm_key, powershell_json, protection_reason)

PUBLIC = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
TEMP = Path(tempfile.gettempdir())
START_MENUS = [PROGRAMDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs",
               ROAMING / "Microsoft" / "Windows" / "Start Menu" / "Programs"]
DESKTOPS = [HOME / "Desktop", PUBLIC / "Desktop"]
# Where programs keep files. Each is searched one level deep, plus one more level inside a
# vendor folder that another installed program still uses.
ROOTS = [PF, PF86, PF / "Common Files", PF86 / "Common Files", PROGRAMDATA, LOCAL, LOCAL / "Programs",
         ROAMING, LOCALLOW, TEMP, PUBLIC / "Documents"]
_ROOT_KEYS = {norm_key(p) for p in ROOTS + [HOME]}

_HIVES = {"HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE, "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
          "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT}
_SVC = r"SYSTEM\CurrentControlSet\Services"
_RUN_KEYS = [
    ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    ("HKEY_LOCAL_MACHINE", r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKEY_LOCAL_MACHINE", r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    ("HKEY_LOCAL_MACHINE", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ("HKEY_LOCAL_MACHINE", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce"),
]
_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
_SHELL_BASES = ["*", "Directory", r"Directory\Background", "Folder", "Drive", "AllFilesystemObjects",
                "lnkfile", "exefile"]
_VENDOR_ROOTS = [("HKEY_CURRENT_USER", "Software"), ("HKEY_LOCAL_MACHINE", "Software"),
                 ("HKEY_LOCAL_MACHINE", r"Software\WOW6432Node")]

# Words too common to identify a program by.
_GENERIC = set(inv._STOP) | {
    "one", "free", "pro", "plus", "premium", "home", "suite", "launcher", "security", "antivirus", "driver",
    "drivers", "updater", "update", "updates", "uninstall", "uninstaller", "cleaner", "cleanup", "player",
    "manager", "service", "services", "helper", "desktop", "online", "internet", "system", "program",
    "programs", "commonfiles", "bin", "apps", "current", "shared", "temp", "cache", "config", "settings",
    "user", "users", "local", "roaming", "default", "package", "packages", "plugins", "plugin", "tool",
    "utility", "utilities", "amd64", "win64", "win32", "release", "installer", "install", "setup", "browser",
    "vpn", "web", "cloud", "sync", "assistant", "agent", "core", "engine", "framework", "sdk",
    "redistributable", "library", "server", "beta", "preview", "nightly", "portable", "standalone",
    "launch", "store", "logs", "log", "backup", "backups", "documents", "downloads", "media", "audio",
    "video", "game", "network", "device", "devices", "support", "office", "application", "applications",
}
_VERSIONISH = re.compile(r"\(.*?\)|\bv?\d+(?:[.\-_]\d+)*\b|\b(?:x64|x86|64-bit|32-bit|amd64)\b", re.I)
_CORP = re.compile(r"[,.]?\s*\b(?:inc|llc|ltd|limited|corp|corporation|co|gmbh|s\.r\.o|a\.s|ag|sa|bv|plc|pty"
                   r"|software|technologies|technology)\b\.?", re.I)
_SPLIT = re.compile(r"[\s_\-.\\/,;:()\[\]{}@'\"|]+")


# ---- names --------------------------------------------------------------------------

def squash(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def program_names(p: dict) -> dict[str, str]:
    """{squashed name: 'product' | 'vendor'} that this program's files and entries go by."""
    out: dict[str, str] = {}

    def add(s: str | None, kind: str) -> None:
        s = squash(s)
        if len(s) >= 4 and s not in _GENERIC and not s.isdigit() and out.get(s) != "product":
            out[s] = kind

    pub = p.get("publisher") or ""
    add(pub, "vendor")
    add(_CORP.sub(" ", pub), "vendor")
    base = _VERSIONISH.sub(" ", p.get("name") or "")
    add(base, "product")
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", base)
    if words:
        first = squash(words[0])
        add(first, "vendor" if first and squash(pub).startswith(first) else "product")
    d = (p.get("install_dir") or "").rstrip("\\")
    # Chrome lives in Google\Chrome\Application: the product is the first non-generic folder up.
    while d and squash(os.path.basename(d)) in _GENERIC and norm_key(os.path.dirname(d)) not in _ROOT_KEYS \
            and len(os.path.dirname(d)) > 3:
        d = os.path.dirname(d)
    if d:
        add(os.path.basename(d), "product")
        parent = os.path.dirname(d)
        if norm_key(parent) not in _ROOT_KEYS and len(norm_key(parent)) > 3:
            add(os.path.basename(parent), "vendor")
    leaf = p["key"].rsplit("\\", 1)[-1]
    if not leaf.startswith("{"):
        add(_VERSIONISH.sub(" ", leaf), "product")
    return out


def _match(text: str | None, names: dict[str, str]) -> str | None:
    """'product' or 'vendor' if text (a folder, task, service or value name) is one of the names."""
    n = squash(text)
    if len(n) < 4 or n in _GENERIC:
        return None
    kind = None
    for name, k in names.items():
        if n == name or (len(name) >= 5 and n.startswith(name)):
            if k == "product":
                return "product"
            kind = "vendor"
    return kind


def _match_words(text: str | None, names: dict[str, str]) -> str | None:
    """Like _match, but also tries each word: 'Avast Emergency Update', 'CCleaner_Update'."""
    if not text:
        return None
    best = _match(text, names)
    if best == "product":
        return best
    for w in _SPLIT.split(text):
        m = _match(w, names)
        if m == "product":
            return m
        best = best or m
    return best


# ---- other installed programs ------------------------------------------------------

def _others(p: dict) -> list[dict]:
    """Every other installed program, for the 'is this shared?' check."""
    me = (p.get("name") or "").lower()
    my_dir = norm_key(p["install_dir"]) if p.get("install_dir") else None
    out = []
    for o in inv.programs():
        if o["key"] == p["key"] or o["is_update"] or o["system_component"] or not o["name"]:
            continue
        if o["name"].lower() == me:
            continue
        od = norm_key(o["install_dir"]) if o["install_dir"] else None
        if my_dir and od and (od == my_dir or od.startswith(my_dir + "\\")):
            continue  # registered separately but lives inside this program; goes with it
        out.append({"name": o["name"], "names": program_names(o), "dir": od})
    return out


def _shared_with(text: str | None, path: str | None, others: list[dict], ours: str) -> list[str]:
    """Other installed programs this item could also belong to.

    ours = how the item matched this program: 'path' (it lives in/points into our folder),
    'product' or 'vendor'. A product-level match is only shared with another product-level
    match ("IObit Uninstaller" isn't shared with IObit Driver Booster just because both say IObit).
    """
    k = norm_key(path) if path else None
    res: list[str] = []
    for o in others:
        if k and o["dir"] and (o["dir"] == k or o["dir"].startswith(k + "\\") or k.startswith(o["dir"] + "\\")):
            res.append(o["name"])
        elif text and ours != "path":
            theirs = _match_words(text, o["names"])
            if theirs and (ours == "vendor" or theirs == "product"):
                res.append(o["name"])
        if len(res) >= 5:
            break
    return res


def _under(path: str | None, anchors: list[str]) -> bool:
    if not path:
        return False
    k = norm_key(path)
    return any(k == a or k.startswith(a + "\\") for a in anchors)


# ---- registry helpers --------------------------------------------------------------

def _split_key(path: str) -> tuple[int, str]:
    hive, _, sub = path.partition("\\")
    return _HIVES[hive.upper()], sub


def _open(path: str):
    hive, sub = _split_key(path)
    return winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)


def key_exists(path: str) -> bool:
    try:
        _open(path).Close()
        return True
    except OSError:
        return False


def reg_values(path: str) -> dict | None:
    try:
        with _open(path) as k:
            return inv._values(k)
    except OSError:
        return None


def reg_subkeys(path: str) -> list[str]:
    try:
        k = _open(path)
    except OSError:
        return []
    out, i = [], 0
    with k:
        while True:
            try:
                out.append(winreg.EnumKey(k, i))
            except OSError:
                return out
            i += 1


def _default(path: str) -> str | None:
    v = reg_values(path)
    return v.get("") if v else None


# ---- collectors --------------------------------------------------------------------

def _scandir(path) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _real_dir(e: os.DirEntry) -> bool:
    try:
        return e.is_dir(follow_symlinks=False) and not e.stat(follow_symlinks=False).st_file_attributes & REPARSE
    except OSError:
        return False


def _folder_item(path: str, match: str, shared: list[str] | None = None) -> dict:
    # Shared folders are never offered, so don't spend time sizing them (G:\SteamLibrary).
    size, files, newest, complete = (0, 0, 0.0, False) if shared else dir_size(path, budget_s=15)
    return {"kind": "folder", "id": path, "path": path, "label": path, "text": os.path.basename(path),
            "match": match, "size": size, "files": files, "size_complete": complete,
            "shared_with": shared or []}


def _folders(p: dict, names: dict, others: list[dict]) -> list[dict]:
    found: dict[str, dict] = {}
    for root in ROOTS:
        for e in _scandir(root):
            if not _real_dir(e) or protection_reason(e.path):
                continue
            m = _match(e.name, names)
            if not m:
                continue
            shared = _shared_with(e.name, e.path, others, m)
            found[norm_key(e.path)] = _folder_item(e.path, m, shared)
            if not shared:
                continue
            # A vendor folder another installed program still uses: look one level in for ours.
            for c in _scandir(e.path):
                if _real_dir(c) and _match(c.name, names) == "product":
                    found[norm_key(c.path)] = _folder_item(c.path, "product",
                                                           _shared_with(c.name, c.path, others, "product"))
    # Drive roots (Avast's "C:\avast! sandbox"). Reported only: quarantine refuses drive-root folders,
    # and a root folder with a matching name could just as well be the user's own (G:\SteamLibrary).
    for root in _fixed_drives():
        for e in _scandir(root):
            if not _real_dir(e) or e.name.lower() in _DRIVE_ROOT_SKIP:
                continue
            m = _match(e.name, names)
            if m:
                found[norm_key(e.path)] = _folder_item(e.path, m, _shared_with(e.name, e.path, others, m)) | {"at_root": True}
    d = p.get("install_dir")
    if d and os.path.isdir(d) and not protection_reason(d):
        k = norm_key(d)
        if not any(k == f or k.startswith(f + "\\") for f, it in found.items() if not it["shared_with"]):
            found[k] = _folder_item(d, "path", _shared_with(None, d, others, "path"))
    return list(found.values())


_DRIVE_ROOT_SKIP = {"windows", "program files", "program files (x86)", "programdata", "users", "pitstop-quarantine",
                    "$recycle.bin", "system volume information", "recovery", "perflogs"}


def _fixed_drives() -> list[str]:
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
            out.append(root)
    return out


def _shortcuts(names: dict, others: list[dict]) -> list[dict]:
    out = []

    def consider(e: os.DirEntry, deeper: bool) -> None:
        is_link = e.name.lower().endswith((".lnk", ".url"))
        is_dir = _real_dir(e)
        if not (is_link or is_dir):
            return
        stem = os.path.splitext(e.name)[0] if is_link else e.name
        m = _match_words(stem, names)
        if m and not _shared_with(stem, None, others, m):
            out.append({"kind": "shortcut", "id": e.path, "path": e.path, "label": e.path, "text": stem,
                        "match": m, "size": 0, "shared_with": []})
        elif is_dir and deeper:
            for c in _scandir(e.path):
                consider(c, False)

    for sm in START_MENUS:
        for e in _scandir(sm):
            if e.name.lower() != "startup":
                consider(e, True)
        for e in _scandir(sm / "Startup"):
            consider(e, False)
    for desk in DESKTOPS:
        for e in _scandir(desk):
            if e.name.lower().endswith((".lnk", ".url")):
                consider(e, False)
    return out


def _image_path(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    low = s.lower()
    if low.startswith("\\systemroot\\"):
        s = str(WINDIR) + s[len("\\SystemRoot"):]
    elif low.startswith("system32\\"):
        s = str(WINDIR / s)
    elif s.startswith("\\??\\"):
        s = s[4:]
    return clean_path(s)


_version = ctypes.WinDLL("version")
_version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(wintypes.UINT)]


def file_company(path: str | None) -> str | None:
    """CompanyName from a file's version resource. Drivers like aswSP.sys say who they belong to
    only here; their registry entries are bare."""
    if not path or not os.path.isfile(path):
        return None
    size = _version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not _version.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr, ln = ctypes.c_void_p(), wintypes.UINT()
    langs = [(0x0409, 0x04B0), (0x0409, 0x04E4), (0x0000, 0x04B0)]
    if _version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(ln)) and ln.value >= 4:
        pair = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ushort * 2)).contents
        langs.insert(0, (pair[0], pair[1]))
    for lang, cp in langs:
        if _version.VerQueryValueW(buf, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\CompanyName",
                                   ctypes.byref(ptr), ctypes.byref(ln)) and ln.value > 1 and ptr.value:
            return ctypes.wstring_at(ptr.value).strip() or None
    return None


def _services(names: dict, anchors: list[str], others: list[dict]) -> list[dict]:
    out, unmatched_drivers = [], []

    def item(key, name, disp, img, is_driver, m, company=None, why=""):
        label = f"{disp or name} ({name}{', by ' + company if company else ''}{why})"
        return {"kind": "driver" if is_driver else "service", "id": key, "name": name, "label": label,
                "text": company or disp or name, "match": m, "image": img, "shared_with": []}

    for name in reg_subkeys(f"HKEY_LOCAL_MACHINE\\{_SVC}"):
        key = f"HKEY_LOCAL_MACHINE\\{_SVC}\\{name}"
        v = reg_values(key) or {}
        img = _image_path(v.get("ImagePath"))
        disp = v.get("DisplayName")
        if isinstance(disp, str) and disp.startswith("@"):
            disp = None
        typ = v.get("Type") or 0
        is_driver = isinstance(typ, int) and 0 < typ < 0x10
        company = None
        if _under(img, anchors):
            m = "path"
        else:
            m = _match_words(disp, names) or _match(name, names)
            text = disp or name
            if not m and is_driver:
                company = file_company(img)
                m, text = _match_words(company, names), company
            if not m:
                if is_driver:
                    unmatched_drivers.append((key, name, disp, img, company))
                continue
            if _shared_with(text, None, others, m):
                continue
        out.append(item(key, name, disp, img, is_driver, m, company))

    # Drivers that name neither the product nor its brand (Avast's aswSP.sys says "Gen Digital Inc.,
    # Antivirus"): claim those sharing both the publisher and the name prefix of this program's
    # identified services/drivers.
    companies = {c for c in (file_company(i["image"]) for i in out if i.get("image"))
                 if c and not squash(c).startswith("microsoft")}
    prefixes = {i["name"][:3].lower() for i in out if len(i["name"]) >= 4 and i["name"][:3].isalpha()}
    if companies and prefixes:
        for key, name, disp, img, company in unmatched_drivers:
            if name[:3].lower() not in prefixes:
                continue
            company = company or file_company(img)
            if company in companies and not _shared_with(company, None, others, "vendor"):
                out.append(item(key, name, disp, img, True, "vendor", company, ", same publisher and prefix"))
    return out


_TASKS_PS = ("Get-ScheduledTask | ForEach-Object { [pscustomobject]@{ p = $_.TaskPath + $_.TaskName; "
             "x = (@($_.Actions | ForEach-Object { $_.Execute }) -join '|'); a = $_.Author } }")


def all_tasks() -> list[dict]:
    return [t for t in powershell_json(_TASKS_PS, timeout=180) if isinstance(t, dict)]


def _tasks(names: dict, anchors: list[str], others: list[dict]) -> list[dict]:
    out = []
    for t in all_tasks():
        path = t.get("p") or ""
        exes = [clean_path(x) for x in (t.get("x") or "").split("|") if x.strip()]
        if any(_under(e, anchors) for e in exes):
            m = "path"
        else:
            if path.lower().startswith("\\microsoft\\"):
                continue
            leaf = path.rsplit("\\", 1)[-1]
            m = _match_words(leaf, names) or _match_words(t.get("a"), names)
            if not m or _shared_with(leaf, None, others, m):
                continue
        out.append({"kind": "task", "id": path, "label": f"Scheduled task {path}", "text": path.rsplit("\\", 1)[-1],
                    "match": m, "exe": exes[0] if exes else None, "shared_with": []})
    return out


def _run_entries(names: dict, anchors: list[str], others: list[dict]) -> list[dict]:
    out = []
    for hive, sub in _RUN_KEYS:
        key = f"{hive}\\{sub}"
        for value, data in (reg_values(key) or {}).items():
            if not value:
                continue
            exe = clean_path(data) if isinstance(data, str) else None
            if _under(exe, anchors):
                m = "path"
            else:
                m = _match_words(value, names)
                if not m or _shared_with(value, None, others, m):
                    continue
            out.append({"kind": "run", "id": f"{key}|{value}", "key": key, "value": value, "text": value,
                        "label": f"Startup entry '{value}' ({key.split(chr(92))[0]} {sub.rsplit(chr(92), 1)[-1]})",
                        "match": m, "data": data if isinstance(data, str) else None, "shared_with": []})
    return out


def _startup_approved(names: dict, others: list[dict]) -> list[dict]:
    out = []
    for hive in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"):
        for leaf in ("Run", "Run32", "StartupFolder"):
            key = f"{hive}\\{_APPROVED}\\{leaf}"
            for value in (reg_values(key) or {}):
                stem = os.path.splitext(value)[0] if leaf == "StartupFolder" else value
                m = _match_words(stem, names)
                if not value or not m or _shared_with(stem, None, others, m):
                    continue
                out.append({"kind": "startup-approved", "id": f"{key}|{value}", "key": key, "value": value,
                            "text": stem, "label": f"Startup on/off flag '{value}'", "match": m, "shared_with": []})
    return out


def _clsid_server(clsid: str | None) -> str | None:
    if not clsid or not clsid.startswith("{"):
        return None
    for base in ("CLSID", r"WOW6432Node\CLSID"):
        dll = _default(f"HKEY_CLASSES_ROOT\\{base}\\{clsid}\\InprocServer32")
        if dll:
            return clean_path(dll)
    return None


def _shell_handlers(names: dict, anchors: list[str], others: list[dict]) -> list[dict]:
    out = []
    for base in _SHELL_BASES:
        hkey = f"HKEY_CLASSES_ROOT\\{base}\\shellex\\ContextMenuHandlers"
        for sub in reg_subkeys(hkey):
            clsid = sub if sub.startswith("{") else _default(f"{hkey}\\{sub}")
            if _under(_clsid_server(clsid), anchors):
                m = "path"
            else:
                m = _match_words(sub, names)
                if not m or _shared_with(sub, None, others, m):
                    continue
            out.append({"kind": "shell-ext", "id": f"{hkey}\\{sub}", "label": f"Right-click handler '{sub}' ({base})",
                        "text": sub, "match": m, "clsid": clsid, "shared_with": []})
        vkey = f"HKEY_CLASSES_ROOT\\{base}\\shell"
        for verb in reg_subkeys(vkey):
            cmd = _default(f"{vkey}\\{verb}\\command")
            if _under(clean_path(cmd), anchors):
                m = "path"
            else:
                m = _match_words(verb, names)
                if not m or _shared_with(verb, None, others, m):
                    continue
            out.append({"kind": "shell-verb", "id": f"{vkey}\\{verb}", "label": f"Right-click command '{verb}' ({base})",
                        "text": verb, "match": m, "shared_with": []})
    return out


def _vendor_keys(names: dict, others: list[dict]) -> list[dict]:
    out = []
    for hive, sub in _VENDOR_ROOTS:
        for name in reg_subkeys(f"{hive}\\{sub}"):
            m = _match(name, names)
            if not m:
                continue
            out.append({"kind": "registry", "id": f"{hive}\\{sub}\\{name}", "label": f"{hive}\\{sub}\\{name}",
                        "text": name, "match": m, "shared_with": _shared_with(name, None, others, m)})
    return out


# ---- public API --------------------------------------------------------------------

def snapshot(p: dict) -> dict:
    """Everything that belongs to program p (an inventory.programs() row), before uninstalling."""
    names = program_names(p)
    others = _others(p)
    folders = _folders(p, names, others)
    anchors = [norm_key(f["path"]) for f in folders if not f["shared_with"] or f["match"] == "path"]
    if p.get("install_dir"):
        anchors.append(norm_key(p["install_dir"]))
    items = (folders + _shortcuts(names, others) + _services(names, anchors, others)
             + _tasks(names, anchors, others) + _run_entries(names, anchors, others)
             + _startup_approved(names, others) + _shell_handlers(names, anchors, others)
             + _vendor_keys(names, others))
    items.append({"kind": "uninstall-entry", "id": p["key"], "label": "Apps & Features entry",
                  "match": "path", "shared_with": []})
    return {"program": {k: p.get(k) for k in ("name", "key", "publisher", "version", "install_dir", "uninstall", "msi")},
            "names": names, "items": items, "ts": time.time()}


def leftovers(snap: dict) -> list[dict]:
    """Snapshot items still present, with sharing re-checked against what's installed now."""
    inv.programs.cache_clear()
    others = _others(snap["program"])
    tasks: set[str] | None = None
    out = []
    for item in snap["items"]:
        it = dict(item)
        kind = it["kind"]
        if kind in ("folder", "shortcut"):
            if not os.path.lexists(it["path"]):
                continue
            if os.path.isdir(it["path"]):
                size, files, _, complete = dir_size(it["path"], budget_s=15)
                it.update(size=size, files=files, size_complete=complete)
        elif kind in ("service", "driver"):
            v = reg_values(it["id"])
            if v is None:
                continue
            it["pending_delete"] = bool(v.get("DeleteFlag"))
        elif kind == "task":
            if tasks is None:
                tasks = {t.get("p") for t in all_tasks()}
            if it["id"] not in tasks:
                continue
        elif kind in ("run", "startup-approved"):
            v = reg_values(it["key"])
            if not v or it["value"] not in v:
                continue
        elif not key_exists(it["id"]):
            continue
        if kind != "uninstall-entry":
            it["shared_with"] = _shared_with(it.get("text"), it.get("path"), others, it.get("match", "product"))
        out.append(it)
    # A folder inside a leftover folder goes with it.
    tops = [norm_key(i["path"]) for i in out if i.get("path") and not i["shared_with"]]
    return [i for i in out if not (i.get("path") and any(norm_key(i["path"]).startswith(t + "\\") for t in tops))]


def describe(items: list[dict]) -> dict:
    """Compact view of a snapshot for a dry run: what would be checked, and what's shared."""
    from .util import human
    own, shared = [], []
    for i in items:
        row = {"kind": i["kind"], "what": i["label"]}
        if i.get("size"):
            row["size"] = human(i["size"])
        if i.get("shared_with"):
            shared.append(row | {"shared_with": i["shared_with"]})
        else:
            own.append(row)
    return {"belongs_to_program": own, "shared_left_alone": shared,
            "total_size": human(sum(i.get("size") or 0 for i in items if not i.get("shared_with")))}


def busy_processes(snap: dict, since: float) -> list[str]:
    """Processes the uninstaller started that are still running (Inno and NSIS uninstallers
    copy themselves to Temp and exit straight away; others hand off to a child)."""
    anchors = [norm_key(i["path"]) for i in snap["items"] if i["kind"] == "folder"]
    if snap["program"].get("install_dir"):
        anchors.append(norm_key(snap["program"]["install_dir"]))
    temps = [norm_key(TEMP), norm_key(WINDIR / "Temp")]
    rows = powershell_json("Get-CimInstance Win32_Process | Select-Object Name, ExecutablePath, "
                           "@{n='c';e={[DateTimeOffset]::new($_.CreationDate).ToUnixTimeSeconds()}}", timeout=60)
    busy = []
    for r in rows:
        exe = r.get("ExecutablePath")
        if not exe or (r.get("c") or 0) < since - 2:
            continue
        if _under(exe, anchors) or _under(exe, temps) or (r.get("Name") or "").lower() == "msiexec.exe":
            busy.append(r.get("Name") or exe)
    return busy


def wait_for_uninstaller(snap: dict, since: float, timeout_s: float = 900) -> list[str]:
    """Wait until nothing the uninstaller spawned is still running. Returns stragglers at timeout."""
    deadline = time.monotonic() + timeout_s
    quiet = 0
    busy: list[str] = []
    while time.monotonic() < deadline:
        busy = busy_processes(snap, since)
        quiet = 0 if busy else quiet + 1
        if quiet >= 2:
            return []
        time.sleep(3)
    return busy


_SUGGEST = {
    "folder": "Left behind by {prog}'s uninstaller. Quarantine it (restorable until purge).",
    "shortcut": "Shortcut left behind by {prog}. Quarantine it.",
    "task": "Scheduled task left behind by {prog}. `leftover-remove` deletes it (XML backup kept).",
    "run": "Startup entry left behind by {prog}. `leftover-remove` deletes it (value backed up).",
    "startup-approved": "Task Manager on/off flag for a startup entry that no longer exists. Harmless; "
                        "`leftover-remove` tidies it.",
    "shell-ext": "Right-click menu handler left behind by {prog}. `leftover-remove` deletes it (.reg backup kept).",
    "shell-verb": "Right-click menu command left behind by {prog}. `leftover-remove` deletes it (.reg backup kept).",
    "registry": "{prog}'s settings key (a few KB). Left alone: Pitstop doesn't do registry cleaning.",
}


def to_finding(item: dict, prog: str, journal_id: int) -> Finding:
    kind = item["kind"]
    command = None
    if kind in ("service", "driver"):
        if item.get("pending_delete"):
            suggestion = f"Marked for deletion by {prog}'s uninstaller; Windows removes it at the next restart."
        else:
            suggestion = (f"{kind.title()} still registered after {prog} was uninstalled. If its file is gone, "
                          "remove it from an elevated terminal.")
            command = f"sc.exe delete \"{item['name']}\""
    elif item.get("at_root"):
        suggestion = (f"Folder at a drive root matching {prog}. Pitstop never moves drive-root folders, and it "
                      f"could be your own data: check it, then delete it yourself if it's really {prog}'s.")
    elif kind == "folder" and _under(item.get("path"), [norm_key(TEMP)]):
        suggestion = f"Temporary files left by {prog}. Quarantine it."
    elif kind == "folder" and _under(item.get("path"), [norm_key(LOCAL), norm_key(ROAMING), norm_key(LOCALLOW)]):
        suggestion = (f"{prog}'s settings/data in AppData (profiles, saves, caches). Quarantine it unless you "
                      "might reinstall and want them back.")
    else:
        suggestion = _SUGGEST.get(kind, "Left behind by {prog}.").format(prog=prog)
    risk = "low" if item.get("match") in ("path", "product") else "medium"
    if (kind == "driver" and not item.get("pending_delete")) or item.get("at_root"):
        risk = "high"
    return Finding(
        scanner="uninstall", category="uninstall-leftover", title=f"{prog}: {item['label']}",
        path=item.get("path") or item["id"], size=item.get("size") or 0, risk=risk,
        suggestion=suggestion, command=command,
        evidence={"program": prog, "kind": kind, "journal_id": journal_id, "match": item.get("match"),
                  "pending_reboot": bool(item.get("pending_delete")), "files": item.get("files"),
                  **{k: item[k] for k in ("key", "value", "name", "image", "exe", "clsid") if item.get(k)}},
    )


def covered_paths() -> list[str]:
    """Leftover folders already reported by an uninstall, so the orphans scanner doesn't repeat them."""
    rows = connect().execute("SELECT path FROM findings WHERE scanner='uninstall' AND status NOT IN ('gone', 'done')")
    return [r["path"] for r in rows if r["path"] and r["path"][1:3] == ":\\"]
