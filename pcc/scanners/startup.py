"""What starts with Windows: startup apps, sign-in/boot scheduled tasks, automatic services.

Read-only. Turning things off goes through actions.startup_disable, which uses the same
switches as Task Manager (StartupApproved flags), Task Scheduler (disable) and the service
manager (Manual, never Disabled). Broken entries (program gone, leftover on/off switches)
are a separate category, cleaned up by actions.startup_fix.
"""
from __future__ import annotations

import os
import re
import shutil

from ..footprint import _image_path, file_company, reg_values
from ..store import Finding
from ..util import WINDIR, clean_path, norm_key, powershell_json

_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
# (where the entry lives, label, its Task Manager on/off key)
RUN_KEYS = [
    (f"HKEY_CURRENT_USER\\{_RUN}", "Run key (you)", f"HKEY_CURRENT_USER\\{_APPROVED}\\Run"),
    (f"HKEY_LOCAL_MACHINE\\{_RUN}", "Run key (all users)", f"HKEY_LOCAL_MACHINE\\{_APPROVED}\\Run"),
    (f"HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Run", "Run key (all users, 32-bit)",
     f"HKEY_LOCAL_MACHINE\\{_APPROVED}\\Run32"),
]
APPROVAL_KEYS = [f"{h}\\{_APPROVED}\\{leaf}" for h in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE")
                 for leaf in ("Run", "Run32", "StartupFolder")]

# Turning these off can stop fans, RGB or device settings working.
_HARDWARE = re.compile(r"corsair|logitech|razer|gigabyte|asus|micro-star|\bmsi\b|nvidia|advanced micro|\bamd\b|"
                       r"\bintel\b|realtek|samsung|elgato|steelseries|hyperx|creative|dolby|\bthx\b|synaptics|"
                       r"fan ?control|\bfan\b|rgb|icue|lghub|g hub|synapse|armoury|easytune|afterburner|signalrgb", re.I)
# Security software protects itself; turn its parts off in the app, never from here.
_SECURITY = re.compile(r"malwarebytes|avast|\bavg\b|norton|gen digital|kaspersky|eset|bitdefender|mcafee|sophos|"
                       r"trend micro|webroot|f-secure|panda security|emsisoft|windows defender|microsoft defender", re.I)
_UPDATER = re.compile(r"updat", re.I)
_PROGRAM_FILE = re.compile(r"\.(exe|com|bat|cmd|vbs|ps1|lnk)$", re.I)

_GATHER = r"""
$procs = Get-CimInstance Win32_Process | Select-Object ProcessId, ExecutablePath, WorkingSetSize
$svcs = Get-CimInstance Win32_Service | Where-Object { $_.StartMode -eq 'Auto' } |
  Select-Object Name, DisplayName, PathName, State, ProcessId
$tasks = Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\Microsoft\*' -and $_.State -ne 'Disabled' } |
  ForEach-Object {
    $tr = @($_.Triggers | ForEach-Object { $_.CimClass.CimClassName })
    if (($tr -join ',') -match 'Logon|Boot') {
      [pscustomobject]@{ p = $_.TaskPath + $_.TaskName; x = (@($_.Actions | ForEach-Object { $_.Execute }) -join '|');
                         w = (@($_.Actions | ForEach-Object { $_.WorkingDirectory }) -join '|');
                         a = (@($_.Actions | ForEach-Object { $_.Arguments }) -join ' '); au = $_.Author;
                         tr = (($tr -join ',') -replace 'MSFT_Task|Trigger', '') }
    } }
$sh = New-Object -ComObject WScript.Shell
$mine = [Environment]::GetFolderPath('Startup')
$links = foreach ($d in @($mine, [Environment]::GetFolderPath('CommonStartup'))) {
  Get-ChildItem -LiteralPath $d -File -Force -ErrorAction SilentlyContinue | Where-Object Name -ne 'desktop.ini' |
    ForEach-Object {
      $t = if ($_.Extension -eq '.lnk') { $sh.CreateShortcut($_.FullName) } else { $null }
      [pscustomobject]@{ path = $_.FullName; name = $_.Name; target = $t.TargetPath; args = $t.Arguments;
                         common = ($d -ne $mine) } } }
[pscustomobject]@{ procs = @($procs); services = @($svcs); tasks = @($tasks); links = @($links) }
"""


def _enabled(flag) -> bool:
    """Task Manager's StartupApproved value: missing or an even first byte = on."""
    return not (isinstance(flag, bytes) and flag and flag[0] & 1)


def _exe_of(command: str | None) -> str | None:
    exe = clean_path(command)
    if exe and os.path.basename(exe).lower() == "rundll32.exe":  # the DLL is the real program
        m = re.search(r"rundll32(?:\.exe)?\"?\s+\"?([^,\"]+)", command or "", re.I)
        exe = clean_path(m.group(1)) if m else exe
    return exe


def _windows_component(exe: str | None) -> bool:
    return bool(exe) and norm_key(exe).startswith(norm_key(WINDIR) + "\\")


def _is_microsoft(company: str | None) -> bool:
    # Some Microsoft parts (GameInput, Xbox) are signed "Windows (R) Win 7 DDK provider".
    return bool(company) and company.lower().startswith(("microsoft", "windows (r)"))


def _task_exe(t: dict) -> str | None:
    """A task's program. Some name just "FanControl.exe" and rely on their working folder."""
    exe = next((clean_path(x) for x in (t.get("x") or "").split("|") if x.strip()), None)
    if exe and not os.path.isabs(exe):
        workdir = clean_path((t.get("w") or "").split("|")[0])
        found = os.path.join(workdir, exe) if workdir else None
        exe = found if found and os.path.exists(found) else shutil.which(exe)
    return exe


def _notes(name: str, publisher: str | None) -> tuple[list[str], bool, str | None]:
    """(extra notes, is hardware software, protected reason)."""
    text = f"{name} {publisher or ''}"
    notes = []
    hardware = bool(_HARDWARE.search(text))
    protected = "security software: change it in the app itself" if _SECURITY.search(text) else None
    if hardware:
        notes.append("Peripheral, fan or GPU software: turning it off can stop RGB lighting, fan curves or "
                     "device settings from applying.")
    if _UPDATER.search(name) and not hardware:
        notes.append("An updater: without it the app updates when you open it, or from PC Cleaner's Updates page.")
    return notes, hardware, protected


def scan() -> list[Finding]:
    data = (powershell_json(_GATHER, timeout=180) or [{}])[0]
    ram: dict[str, int] = {}
    pid_ram: dict[int, int] = {}
    for p in data.get("procs") or []:
        ws = p.get("WorkingSetSize") or 0
        pid_ram[p.get("ProcessId")] = ws
        if p.get("ExecutablePath"):
            k = norm_key(p["ExecutablePath"])
            ram[k] = ram.get(k, 0) + ws
    approvals = {k: (reg_values(k) or {}) for k in APPROVAL_KEYS}
    used_flags: set[tuple[str, str]] = set()
    out: list[Finding] = []
    apps: list[dict] = []

    # ---- Run keys and Startup folders -----------------------------------------------
    for key, where, akey in RUN_KEYS:
        for name, cmd in (reg_values(key) or {}).items():
            if not name:
                continue  # the key's (default) value, not an entry
            used_flags.add((akey, name))
            apps.append({"name": name, "command": cmd if isinstance(cmd, str) else "", "where": where,
                         "key": key, "value": name, "akey": akey, "flag": approvals[akey].get(name)})
    for ln in data.get("links") or []:
        akey = f"{'HKEY_LOCAL_MACHINE' if ln.get('common') else 'HKEY_CURRENT_USER'}\\{_APPROVED}\\StartupFolder"
        used_flags.add((akey, ln["name"]))
        target = ln.get("target") or ""
        apps.append({"name": os.path.splitext(ln["name"])[0], "command": f'"{target}" {ln.get("args") or ""}'.strip()
                     if target else ln["path"], "where": "Startup folder" + (" (all users)" if ln.get("common") else " (you)"),
                     "link": ln["path"], "akey": akey, "value": ln["name"], "flag": approvals[akey].get(ln["name"]),
                     "target": target})

    live = [a for a in apps if _enabled(a["flag"])]
    by_exe: dict[str, list[dict]] = {}
    for a in live:
        exe = _exe_of(a.get("target") or a["command"])
        a["missing"] = bool(exe) and not os.path.exists(exe) and bool(_PROGRAM_FILE.search(exe))
        a["exe"] = exe if exe and (a["missing"] or os.path.exists(exe)) else None  # unparseable command: unknown
        if a["exe"] and not a["missing"]:
            by_exe.setdefault(norm_key(a["exe"]), []).append(a)

    for a in live:
        exe = a["exe"]
        if _windows_component(exe):
            continue
        ident = f"{a['where']}|{a['value']}"
        if not a["command"].strip() or a["missing"]:
            out.append(_broken(a, ident))
            continue
        publisher = file_company(exe)
        notes, hardware, protected = _notes(a["name"], publisher)
        dupes = [d["where"] for d in by_exe.get(norm_key(exe), []) if d is not a] if exe else []
        if dupes:
            notes.insert(0, f"Also starts from {', '.join(dupes)}: it's registered twice, so turning this copy off is harmless.")
        mb = round(ram.get(norm_key(exe), 0) / 2**20) if exe else 0
        out.append(Finding(
            scanner="startup", category="startup", title=a["name"], path=None, size=0,
            risk="high" if (hardware or protected) else "low",
            suggestion=" ".join([f"Starts when you sign in{f' and is using {mb} MB now' if mb else ''}. Turning it off "
                                 "only stops the automatic start; it still works when you open it. It's the same "
                                 "switch as Task Manager > Startup apps."] + notes),
            evidence={"kind": "app", "location": a["where"], "command": a["command"], "exe": exe,
                      "publisher": publisher, "ram_mb": mb, "running": bool(mb), "approval_key": a["akey"],
                      "approval_value": a["value"], "hardware": hardware or None, "protected": protected,
                      "duplicate_of": dupes or None},
            key=f"app:{ident}",
        ))

    # ---- leftover on/off switches for entries that no longer exist ----------------------
    orphans = [(k, v) for k, vals in approvals.items() for v in vals if v and (k, v) not in used_flags]
    if orphans:
        out.append(Finding(
            scanner="startup", category="startup-broken", title=f"{len(orphans)} leftover startup on/off switches",
            path=None, size=0, risk="low",
            suggestion="Task Manager remembers whether each startup entry is on or off. These switches belong to "
                       "entries that no longer exist (uninstalled or changed apps). They do nothing; cleaning them "
                       "up just tidies Task Manager's list. Each is backed up. If one of these apps is reinstalled "
                       "and adds itself back, it starts switched on.",
            evidence={"kind": "approval-orphans", "switches": [f"{k.split(chr(92))[0]}\\...\\{k.rsplit(chr(92), 1)[-1]}: {v}"
                                                                for k, v in orphans],
                      "items": [[k, v] for k, v in orphans]},
            key="approval-orphans",
        ))

    # ---- scheduled tasks at sign-in / boot ---------------------------------------------
    for t in data.get("tasks") or []:
        path = t.get("p") or ""
        exe = _task_exe(t)
        leaf = path.rsplit("\\", 1)[-1]
        if exe and not os.path.exists(exe) and os.path.isabs(exe):
            out.append(Finding(
                scanner="startup", category="startup-broken", title=f"Task: {leaf}", path=None, size=0, risk="low",
                suggestion="Scheduled task that runs at sign-in/boot, but its program is gone. Cleaning it up deletes "
                           "the task (an XML backup is kept).",
                evidence={"kind": "task", "task": path, "exe": exe, "author": t.get("au")},
                key=f"task:{path}",
            ))
            continue
        company = file_company(exe)
        publisher = t.get("au") if (not company or _is_microsoft(company)) and t.get("au") else company
        notes, hardware, protected = _notes(leaf, publisher)
        out.append(Finding(
            scanner="startup", category="startup", title=leaf, path=None, size=0,
            risk="high" if (hardware or protected) else "low",
            suggestion=" ".join([f"Scheduled task that runs at {t.get('tr', '').lower().replace(',', ' and ')}. "
                                 "Disabling it stops that; the program itself is untouched."] + notes),
            evidence={"kind": "task", "location": "Task Scheduler", "task": path, "exe": exe,
                      "command": f"{exe or ''} {t.get('a') or ''}".strip(), "publisher": publisher,
                      "ram_mb": round(ram.get(norm_key(exe), 0) / 2**20) if exe else 0,
                      "hardware": hardware or None, "protected": protected},
            key=f"task:{path}",
        ))

    # ---- automatic services ------------------------------------------------------------
    for s in data.get("services") or []:
        img = _image_path(s.get("PathName"))
        if not img or _windows_component(img):
            continue
        company = file_company(img)
        if _is_microsoft(company):
            continue
        name, disp = s.get("Name") or "", s.get("DisplayName") or s.get("Name") or ""
        if not os.path.exists(img):
            out.append(Finding(
                scanner="startup", category="startup-broken", title=f"Service: {disp} ({name})", path=None, size=0,
                risk="medium",
                suggestion="Set to start automatically, but its program is gone, so it fails at every boot. "
                           "Remove it from an elevated terminal.",
                command=f'sc.exe delete "{name}"',
                evidence={"kind": "service", "service": name, "image": img},
                key=f"service:{name}",
            ))
            continue
        notes, hardware, protected = _notes(f"{disp} {name}", company)
        running = (s.get("State") or "").lower() == "running"
        mb = round(pid_ram.get(s.get("ProcessId"), 0) / 2**20) if running else 0
        out.append(Finding(
            scanner="startup", category="startup", title=disp, path=None, size=0,
            risk="high" if (hardware or protected) else "medium",
            suggestion=" ".join([f"Background service set to start with Windows{f', using {mb} MB now' if mb else ''}. "
                                 "Switching it to Manual means it only starts when something asks for it. Some apps "
                                 "need theirs running all the time (VPNs, sync tools), so check the app still "
                                 "works afterwards."] + notes),
            evidence={"kind": "service", "location": "Service", "service": name, "exe": img, "publisher": company,
                      "ram_mb": mb, "running": running, "hardware": hardware or None, "protected": protected},
            key=f"service:{name}",
        ))
    return out


def _broken(a: dict, ident: str) -> Finding:
    if a.get("link"):
        return Finding(
            scanner="startup", category="startup-broken", title=f"Shortcut: {a['name']}", path=a["link"], size=0,
            risk="low", suggestion="Startup-folder shortcut whose program is gone. Cleaning it up quarantines the "
                                   "shortcut (restorable).",
            evidence={"kind": "link", "location": a["where"], "target": a.get("target")},
            key=f"app:{ident}")
    return Finding(
        scanner="startup", category="startup-broken", title=f"Startup entry: {a['name']}", path=None, size=0,
        risk="low", suggestion="Starts at sign-in, but its program is gone (or the entry is empty), so it does "
                               "nothing. Cleaning it up deletes the entry; the old value is kept for restore.",
        evidence={"kind": "run", "location": a["where"], "key": a["key"], "value": a["value"],
                  "command": a["command"]},
        key=f"app:{ident}")
