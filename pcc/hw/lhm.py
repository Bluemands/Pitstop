"""Sensor readings through LibreHardwareMonitorLib, loaded with pythonnet (.NET Framework).

Read-only: nothing here calls Control.SetSoftware/SetDefault. Groups that talk to devices other
software owns over USB (Controller: AIO coolers and fan hubs; Psu: Corsair/NZXT PSUs) stay
disabled, because two programs driving the same USB device can garble its commands.

Without admin, LHM reads GPUs, drives, CPU load/power and RAM use. With admin (through the signed
PawnIO driver) it adds CPU temperatures and clocks, the motherboard's fan/temperature chip and RAM
temperatures. Never ship or load the old WinRing0 driver: Defender flags it as vulnerable.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from ..elevate import is_admin
from ..util import LOCAL, PF, PF86

DLL = "LibreHardwareMonitorLib.dll"
ROOT = Path(__file__).resolve().parents[2]


class SensorError(Exception):
    pass


def find_lhm_dir() -> Path | None:
    """Where LibreHardwareMonitorLib.dll (and its dependencies) live. $PCC_LHM_DIR wins, then the copy
    FanControl ships (the library it reads sensors with: same version, and it finds NVMe drives), then the
    pinned NuGet copy `hw setup` puts in vendor/lhm. That one reads everything except NVMe drives so far:
    the NuGet package pulls DiskInfoToolkit 1.1.2, FanControl ships 2.1.0."""
    cands = [os.environ.get("PCC_LHM_DIR"), PF86 / "FanControl", PF / "FanControl",
             LOCAL / "Programs" / "FanControl", ROOT / "vendor" / "lhm"]
    for c in cands:
        if c and (Path(c) / DLL).is_file():
            return Path(c)
    return None


# Sensors that are fixed thresholds or derived values rather than readings: kept in the data
# (limits() uses them), hidden from the live view and the log.
_HIDDEN = re.compile(r"limit|resolution|warning temperature|critical temperature|distance to tjmax", re.I)


class Reader:
    """One open LibreHardwareMonitor Computer. Thread-safe; open once, read() as often as needed."""

    def __init__(self):
        d = find_lhm_dir()
        if not d:
            raise SensorError("LibreHardwareMonitorLib.dll not found. Install FanControl, put LHM in "
                              "vendor\\lhm, or set PCC_LHM_DIR.")
        try:
            import sys
            from pythonnet import load
            try:
                load("netfx")
            except RuntimeError:
                pass  # already loaded in this process
            import clr  # noqa: F401  (pythonnet)
            if str(d) not in sys.path:
                sys.path.append(str(d))
            clr.AddReference("LibreHardwareMonitorLib")
            from LibreHardwareMonitor.Hardware import Computer
        except Exception as e:  # pythonnet missing, .NET Framework missing, bad DLL
            raise SensorError(f"Could not load LibreHardwareMonitor from {d}: {e}") from e
        self.dir = d
        self.version = _file_version(d / DLL)
        self.admin = is_admin()
        self._lock = threading.Lock()
        c = Computer()
        c.IsCpuEnabled = c.IsGpuEnabled = c.IsMotherboardEnabled = True
        c.IsMemoryEnabled = c.IsStorageEnabled = True
        c.IsControllerEnabled = c.IsPsuEnabled = False   # USB devices other software owns: never
        c.IsNetworkEnabled = c.IsBatteryEnabled = False
        c.Open()
        self._c = c

    def read(self) -> dict:
        with self._lock:
            hw = [_hardware(h) for h in self._c.Hardware]
        snap = {"ts": time.time(), "admin": self.admin,
                "source": f"LibreHardwareMonitor {self.version}", "hardware": hw}
        add_limits(snap)
        return snap

    def close(self) -> None:
        with self._lock:
            try:
                self._c.Close()
            except Exception:
                pass


def _num(v):
    return None if v is None else round(float(v), 2)


def _hardware(h, parent: str | None = None) -> dict:
    h.Update()
    out = {"id": str(h.Identifier), "type": str(h.HardwareType), "name": str(h.Name),
           "parent": parent, "sensors": [], "sub": []}
    for sub in h.SubHardware:
        out["sub"].append(_hardware(sub, out["id"]))
    for s in h.Sensors:
        name = str(s.Name)
        out["sensors"].append({"id": str(s.Identifier), "type": str(s.SensorType), "name": name,
                               "value": _num(s.Value), "min": _num(s.Min), "max": _num(s.Max),
                               "hidden": bool(_HIDDEN.search(name))})
    return out


def _file_version(p: Path) -> str:
    try:
        import win32api
        info = win32api.GetFileVersionInfo(str(p), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}"
    except Exception:
        return "?"


def all_hardware(snap: dict):
    """Every hardware node, sub-hardware included (the motherboard's fan chip is a sub-node)."""
    stack = list(snap.get("hardware", []))
    while stack:
        h = stack.pop(0)
        yield h
        stack[0:0] = h.get("sub", [])


def all_sensors(snap: dict):
    for h in all_hardware(snap):
        for s in h["sensors"]:
            yield h, s


# ---- limits -------------------------------------------------------------------------------
# warn = getting hot, worth a look; crit = at or near where the part protects itself. Derived from
# the part where it says (CPU TjMax, NVMe warning/critical); otherwise conservative rules of thumb.

GPU_LIMITS = {  # sensor name -> (warn, crit)
    "GPU Core": (83, 90), "GPU Hot Spot": (95, 105), "GPU Memory Junction": (100, 106),
    "GPU Memory": (95, 105),
}


def add_limits(snap: dict) -> None:
    for h in all_hardware(snap):
        sensors = h["sensors"]
        temps = [s for s in sensors if s["type"] == "Temperature"]
        by_name = {s["name"]: s for s in temps}
        if h["type"] == "Cpu":
            tj = [s["value"] + d["value"] for s in temps if not s["hidden"]
                  for d in [by_name.get(f"{s['name']} Distance to TjMax")] if d and s["value"] is not None
                  and d["value"] is not None]
            tjmax = round(max(tj)) if tj else 100
            for s in temps:
                if not s["hidden"]:
                    s["warn"], s["crit"] = tjmax - 15, tjmax - 5
            h["tjmax"] = tjmax
        elif h["type"].startswith("Gpu"):
            for s in temps:
                w = GPU_LIMITS.get(s["name"])
                if w:
                    s["warn"], s["crit"] = w
        elif h["type"] == "Storage":
            warn = (by_name.get("Warning Temperature") or {}).get("value") or 70
            crit = (by_name.get("Critical Temperature") or {}).get("value") or 80
            for s in temps:
                if not s["hidden"]:
                    s["warn"], s["crit"] = min(warn, 70), crit
        elif h["type"] == "Memory":
            crit = next((s["value"] for s in temps if "Critical High" in s["name"] and s["value"]), 85)
            for s in temps:
                if not s["hidden"]:
                    s["warn"], s["crit"] = 70, crit
