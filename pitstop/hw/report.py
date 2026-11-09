"""Turn a sensor log into something Claude can read: per-sensor statistics, every fan ramp with what
preceded it (which temperatures rose, power, load, top processes), the user's marks, limit breaches,
and the busiest programs. Raw samples are far too big to read; `query` pulls a downsampled slice.
"""
from __future__ import annotations

import csv
import json
import time
from collections import defaultdict

from . import logdb

RAMP_WINDOW_S = 15        # a ramp is a rise within this many seconds...
RAMP_MIN_RPM = 150        # ...of at least this many RPM...
RAMP_MIN_FRAC = 0.20      # ...and this fraction of where it started
MERGE_S = 10              # ramps on different fans this close together are one event
MAX_RAMPS = 25


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _pct(sorted_vals: list[float], p: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(p * (len(sorted_vals) - 1) + 0.5))]


class _Log:
    def __init__(self, d: dict, names: dict):
        self.d, self.ts, self.rows, self.meta = d, d["ts"], d["rows"], d["meta"]
        self.idx = {c: i for i, c in enumerate(d["cols"])}
        self.names = names

    def label(self, c: str) -> str:
        m = self.meta[c]
        return self.names.get(c) or m["name"]

    def series(self, c: str) -> list[float | None]:
        i = self.idx[c]
        return [r[i] if i < len(r) else None for r in self.rows]

    def at(self, c: str, k: int):
        r = self.rows[k]
        i = self.idx[c]
        return r[i] if i < len(r) else None

    def k_at(self, ts: float) -> int:
        """Index of the sample nearest ts."""
        import bisect
        k = bisect.bisect_left(self.ts, ts)
        if k >= len(self.ts):
            return len(self.ts) - 1
        if k > 0 and ts - self.ts[k - 1] < self.ts[k] - ts:
            return k - 1
        return k

    def procs_at(self, ts: float) -> dict:
        best, gap = {}, 1e9
        for t, p in self.d["procs"]:
            if abs(t - ts) < gap:
                best, gap = p, abs(t - ts)
        return best if gap <= 10 else {}

    def cols(self, pred) -> list[str]:
        return [c for c in self.d["cols"] if pred(self.meta[c])]


def _key_temp(m: dict) -> bool:
    return m["type"] == "Temperature" and not (m["part_type"] == "Cpu" and "Core #" in m["name"])


def _stats(vals: list) -> dict | None:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    return {"min": round(v[0], 1), "avg": round(sum(v) / len(v), 1), "p95": round(_pct(v, .95), 1),
            "max": round(v[-1], 1)}


def _median5(s: list) -> list:
    """Rolling median over 5 readings: fan tach lines (pumps especially) sometimes misread for a single
    second, e.g. 4,350 -> 7,300 -> 4,350 RPM. A real ramp lasts longer than that."""
    out = []
    for k in range(len(s)):
        w = sorted(v for v in s[max(0, k - 2):k + 3] if v is not None)
        out.append(w[len(w) // 2] if w and s[k] is not None else None)
    return out


def _ramps(L: _Log, fans: list[str], temps: list[str]) -> list[dict]:
    found = []
    for c in fans:
        s = _median5(L.series(c))
        k = 0
        while k < len(s):
            v = s[k]
            if v is None:
                k += 1
                continue
            lo_k = None
            j = k - 1
            while j >= 0 and L.ts[k] - L.ts[j] <= RAMP_WINDOW_S:   # lowest point in the window before k
                if s[j] is not None and (lo_k is None or s[j] < s[lo_k]):
                    lo_k = j
                j -= 1
            if lo_k is None:
                k += 1
                continue
            base = s[lo_k]
            if v - base >= max(RAMP_MIN_RPM, RAMP_MIN_FRAC * max(base, 1)):
                peak_k, e = k, k
                settle = base + 0.25 * (v - base)
                while e + 1 < len(s) and (s[e + 1] is None or s[e + 1] > settle):   # until it settles back
                    e += 1
                    if s[e] is not None and s[e] > s[peak_k]:
                        peak_k = e
                found.append({"col": c, "start_k": lo_k, "peak_k": peak_k, "end_k": e,
                              "from": round(base), "to": round(s[peak_k])})
                k = e + 1
            else:
                k += 1
    found.sort(key=lambda r: L.ts[r["start_k"]])
    events: list[list[dict]] = []
    for r in found:                       # merge fans ramping together
        if events and L.ts[r["start_k"]] - L.ts[events[-1][0]["start_k"]] <= MERGE_S:
            events[-1].append(r)
        else:
            events.append([r])
    out = []
    for ev in events:
        biggest: dict = {}                # a fan that ramped twice in one event: keep its bigger rise
        for r in ev:
            if r["col"] not in biggest or r["to"] - r["from"] > biggest[r["col"]]["to"] - biggest[r["col"]]["from"]:
                biggest[r["col"]] = r
        ev = list(biggest.values())
        k0 = min(r["start_k"] for r in ev)
        k1 = max(r["end_k"] for r in ev)
        kp = max(r["peak_k"] for r in ev)
        before = L.k_at(L.ts[k0] - 30)
        rose = []
        for c in temps:
            a, b = L.at(c, before), L.at(c, kp)
            if a is not None and b is not None and b - a >= 3:
                rose.append((b - a, {"sensor": f"{L.meta[c]['part']}: {L.label(c)}", "from": round(a), "to": round(b)}))
        rose.sort(key=lambda x: -x[0])
        ctx = {}
        for c in L.cols(lambda m: (m["type"] == "Power" and "Package" in m["name"])
                        or (m["type"] == "Load" and m["name"] in ("CPU Total", "GPU Core"))):
            v = L.at(c, kp)
            if v is not None:
                ctx[f"{L.meta[c]['part']}: {L.meta[c]['name']} {L.meta[c]['type'].lower()}"] = round(v, 1)
        p = L.procs_at(L.ts[kp])
        out.append({"at": _clock(L.ts[k0]), "ts": L.ts[k0], "lasted_s": round(L.ts[k1] - L.ts[k0]),
                    "fans": [{"fan": L.label(r["col"]), "from_rpm": r["from"], "to_rpm": r["to"]} for r in ev],
                    "temps_rose_30s_before": [x for _, x in rose[:4]], "at_peak": ctx,
                    "top_cpu": p.get("cpu", [])[:3], "top_gpu": p.get("gpu", [])[:3],
                    "_size": max(r["to"] - r["from"] for r in ev)})
    return out


def _fan_setup(fc_json: str | None) -> list[dict]:
    if not fc_json:
        return []
    fc = json.loads(fc_json)
    out = []
    for cid, c in (fc.get("controls") or {}).items():
        if c.get("enabled"):
            d = c.get("curve_detail") or {}
            out.append({"control": cid, "curve": c.get("curve"), "kind": d.get("kind"),
                        "follows": d.get("source_name"),
                        "points": ", ".join(f"{t:g}°C→{p:g}%" for t, p in d.get("points") or []) or None,
                        "hysteresis": d.get("hysteresis") or None})
    return out


def report(session: int | None = None, names: dict | None = None) -> dict:
    d = logdb.load(session)
    if d is None:
        return {"error": "No logs yet. Start one with `pitstop hw log start --label \"...\"`."}
    s = d["session"]
    head = {"id": s["id"], "label": s["label"], "kind": s["kind"], "started": _when(s["started"]),
            "minutes": round(((s["ended"] or time.time()) - s["started"]) / 60, 1),
            "still_logging": s["ended"] is None and s["kind"] == "log", "stop_reason": s["stop_reason"],
            "all_sensors": bool(s["admin"]) if s["admin"] is not None else None}
    if d.get("empty") or not d["ts"]:
        return {"session": head, "note": "No samples yet: the background reader hasn't picked this log up."}
    L = _Log(d, names or {})
    head["samples"] = len(d["ts"])
    temps = L.cols(_key_temp)
    fans = L.cols(lambda m: m["type"] == "Fan")
    controls = {(m := L.meta[c])["part"] + "|" + c.rsplit("/", 1)[-1]: c for c in L.cols(lambda m: m["type"] == "Control")}

    t_rows, breaches = [], []
    for c in temps:
        st = _stats(L.series(c))
        if not st:
            continue
        m = L.meta[c]
        row = {"sensor": f"{m['part']}: {L.label(c)}", **st}
        if m.get("warn") is not None:
            row["warn"] = m["warn"]
            over = [k for k, v in enumerate(L.series(c)) if v is not None and v >= m["warn"]]
            if over:
                secs = len(over) * (s["interval"] or 1)
                row["seconds_over_warn"] = round(secs)
                breaches.append(f"{row['sensor']} was at or over {m['warn']}°C for {secs:.0f} s "
                                f"(first at {_clock(L.ts[over[0]])}, peak {st['max']}°C)")
        t_rows.append(row)

    f_rows = []
    for c in fans:
        st = _stats(L.series(c))
        if not st or st["max"] == 0:
            continue
        m = L.meta[c]
        row = {"fan": L.label(c), "part": m["part"], "rpm": st}
        ctl = controls.get(m["part"] + "|" + c.rsplit("/", 1)[-1])
        if ctl and (cs := _stats(L.series(ctl))):
            row["percent"] = cs
        f_rows.append(row)

    other = []
    for c in L.cols(lambda m: (m["type"] == "Power" and "Package" in m["name"])
                    or (m["type"] == "Load" and m["name"] in ("CPU Total", "GPU Core"))):
        if (st := _stats(L.series(c))):
            other.append({"sensor": f"{L.meta[c]['part']}: {L.meta[c]['name']}", "type": L.meta[c]["type"], **st})

    ramps = _ramps(L, fans, temps)
    biggest = sorted(ramps, key=lambda r: -r["_size"])[:MAX_RAMPS]
    shown = sorted(biggest, key=lambda r: r["ts"])
    for r in shown:
        r.pop("_size"); r.pop("ts")

    marks = []
    for e in d["events"]:
        k = L.k_at(e["ts"])
        k60 = L.k_at(e["ts"] - 60)
        snap = {L.label(c): round(v) for c in fans if (v := L.at(c, k)) is not None and v > 0}
        rose = [f"{L.label(c)} {round(a)}→{round(b)} RPM" for c in fans
                if (a := L.at(c, k60)) is not None and (b := L.at(c, k)) is not None and b - a >= RAMP_MIN_RPM]
        hot = {f"{L.meta[c]['part']}: {L.label(c)}": round(v, 1) for c in temps
               if (v := L.at(c, k)) is not None and (L.meta[c]["part_type"] in ("Cpu", "GpuNvidia", "GpuAmd")
                                                     or c in L.names)}
        p = L.procs_at(e["ts"])
        marks.append({"at": _clock(e["ts"]), "note": e["text"], "fans_rpm": snap, "fans_rose_last_60s": rose,
                      "temps": hot, "top_cpu": p.get("cpu", [])[:3], "top_gpu": p.get("gpu", [])[:3]})

    agg = {"cpu": defaultdict(float), "gpu": defaultdict(float)}
    for _, p in d["procs"]:
        for kind in agg:
            for name, v in p.get(kind, []):
                agg[kind][name] += v
    n = max(1, len(d["procs"]))
    busiest = {k: [[name, round(v / n, 1)] for name, v in sorted(a.items(), key=lambda x: -x[1])[:6]]
               for k, a in agg.items()}

    return {"session": head, "fan_setup_at_start": _fan_setup(s.get("fan_config")),
            "temperatures": t_rows, "fans": f_rows, "power_and_load": other,
            "fan_ramps": shown, "fan_ramps_total": len(ramps), "marks": marks,
            "busiest_programs_avg_percent": busiest, "limit_breaches": breaches,
            "notes": [] if s["admin"] else ["Logged without admin: no CPU temperatures or motherboard fans."]}


def query(session: int | None, match: list[str], start: str | None = None, end: str | None = None,
          points: int = 120, names: dict | None = None) -> dict:
    """Downsampled series (average and max per bucket) for sensors whose id, name, part or label contains
    any of `match`, optionally between two local clock times (HH:MM or HH:MM:SS)."""
    d = logdb.load(session)
    if not d or d.get("empty"):
        return {"error": "No such log, or it has no samples."}
    L = _Log(d, names or {})
    day = time.localtime(d["ts"][0])

    def clock(s):
        parts = [int(x) for x in s.split(":")] + [0]
        return time.mktime((day.tm_year, day.tm_mon, day.tm_mday, parts[0], parts[1], parts[2], 0, 0, -1))
    t0 = clock(start) if start else d["ts"][0]
    t1 = clock(end) if end else d["ts"][-1]
    if t1 < t0:
        t1 += 86400
    ks = [k for k, t in enumerate(d["ts"]) if t0 <= t <= t1]
    if not ks:
        return {"error": "No samples in that time range."}
    want = [c for c in d["cols"] if any(m.lower() in f"{c} {L.meta[c]['part']} {L.meta[c]['name']} {L.label(c)}".lower()
                                        for m in match)][:12]
    size = max(1, len(ks) // points)
    out = {}
    for c in want:
        vals = L.series(c)
        pts = []
        for b in range(0, len(ks), size):
            chunk = [vals[k] for k in ks[b:b + size] if vals[k] is not None]
            if chunk:
                pts.append(f"{_clock(d['ts'][ks[b]])} {sum(chunk) / len(chunk):.1f}/{max(chunk):.1f}")
        out[f"{L.meta[c]['part']}: {L.label(c)} ({L.meta[c]['type']})"] = pts
    return {"session": d["session"]["id"], "from": _clock(d["ts"][ks[0]]), "to": _clock(d["ts"][ks[-1]]),
            "bucket_s": size * (d["session"]["interval"] or 1), "format": "time avg/max", "series": out,
            "matched": len(want)}


def export_csv(session: int | None, path: str, names: dict | None = None) -> dict:
    d = logdb.load(session)
    if not d or d.get("empty"):
        return {"error": "No such log, or it has no samples."}
    L = _Log(d, names or {})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time"] + [f"{L.meta[c]['part']}: {L.label(c)} ({L.meta[c]['type']})" for c in d["cols"]])
        for t, r in zip(d["ts"], d["rows"]):
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))] + ["" if v is None else v for v in r])
    return {"exported": path, "rows": len(d["ts"]), "columns": len(d["cols"])}
