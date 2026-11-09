"""Which programs are using the CPU and GPU right now: the "who caused that" half of a fan ramp.

Sampled in-process (psutil, and Windows' own GPU Engine counters through PDH) rather than by starting
PowerShell, because a sampler that spawns a process every few seconds causes the CPU spikes it's
trying to explain.
"""
from __future__ import annotations

import re
from collections import defaultdict

import psutil

TOP = 5
_PID = re.compile(r"pid_(\d+)_.*engtype_(\w+)")


class _GpuCounters:
    def __init__(self):
        import win32pdh
        self.pdh = win32pdh
        self.q = win32pdh.OpenQuery()
        self.c = win32pdh.AddEnglishCounter(self.q, r"\GPU Engine(*)\Utilization Percentage")
        win32pdh.CollectQueryData(self.q)

    def by_pid(self) -> dict[int, float]:
        self.pdh.CollectQueryData(self.q)
        items = self.pdh.GetFormattedCounterArray(self.c, self.pdh.PDH_FMT_DOUBLE)
        per = defaultdict(lambda: defaultdict(float))       # pid -> engine type -> %
        for inst, v in items.items():
            m = _PID.search(inst)
            if m and v > 0:
                per[int(m.group(1))][m.group(2)] += v
        return {pid: max(e.values()) for pid, e in per.items()}  # Task Manager shows the busiest engine


class Sampler:
    def __init__(self):
        self.ncpu = psutil.cpu_count() or 1
        self.names: dict[int, str] = {}
        for p in psutil.process_iter(["name"]):
            try:
                p.cpu_percent(None)            # first call primes the counter
            except psutil.Error:
                pass
        try:
            self.gpu = _GpuCounters()
        except Exception:
            self.gpu = None

    def sample(self) -> dict:
        cpu = []
        for p in psutil.process_iter(["name"]):
            if p.pid == 0:
                continue
            try:
                pct = p.cpu_percent(None) / self.ncpu
            except psutil.Error:
                continue
            name = p.info.get("name") or str(p.pid)
            self.names[p.pid] = name
            if pct >= 0.5:
                cpu.append([name, round(pct, 1)])
        gpu = []
        if self.gpu:
            try:
                for pid, pct in self.gpu.by_pid().items():
                    if pct >= 1 and pid:
                        gpu.append([self.names.get(pid) or _name(pid), round(min(pct, 100), 1)])
            except Exception:
                pass
        return {"cpu": _merge(cpu), "gpu": _merge(gpu)}


def _name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return str(pid)


def _merge(rows: list) -> list:
    """Sum processes with the same name (Chrome, Opera...), busiest first."""
    tot = defaultdict(float)
    for n, v in rows:
        tot[n] += v
    return [[n, round(v, 1)] for n, v in sorted(tot.items(), key=lambda x: -x[1])[:TOP]]
