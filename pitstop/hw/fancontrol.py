"""Read FanControl's (Rem0o) active configuration: which fan header follows which curve, and which
sensor that curve follows. Read-only; FanControl stays in charge of the fans.

The format isn't documented, so everything here is defensive: unknown curve types come back as
kind "other" with their raw keys, and a missing or unreadable config just means "not installed".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..util import LOCAL, PF, PF86, run

INSTALL_DIRS = [PF86 / "FanControl", PF / "FanControl", LOCAL / "Programs" / "FanControl"]


def install_dir() -> Path | None:
    return next((d for d in INSTALL_DIRS if (d / "FanControl.exe").is_file()), None)


def norm_id(identifier: str) -> str:
    """FanControl drops LHM's chip index: '/lpc/it8689e/control/1' vs LHM 0.9.6's
    '/lpc/it8689e/0/control/1'. Compare in FanControl's form."""
    return re.sub(r"^(/lpc/[^/]+)/\d+/", r"\1/", identifier or "")


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8-sig"))


def config_path() -> Path | None:
    d = install_dir()
    if not d:
        return None
    folder, name = d / "Configurations", "userConfig.json"
    try:
        cache = _load(folder / "CACHE")
        name = cache.get("CurrentConfigFileName") or name
        folder = Path(cache.get("CustomConfigFolder") or folder)
    except (OSError, ValueError):
        pass
    p = folder / name
    return p if p.is_file() else None


def running() -> bool:
    out = run(["tasklist", "/FI", "IMAGENAME eq FanControl.exe", "/NH"], timeout=15)
    return "FanControl.exe" in out


def _describe_source(identifier: str | None, names: dict) -> str | None:
    if not identifier:
        return None
    if identifier in names:
        return names[identifier]
    i = identifier.lower()
    if "nvapi" in i or "gpu" in i or "adlx" in i:
        return "GPU temperature"
    if "cpu" in i:
        return "CPU temperature"
    if i.startswith("/lpc/"):
        return "Motherboard sensor"
    if "nvme" in i or "hdd" in i or "storage" in i:
        return "Drive temperature"
    return identifier


def _curve(c: dict, names: dict) -> dict:
    src = (c.get("SelectedTempSource") or {}).get("Identifier")
    out = {"name": c.get("Name"), "source": src, "source_name": _describe_source(src, names)}
    if c.get("Points"):
        pts = []
        for p in c["Points"]:
            try:
                t, v = (float(x) for x in str(p).split(","))
                pts.append([round(t, 1), round(v, 1)])
            except ValueError:
                pass
        out |= {"kind": "graph", "points": pts}
        hc = c.get("HysteresisConfig") or {}
        out["hysteresis"] = {k: hc.get(k) for k in ("HysteresisValueUp", "HysteresisValueDown",
                                                    "ResponseTimeUp", "ResponseTimeDown") if k in hc}
    elif "IdleTemperature" in c:
        out |= {"kind": "linear", "points": [[c.get("IdleTemperature"), c.get("MinFanSpeed")],
                                             [c.get("LoadTemperature"), c.get("MaxFanSpeed")]]}
    elif "SelectedFanCurves" in c:
        out |= {"kind": "mix", "function": c.get("SelectedMixFunction"),
                "curves": [x.get("Name") for x in c.get("SelectedFanCurves") or []]}
    elif "Percent" in c and not src:
        out |= {"kind": "flat", "percent": c.get("Percent")}
    else:
        out |= {"kind": "other", "keys": sorted(c)}
    return out


def read() -> dict:
    """{installed, running, config, controls: {norm_id: {...}}, curves: [...]}"""
    p = config_path()
    if not p:
        return {"installed": install_dir() is not None, "running": False, "controls": {}, "curves": []}
    try:
        fc = _load(p).get("FanControl", {})
    except (OSError, ValueError) as e:
        return {"installed": True, "running": running(), "config": str(p), "error": str(e),
                "controls": {}, "curves": []}
    names = {t.get("Identifier"): t.get("Name") for t in fc.get("TemperatureSensors") or []
             if t.get("Identifier") and t.get("Name")}
    curves = [_curve(c, names) for c in fc.get("FanCurves") or []]
    by_name = {c["name"]: c for c in curves}
    controls = {}
    for c in fc.get("Controls") or []:
        ident = c.get("Identifier")
        if not ident:
            continue
        curve = (c.get("SelectedFanCurve") or {}).get("Name")
        controls[norm_id(ident)] = {
            "identifier": ident, "name": c.get("Name"), "enabled": bool(c.get("Enable")),
            "curve": curve if c.get("Enable") else None,
            "curve_detail": by_name.get(curve) if c.get("Enable") else None,
            "minimum": c.get("MinimumPercent"), "start": c.get("SelectedStart"), "stop": c.get("SelectedStop"),
            "manual": c.get("ManualControl"), "manual_value": c.get("ManualControlValue"),
        }
    return {"installed": True, "running": running(), "config": str(p), "controls": controls, "curves": curves}
