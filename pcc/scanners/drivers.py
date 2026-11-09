"""Driver health: updates Windows Update offers, devices without a working driver, and old copies
of driver packages left in the DriverStore.

Read-only. PC Cleaner never installs drivers: the user ticks driver updates in Settings >
Windows Update > Advanced options > Optional updates (actions.open_settings), ideally after
actions.restore_point. Nothing here comes from third-party "driver updater" databases, and
drivers aren't flagged as "old" by date: vendors often never change the date on current drivers.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..store import Finding
from ..util import WINDIR, dir_size, powershell_json, run

FILE_REPO = WINDIR / "System32" / "DriverStore" / "FileRepository"

_WU = r"""
$s = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
$r = $s.Search("IsInstalled=0 and Type='Driver'")
[pscustomobject]@{ ok = $true; updates = @($r.Updates | ForEach-Object {
  [pscustomobject]@{ title = $_.Title; model = $_.DriverModel; cls = $_.DriverClass; mfr = $_.DriverManufacturer;
                     date = $_.DriverVerDate.ToString('yyyy-MM-dd'); hwid = $_.DriverHardwareID;
                     id = $_.Identity.UpdateID; mb = [math]::Round($_.MaxDownloadSize / 1MB, 1) } }) }
"""
_DEVICES = r"""
$drv = Get-CimInstance Win32_PnPSignedDriver | Select-Object DeviceName, InfName, DriverVersion, DriverProviderName,
  @{n='hwids';e={ @($_.HardWareID) -join '|' }}, @{n='date';e={ if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } }}
$bad = Get-CimInstance Win32_PnPEntity | Where-Object { $_.ConfigManagerErrorCode -notin 0, 22 -and $_.Present } |
  Select-Object Name, ConfigManagerErrorCode, PNPDeviceID, PNPClass, @{n='hwid';e={ @($_.HardwareID)[0] }}
$bb = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product
[pscustomobject]@{ drivers = @($drv); problems = @($bad); board = $bb }
"""

# Device Manager problem codes worth explaining. 22 (disabled by you) is skipped entirely.
_CODES = {
    1: "isn't configured correctly", 3: "driver may be corrupted, or memory is low", 10: "can't start",
    12: "can't find enough free resources", 14: "needs a restart to work", 18: "needs its driver reinstalled",
    19: "has a damaged registry entry", 24: "isn't present, or isn't working properly",
    28: "has no driver installed", 31: "isn't working properly", 32: "driver service is disabled",
    37: "driver failed to initialise", 39: "driver is missing or corrupted", 43: "was stopped after reporting problems",
    45: "isn't connected", 52: "driver isn't signed properly",
}
_CLASS_LABEL = {"OtherHardware": "Other hardware", "Networking": "Network", "Video": "Display",
                 "AudioProcessingObject": "Audio", "HIDClass": "Input", "SoftwareComponent": "Software component"}
_VENDORS = {"8086": "Intel", "10DE": "NVIDIA", "1022": "AMD", "1002": "AMD", "10EC": "Realtek", "1B21": "ASMedia",
            "14E4": "Broadcom", "168C": "Qualcomm Atheros", "1969": "Qualcomm Atheros", "8087": "Intel"}


def _ver(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def _vendor(hwid: str | None) -> str | None:
    m = re.search(r"(?:VEN|VID)_([0-9A-F]{4})", hwid or "", re.I)
    return _VENDORS.get(m.group(1).upper()) if m else None


def _driver_updates(devices: list[dict]) -> list[Finding]:
    data = (powershell_json(_WU, timeout=240) or [{}])[0]
    if not data.get("ok"):
        raise RuntimeError("Windows Update search failed (is the Windows Update service running?)")
    out = []
    for u in data.get("updates") or []:
        hwid = (u.get("hwid") or "").lower()
        dev = next((d for d in devices if hwid and any(h.lower().startswith(hwid) for h in (d.get("hwids") or "").split("|"))),
                   None) or next((d for d in devices if d.get("DeviceName") == u.get("model")), None) or {}
        offered = (re.search(r"\(([\d.]+)\)\s*$", u.get("title") or "") or [None, None])[1]
        cur, cur_date = dev.get("DriverVersion"), dev.get("date")
        raw_cls = u.get("cls") or ""
        risky = raw_cls.lower() in ("video", "display", "networking", "net")
        cls = _CLASS_LABEL.get(raw_cls, raw_cls)
        text = (f"Windows Update offers {offered or 'a newer driver'} ({u.get('date')})"
                f"{f'; you have {cur} ({cur_date})' if cur else ''}. Install it yourself from Settings > Windows "
                "Update > Advanced options > Optional updates (PC Cleaner never installs drivers). If it causes "
                "trouble, Device Manager > the device > Driver > Roll Back Driver undoes it.")
        if risky:
            text += " Display and network drivers are the ones most worth a restore point first."
        out.append(Finding(
            scanner="drivers", category="driver-update", title=u.get("model") or u.get("title"), path=None, size=0,
            risk="medium" if risky else "low", suggestion=text,
            evidence={"installed": cur, "installed_date": cur_date, "available": offered, "available_date": u.get("date"),
                      "class": cls, "manufacturer": u.get("mfr"), "update": u.get("title"),
                      "download_mb": u.get("mb"), "update_id": u.get("id")},
            key=f"wu:{u.get('id')}",
        ))
    return out


def _problems(problems: list[dict], board: dict | None) -> list[Finding]:
    out = []
    maker = re.sub(r"[\s,]*\b(Technology|Co|Ltd|Inc|Corporation|Computer|International)\b\.?", "",
                   (board or {}).get("Manufacturer") or "").strip(" ,.")
    board_name = " ".join(x for x in (maker, (board or {}).get("Product")) if x)
    for p in problems:
        code = p.get("ConfigManagerErrorCode")
        what = _CODES.get(code, f"reports problem code {code}")
        vendor = _vendor(p.get("hwid") or p.get("PNPDeviceID"))
        name = p.get("Name") or "Unknown device"
        text = f"Device Manager says this device {what} (code {code})."
        if code == 28 and vendor == "Intel" and re.search(r"SM ?Bus|PCI Device|PCI Simple|Serial IO|Management Engine", name, re.I):
            text += (" Unnamed Intel devices like this usually mean the motherboard's chipset driver was never "
                     f"installed. Get \"Intel Chipset Device Software\" from {board_name or 'your motherboard'}'s "
                     "support page (or Intel's site), or check Optional updates first.")
        elif code == 28:
            text += (f" Check Optional updates first, then {vendor + ' or ' if vendor else ''}"
                     f"{board_name or 'the device maker'}'s support page.")
        else:
            text += " Device Manager > the device > Properties shows details; reinstalling its driver often fixes it."
        out.append(Finding(
            scanner="drivers", category="device-problem", title=name, path=None, size=0, risk="low",
            suggestion=text,
            evidence={"problem": f"{what} (code {code})", "vendor": vendor, "device_class": p.get("PNPClass"),
                      "hardware_id": p.get("hwid"), "device_id": p.get("PNPDeviceID"), "motherboard": board_name or None},
            key=f"dev:{p.get('PNPDeviceID')}",
        ))
    return out


def parse_enum_drivers(text: str) -> list[dict]:
    """`pnputil /enum-drivers` blocks -> dicts with inf, original, provider, cls, date, version."""
    out = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        f = dict((k.strip(), v.strip()) for k, v in re.findall(r"^\s*([^:\r\n]+):\s+(.*)$", block, re.M))
        if not f.get("Published Name", "").lower().startswith("oem"):
            continue
        dv = f.get("Driver Version", "").split()
        out.append({"inf": f["Published Name"].lower(), "original": f.get("Original Name", "").lower(),
                    "provider": f.get("Provider Name"), "cls": f.get("Class Name"),
                    "date": dv[0] if len(dv) > 1 else None, "version": dv[-1] if dv else None})
    return out


def _repo_folder(pkg: dict) -> Path | None:
    """The DriverStore folder holding this package: the one whose .inf matches C:\\Windows\\INF\\oemNN.inf."""
    try:
        published = (WINDIR / "INF" / pkg["inf"]).read_bytes()
    except OSError:
        return None
    try:
        candidates = [e for e in os.scandir(FILE_REPO) if e.name.lower().startswith(pkg["original"] + "_")]
    except OSError:
        return None
    for e in candidates:
        try:
            if (Path(e.path) / pkg["original"]).read_bytes() == published:
                return Path(e.path)
        except OSError:
            continue
    return None


def _superseded(devices: list[dict]) -> list[Finding]:
    in_use = {(d.get("InfName") or "").lower() for d in devices}
    pkgs = [p for p in parse_enum_drivers(run(["pnputil", "/enum-drivers"], timeout=120))
            if not (p["provider"] or "").lower().startswith("microsoft")]
    groups: dict[str, list[dict]] = {}
    for p in pkgs:
        groups.setdefault(p["original"], []).append(p)
    out = []
    for original, group in groups.items():
        if len(group) < 2:
            continue
        newest = max(group, key=lambda p: (_ver(p["version"]), p["date"] or ""))
        for p in group:
            if p is newest or p["inf"] in in_use or _ver(p["version"]) >= _ver(newest["version"]):
                continue
            folder = _repo_folder(p)
            size = dir_size(folder, budget_s=10)[0] if folder else 0
            out.append(Finding(
                scanner="drivers", category="driver-superseded",
                title=f"{p['provider']} {p['cls'] or ''} driver {p['version']} ({original})".replace("  ", " "),
                path=str(folder) if folder else None, size=size, risk="low",
                suggestion=(f"An older copy of {original}: version {newest['version']} ({newest['inf']}) has replaced "
                            "it and no device uses this one. Removing it exports it to quarantine first, so it's "
                            "restorable until you purge. Windows refuses to remove it if any device still needs it."),
                evidence={"inf": p["inf"], "original": original, "provider": p["provider"], "class": p["cls"],
                          "version": p["version"], "date": p["date"], "newer": f"{newest['version']} ({newest['inf']})"},
                key=f"inf:{p['inf']}",
            ))
    return out


def scan() -> list[Finding]:
    data = (powershell_json(_DEVICES, timeout=180) or [{}])[0]
    devices = [d for d in data.get("drivers") or [] if isinstance(d, dict)]
    return (_driver_updates(devices) + _problems([p for p in data.get("problems") or [] if isinstance(p, dict)],
                                                 data.get("board")) + _superseded(devices))
