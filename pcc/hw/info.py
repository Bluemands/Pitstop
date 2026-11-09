"""Static inventory (what HWiNFO's summary shows) and the fan-header table."""
from __future__ import annotations

from ..util import powershell_json
from . import fancontrol
from .lhm import all_hardware

# Processes of software that controls fans or reads the same sensors. PC Cleaner reads alongside them
# and never competes: FanControl's curves are read from its config, everything else is just named.
FAN_SOFTWARE = {
    "FanControl": "FanControl", "iCUE": "Corsair iCUE", "NZXT CAM": "NZXT CAM", "ArmouryCrate": "ASUS Armoury Crate",
    "GCC": "GIGABYTE Control Center", "MSI.CentralServer": "MSI Center", "ArgusMonitor": "Argus Monitor",
    "SignalRgb": "SignalRGB", "L-Connect 3": "Lian Li L-Connect", "HWiNFO64": "HWiNFO", "SpeedFan": "SpeedFan",
    "LGHUB": "Logitech G HUB", "AquaSuite": "Aquasuite",
}

_INVENTORY_PS = r"""
$d = { param($x) if ($x) { $x.ToString('yyyy-MM-dd') } }
$b = Get-CimInstance Win32_BaseBoard; $bi = Get-CimInstance Win32_BIOS; $os = Get-CimInstance Win32_OperatingSystem
$disks = @(Get-PhysicalDisk | ForEach-Object {
  $r = $null; try { $r = $_ | Get-StorageReliabilityCounter -ErrorAction Stop } catch {}
  [ordered]@{ name = $_.FriendlyName; media = [string]$_.MediaType; bus = [string]$_.BusType
              gb = [math]::Round($_.Size / 1GB); health = [string]$_.HealthStatus
              wear_pct = $r.Wear; power_on_hours = $r.PowerOnHours; read_errors = $r.ReadErrorsUncorrected } })
[pscustomobject][ordered]@{
  board = [ordered]@{ maker = $b.Manufacturer; model = $b.Product; bios = $bi.SMBIOSBIOSVersion; bios_date = (& $d $bi.ReleaseDate) }
  os = [ordered]@{ name = $os.Caption; version = $os.Version; build = $os.BuildNumber }
  cpu = @(Get-CimInstance Win32_Processor | ForEach-Object { [ordered]@{ name = $_.Name.Trim(); cores = $_.NumberOfCores
          threads = $_.NumberOfLogicalProcessors } })
  ram = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [ordered]@{ slot = $_.DeviceLocator; maker = $_.Manufacturer
          part = "$($_.PartNumber)".Trim(); gb = [math]::Round($_.Capacity / 1GB); mts = $_.ConfiguredClockSpeed; rated_mts = $_.Speed } })
  gpu = @(Get-CimInstance Win32_VideoController | Where-Object { $_.PNPDeviceID -like 'PCI*' } | ForEach-Object {
          [ordered]@{ name = $_.Name; driver = $_.DriverVersion; driver_date = (& $d $_.DriverDate) } })
  disks = $disks
  processes = @(Get-Process | Select-Object -ExpandProperty Name -Unique)
}
"""


def inventory() -> dict:
    data = (powershell_json(_INVENTORY_PS, timeout=90) or [{}])[0]
    procs = set(data.pop("processes", None) or [])
    data["fan_software"] = [label for proc, label in FAN_SOFTWARE.items() if proc in procs]
    return data


def fan_headers(snap: dict, fc: dict | None = None, names: dict | None = None) -> list[dict]:
    """One row per motherboard fan header (and per GPU fan): speed, control %, what controls it."""
    fc = fc if fc is not None else fancontrol.read()
    names = names or {}
    rows = []
    for h in all_hardware(snap):
        sensors = h["sensors"]
        fans = [s for s in sensors if s["type"] == "Fan"]
        controls = {s["id"].rsplit("/", 1)[-1]: s for s in sensors if s["type"] == "Control"}
        is_board = h["id"].startswith("/lpc/")
        for f in fans:
            idx = f["id"].rsplit("/", 1)[-1]
            ctl = controls.get(idx) if is_board else next(
                (c for c in controls.values() if c["name"] == f["name"]), None)
            fcc = fc["controls"].get(fancontrol.norm_id(ctl["id"])) if ctl else None
            rpm, pct = f["value"], ctl["value"] if ctl else None
            if fcc and fcc["enabled"]:
                d = fcc.get("curve_detail") or {}
                who = f"FanControl: curve “{fcc['curve']}”" + (f" on {d['source_name']}" if d.get("source_name") else "")
            elif not is_board:
                who = "Graphics card (its own fan curve)"
            elif not rpm and pct is None:
                who = "Nothing connected"
            else:
                who = "Motherboard (BIOS fan curve)"
            hint = None
            if is_board and rpm and rpm > 4000:
                hint = "Very fast for a fan: probably a pump's speed signal"
            rows.append({"chip": h["name"], "header": f["name"], "fan_id": f["id"],
                         "control_id": ctl["id"] if ctl else None,
                         "label": names.get(f["id"]), "rpm": rpm, "percent": pct,
                         "controlled_by": who, "fancontrol": fcc, "hint": hint,
                         "kind": "board" if is_board else "gpu"})
    return rows


UNITS = {"Temperature": "°C", "Fan": " RPM", "Control": "%", "Load": "%", "Power": " W", "Clock": " MHz",
         "Voltage": " V", "Data": " GB", "SmallData": " MB", "Level": "%", "Flow": " L/h", "Throughput": " B/s"}


def fmt_value(s: dict, key: str = "value") -> str:
    v = s.get(key)
    if v is None:
        return "—"
    return (f"{v:.0f}" if s["type"] in ("Fan", "Clock", "Throughput") else f"{v:g}") + UNITS.get(s["type"], "")


def _is_key(h: dict, s: dict) -> bool:
    """The readings worth showing by default: temperatures (not every core), fans, package power, total load."""
    n = s["name"]
    if s["hidden"] or s["value"] is None:
        return False
    if s["type"] == "Temperature":
        return not (h["type"] == "Cpu" and ("Core #" in n and n not in ("Core Max", "Core Average")))
    if s["type"] in ("Fan", "Control"):
        return True
    if s["type"] == "Power":
        return "Package" in n
    if s["type"] == "Load":
        return n in ("CPU Total", "GPU Core", "Memory", "Total Activity")
    return False


def report(snap: dict, everything: bool = False, names: dict | None = None, fc: dict | None = None) -> dict:
    """A compact view of a snapshot for Claude / the CLI: key readings per part, plus the fan headers."""
    if "error" in snap:
        return snap
    names = names or {}
    parts = []
    for h in all_hardware(snap):
        if not snap.get("admin") and h["type"] == "Cpu":   # without admin, CPU power/clocks read as 0
            h = h | {"sensors": [s for s in h["sensors"] if s["type"] == "Load"]}
        rows = [{k: s.get(k) for k in ("id", "type", "name", "value", "min", "max", "warn", "crit")}
                | ({"label": names[s["id"]]} if s["id"] in names else {})
                for s in h["sensors"]
                if (everything and not s["hidden"] and s["value"] is not None) or _is_key(h, s)]
        if rows:
            parts.append({"name": h["name"], "type": h["type"], "sensors": rows}
                         | ({"tjmax": h["tjmax"]} if "tjmax" in h else {}))
    hot = [f"{p['name']}: {s.get('label') or s['name']} {s['value']}°C (warn {s['warn']})"
           for p in parts for s in p["sensors"] if s.get("warn") and s["value"] is not None and s["value"] >= s["warn"]]
    out = {"via": snap.get("via"), "admin": snap.get("admin"), "source": snap.get("source"), "parts": parts,
           "fan_headers": [{k: v for k, v in r.items() if k != "fancontrol"} for r in fan_headers(snap, fc, names)],
           "running_hot": hot}
    if not snap.get("admin"):
        out["note"] = ("Read without admin: CPU temperatures, the motherboard fan chip and RAM temperatures are "
                       "missing. Start the admin reader (`pcc hw monitor --background`, `hw_now(elevate=True)`, or Tune › Sensors › Read all sensors).")
    return out
