"""Command line: `python -m pcc <command>`. Add --json for machine-readable output, and
--elevate to run the command as admin (one UAC prompt) from an unelevated terminal."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import actions, diskindex, queries, scanners
from .elevate import ElevationError, is_admin, pcc_elevated
from .util import human


def _print(data, as_json: bool) -> None:
    if as_json or not isinstance(data, list):
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
        return
    for row in data:
        size = str(row.get("size", "")).rjust(9)
        rest = "  ".join(f"{k}={v}" for k, v in row.items()
                         if k not in ("id", "size", "title", "path", "category") and v is not None)
        print(f"{row.get('id', ''):10}  {size}  [{row.get('category', '')}] {row.get('title') or row.get('path')}"
              + (f"\n{'':23}{rest}" if rest else ""))


def _hw(a, j: bool) -> None:
    from .hw import fancontrol, info, live
    from .hw.lhm import SensorError
    if a.hw_cmd == "monitor":
        if a.background:
            _print(live.start_monitor(), j)
            return
        try:
            _print(live.monitor(a.interval, a.idle_exit), j)
        except SensorError as e:
            _print({"error": str(e)}, True)
        return
    if a.hw_cmd == "name":
        _print(live.set_name(a.sensor_id, a.label), j)
        return
    if a.hw_cmd == "names":
        _print(live.names(), j)
        return
    if a.hw_cmd in ("log", "report", "query", "export"):
        from .hw import logdb, report
        if a.hw_cmd == "report":
            _print(report.report(a.session, live.names()), True)
        elif a.hw_cmd == "query":
            _print(report.query(a.session, a.match, a.start, a.end, a.points, live.names()), True)
        elif a.hw_cmd == "export":
            _print(report.export_csv(a.session, a.path, live.names()), j)
        elif a.log_cmd == "start":
            _print(live.start_log(a.label, a.max_hours, admin=not a.no_admin), j)
        elif a.log_cmd == "stop":
            _print(logdb.stop(), j)
        elif a.log_cmd == "mark":
            _print(logdb.mark(a.note), j)
        elif a.log_cmd == "save":
            _print(live.request_save(a.label, a.note), j)
        elif a.log_cmd == "list":
            rows = logdb.sessions()
            if j:
                _print(rows, True)
            for r in [] if j else rows:
                state = "logging now" if r["active"] else (r["stop_reason"] or "")
                print(f"#{r['id']:<4} {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['started']))}  "
                      f"{r['minutes']:>6} min  {r['marks']} marks  {r['label']}  [{r['kind']}{', ' + state if state else ''}]")
        elif a.log_cmd == "delete":
            _print(logdb.delete(a.session), j)
        return
    names, fc = live.names(), fancontrol.read()
    if a.hw_cmd == "info":
        snap = live.snapshot()
        r = info.inventory() | {"sensors_via": snap.get("via"), "sensors_admin": snap.get("admin"),
                                "sensor_source": snap.get("source") or snap.get("error")}
        if "error" not in snap:
            r["parts"] = [f"{h['type']}: {h['name']}" for h in info.all_hardware(snap)]
            r["fan_headers"] = [{k: v for k, v in x.items() if k != "fancontrol"} for x in info.fan_headers(snap, fc, names)]
        r["fancontrol"] = {k: fc.get(k) for k in ("installed", "running", "config", "error") if k in fc}
        _print(r, True)
        return
    r = info.report(live.snapshot(), everything=a.all, names=names, fc=fc)  # now
    if j or "error" in r:
        _print(r, True)
        return
    print(f"{r['source']} via {r['via']}{'' if r['admin'] else ' (no admin)'}")
    for p in r["parts"]:
        print(f"\n{p['name']}  [{p['type']}]")
        for s in p["sensors"]:
            lim = f"   warn {s['warn']:g}" if s.get("warn") else ""
            mm = f"  ({info.fmt_value(s, 'min')} – {info.fmt_value(s, 'max')})" if s.get("max") is not None else ""
            print(f"  {s['type']:<12} {(s.get('label') or s['name'])[:34]:<34} {info.fmt_value(s):>11}{mm}{lim}")
    if r["fan_headers"]:
        print("\nFan headers")
        for h in r["fan_headers"]:
            pct = "" if h["percent"] is None else f"{h['percent']:.0f}%"
            rpm = "" if h["rpm"] is None else f"{h['rpm']:.0f} RPM"
            name = h["label"] or h["header"]
            print(f"  {name[:22]:<22} {rpm:>9} {pct:>5}  {h['controlled_by']}"
                  + (f"  ({h['hint']})" if h["hint"] else ""))
    for x in r["running_hot"]:
        print(f"\n! {x}")
    if r.get("note"):
        print(f"\n{r['note']}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="pcc", description="PC Cleaner: find forgotten space, decide with Claude.")
    ap.add_argument("--json", action="store_true", help="JSON output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="run scanners (all by default)")
    s.add_argument("names", nargs="*", metavar="SCANNER", help=", ".join(scanners.SCANNERS))
    sub.add_parser("scanners", help="list available scanners")
    sub.add_parser("summary", help="drives, totals per category, decisions")

    s = sub.add_parser("list", help="list findings")
    s.add_argument("--category"); s.add_argument("--scanner"); s.add_argument("--status")
    s.add_argument("--min-mb", type=float, default=0); s.add_argument("--search")
    s.add_argument("--limit", type=int, default=40); s.add_argument("--offset", type=int, default=0)
    s.add_argument("--order", choices=["size", "age", "title"], default="size")

    s = sub.add_parser("show", help="full details of a finding"); s.add_argument("id")
    s = sub.add_parser("decide", help="record a decision")
    s.add_argument("status", choices=queries.STATUSES); s.add_argument("ids", nargs="+"); s.add_argument("--note")

    s = sub.add_parser("index", help="walk a drive/folder and store folder sizes"); s.add_argument("root")
    sub.add_parser("index-status")
    s = sub.add_parser("du", help="drill into an indexed folder"); s.add_argument("path"); s.add_argument("--top", type=int, default=25)
    s = sub.add_parser("bigfiles", help="largest files from the index"); s.add_argument("root", nargs="?"); s.add_argument("--top", type=int, default=40)

    s = sub.add_parser("quarantine", help="move paths/finding ids into quarantine (dry run unless --yes)")
    s.add_argument("targets", nargs="+"); s.add_argument("--reason", default=""); s.add_argument("--yes", action="store_true")
    sub.add_parser("quarantine-list")
    s = sub.add_parser("restore", help="reverse a journal entry"); s.add_argument("journal_id", type=int)
    s = sub.add_parser("purge", help="permanently delete quarantined items")
    s.add_argument("journal_ids", nargs="*", type=int); s.add_argument("--older-than", type=float)
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("path-remove"); s.add_argument("--scope", choices=["user", "machine"], required=True)
    s.add_argument("entries", nargs="+"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("path-dedupe"); s.add_argument("--scope", choices=["user", "machine"], required=True)
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("uninstall-entry-remove"); s.add_argument("id"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("uninstall", help="full uninstall: snapshot, run the program's own uninstaller, "
                                         "record what it left behind (dry run unless --yes)")
    s.add_argument("target", help="program finding id, uninstall registry key, or exact program name")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("leftovers", help="list uninstalls, or re-check what one left behind (e.g. after a restart)")
    s.add_argument("journal_id", type=int, nargs="?")
    s = sub.add_parser("leftover-remove", help="remove registry/task leftovers of an uninstall, backed up first "
                                               "(dry run unless --yes)")
    s.add_argument("ids", nargs="+"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("update", help="update apps (app-update finding ids) with winget, one at a time "
                                      "(dry run unless --yes)")
    s.add_argument("ids", nargs="+"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("update-pin", help="stop winget offering updates for these apps (winget pin; "
                                          "restorable; dry run unless --yes)")
    s.add_argument("ids", nargs="+"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("startup-disable", help="stop startup items starting with Windows (Task Manager switch / "
                                               "disable task / service to Manual; restorable; dry run unless --yes)")
    s.add_argument("ids", nargs="+"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("startup-fix", help="clean up broken startup entries, backed up first (dry run unless --yes)")
    s.add_argument("ids", nargs="+"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("driver-remove", help="remove old copies of driver packages, exported to quarantine first "
                                             "(needs --elevate; dry run unless --yes)")
    s.add_argument("ids", nargs="+"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("restore-point", help="create a System Restore point (needs --elevate; dry run unless --yes)")
    s.add_argument("--description", default="PC Cleaner: before driver changes"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("open", help="open a Windows page for the user: optional-updates, windows-update, device-manager")
    s.add_argument("what", choices=list(actions.SETTINGS_PAGES))
    s = sub.add_parser("journal"); s.add_argument("--limit", type=int, default=30)
    hw = sub.add_parser("hw", help="hardware: sensors, fan headers, inventory (read-only)")
    hws = hw.add_subparsers(dest="hw_cmd", required=True)
    hws.add_parser("info", help="inventory: board, BIOS, CPU, RAM, GPU, drives, fan software and fan headers")
    s = hws.add_parser("now", help="current readings (full readings need admin: --elevate, or a running monitor)")
    s.add_argument("--all", action="store_true", help="every sensor, not just the key ones")
    s = hws.add_parser("monitor", help="read every second into data/hw-live.json until nobody asks for "
                                      "readings for a while (run elevated: --elevate)")
    s.add_argument("--interval", type=float, default=1.0); s.add_argument("--idle-exit", type=float, default=120)
    s.add_argument("--background", action="store_true", help="start it and return straight away")
    s = hws.add_parser("name", help="name a sensor or fan header, e.g. 'Top exhaust' (empty label clears it)")
    s.add_argument("sensor_id"); s.add_argument("label")
    hws.add_parser("names", help="list named sensors")
    lg = hws.add_parser("log", help="sensor logs you start and stop (the reader runs as admin: one UAC prompt)")
    lgs = lg.add_subparsers(dest="log_cmd", required=True)
    s = lgs.add_parser("start", help="start logging every second until stopped or the time limit")
    s.add_argument("--label", default=""); s.add_argument("--max-hours", type=float, default=4)
    s.add_argument("--no-admin", action="store_true", help="log without admin (GPU, drives and CPU load only)")
    lgs.add_parser("stop")
    s = lgs.add_parser("mark", help="mark this moment in the running log ('this is the noise')"); s.add_argument("note", nargs="?", default="")
    s = lgs.add_parser("save", help="save the background reader's last 5 minutes as a log")
    s.add_argument("--label", default="Saved moment"); s.add_argument("--note", default="")
    lgs.add_parser("list")
    s = lgs.add_parser("delete", help="delete a finished log"); s.add_argument("session", type=int)
    s = hws.add_parser("report", help="summary of a log for Claude: stats, fan ramps and their causes, marks")
    s.add_argument("session", type=int, nargs="?")
    s = hws.add_parser("query", help="downsampled readings from a log for matching sensors")
    s.add_argument("match", nargs="+", help="text to match in sensor names, e.g. 'CPU Package' 'Fan #2'")
    s.add_argument("--session", type=int); s.add_argument("--from", dest="start"); s.add_argument("--to", dest="end")
    s.add_argument("--points", type=int, default=120)
    s = hws.add_parser("export", help="write a log to CSV"); s.add_argument("session", type=int); s.add_argument("path")
    s = sub.add_parser("ui", help="open the local web UI")
    s.add_argument("--port", type=int, default=8765); s.add_argument("--no-open", action="store_true")
    sub.add_parser("shortcut", help="add 'PC Cleaner' to the Desktop and Start menu")

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--out" in argv:  # used by --elevate: the elevated copy writes its output here
        i = argv.index("--out")
        sys.stdout = open(argv[i + 1], "w", encoding="utf-8")
        del argv[i:i + 2]
    if "--elevate" in argv:
        argv.remove("--elevate")
        if not is_admin():
            try:
                print(pcc_elevated([x for x in argv if x != "--json"]), end="")
            except ElevationError as e:
                _print({"error": str(e)}, True)
                return 1
            return 0
    j = "--json" in argv  # accepted anywhere, not just before the subcommand
    a = ap.parse_args([x for x in argv if x != "--json"])
    try:
        if a.cmd == "scan":
            if bad := [n for n in a.names if n not in scanners.SCANNERS and n not in scanners.EXTRA]:
                ap.error(f"unknown scanner(s) {bad}; choose from {[*scanners.SCANNERS, *scanners.EXTRA]}")
            res = scanners.run(a.names or None, progress=lambda n: print(f"scanning {n}...", file=sys.stderr, flush=True))
            _print({k: v | ({"size": human(v["bytes"])} if "bytes" in v else {}) for k, v in res.items()}, j)
        elif a.cmd == "scanners":
            _print({k: d for k, (_, d) in scanners.SCANNERS.items()}, j)
        elif a.cmd == "summary":
            _print(queries.summary(), j)
        elif a.cmd == "list":
            _print(queries.list_findings(a.category, a.scanner, a.status, a.min_mb, a.search, a.limit, a.offset, a.order), j)
        elif a.cmd == "show":
            _print(queries.get_finding(a.id) or {"error": "not found"}, j)
        elif a.cmd == "decide":
            _print({"updated": queries.decide(a.ids, a.status, a.note)}, j)
        elif a.cmd == "index":
            def prog(st):
                print(f"\r{st['files']:,} files  {human(st['bytes'])}  {st['current'][:80]:80}", end="", file=sys.stderr, flush=True)
            r = diskindex.build(a.root, progress=prog)
            print(file=sys.stderr)
            _print({k: (human(v) if k == "bytes" else v) for k, v in r.items() if k != "current"}, j)
        elif a.cmd == "index-status":
            _print(diskindex.status(), j)
        elif a.cmd == "du":
            r = diskindex.du(a.path, a.top)
            if j or "error" in r:
                _print(r, True)
            else:
                print(f"{human(r['size']):>9}  {r['path']}")
                for c in r["children"]:
                    print(f"{human(c['size']):>9}    {c['path']}")
                for f in r["big_files"]:
                    print(f"{human(f['size']):>9}    {f['path']}  (file)")
                print(f"{human(r['other_bytes']):>9}    (smaller items)")
        elif a.cmd == "bigfiles":
            _print([{"size": human(r["size"]), "path": r["path"]} for r in diskindex.largest_files(a.root, a.top)], j)
        elif a.cmd == "quarantine":
            reserved: set[str] = set()
            for t in a.targets:
                try:
                    r = (actions.quarantine(t, a.reason) if a.yes
                         else actions.plan_quarantine(t, reserved) | {"dry_run": True})
                    _print(r | {"size": human(r["size"])}, j)
                except actions.ActionError as e:
                    _print({"target": t, "error": str(e)}, j)
        elif a.cmd == "quarantine-list":
            _print([q | {"size": human(q["size"])} for q in actions.quarantined()], j)
        elif a.cmd == "restore":
            _print(actions.restore(a.journal_id), j)
        elif a.cmd == "purge":
            ids = a.journal_ids or None
            if not a.yes:
                items = [q for q in actions.quarantined() if (ids is None or q["journal_id"] in ids)
                         and (a.older_than is None or q["age_days"] >= a.older_than)]
                _print({"dry_run": True, "would_purge": items,
                        "would_free": human(sum(q["size"] for q in items))}, j)
            else:
                r = actions.purge(ids, a.older_than)
                _print(r | {"freed": human(r["freed_bytes"])}, j)
        elif a.cmd == "path-remove":
            _print(actions.path_remove(a.scope, a.entries, dry_run=not a.yes), j)
        elif a.cmd == "path-dedupe":
            _print(actions.path_dedupe(a.scope, dry_run=not a.yes), j)
        elif a.cmd == "uninstall-entry-remove":
            _print(actions.remove_uninstall_entry(a.id, dry_run=not a.yes), j)
        elif a.cmd == "uninstall":
            _print(actions.uninstall(a.target) if a.yes else actions.plan_uninstall(a.target), j)
        elif a.cmd == "leftovers":
            _print(actions.uninstall_recheck(a.journal_id) if a.journal_id else actions.uninstalls(), True)
        elif a.cmd == "restore-point":
            _print(actions.restore_point(a.description, dry_run=not a.yes), j)
        elif a.cmd == "open":
            _print(actions.open_settings(a.what), j)
        elif a.cmd in ("leftover-remove", "update", "update-pin", "startup-disable", "startup-fix", "driver-remove"):
            fn = {"leftover-remove": actions.remove_leftover, "update": actions.app_update,
                  "update-pin": actions.app_pin, "startup-disable": actions.startup_disable,
                  "startup-fix": actions.startup_fix, "driver-remove": actions.driver_remove}[a.cmd]
            for i in a.ids:
                try:
                    _print(fn(i, dry_run=not a.yes), j)
                except actions.ActionError as e:
                    _print({"id": i, "error": str(e)}, j)
                sys.stdout.flush()  # updates take a while; show each as it finishes
        elif a.cmd == "journal":
            _print(actions.journal(a.limit), True)
        elif a.cmd == "hw":
            _hw(a, j)
        elif a.cmd == "ui":
            from .ui.server import serve
            serve(a.port, open_window=not a.no_open)
        elif a.cmd == "shortcut":
            from .ui.app import install_shortcuts
            _print({"created": install_shortcuts()}, j)
    except (actions.ActionError, ElevationError, OSError) as e:
        _print({"error": str(e)}, True)
        return 1
    finally:
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
