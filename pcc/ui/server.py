"""Local web UI: `python -m pcc ui`.

Binds to 127.0.0.1 only. Every API call needs the per-launch token (sent as a
header by the page), and the Host header must be local, so other websites open
in the browser can't drive it. The UI only reads and records decisions;
anything that changes the machine stays in the conversation with Claude. The
"Action marked" button just opens that conversation (open_claude) with a fixed prompt.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import actions, diskindex, queries, scanners
from ..util import human, powershell_json

PAGE = Path(__file__).with_name("index.html")
TOKEN = secrets.token_urlsafe(24)
DATA = Path(__file__).resolve().parents[2] / "data"
INSTANCE = DATA / "ui-instance.json"   # port + token of the running app-mode UI
PROFILE = DATA / "ui-profile"          # the app window's own browser profile

_scan = {"state": "idle", "current": None, "started": None, "finished": None, "result": None}


HEALTH_MAX_AGE_S = 3600
ROOT = Path(__file__).resolve().parents[2]


def _code_stamp() -> float:
    """Newest change to PC Cleaner's Python code. The page is read from disk on every load but the
    server's code isn't, so a long-running server can be older than the page it serves."""
    return max((p.stat().st_mtime for p in (ROOT / "pcc").rglob("*.py")), default=0.0)


BUILD = _code_stamp()
_server: dict = {"httpd": None}


def _stale() -> bool:
    return _code_stamp() > BUILD + 1


_watch: dict = {"con": None}


def _data_version() -> int:
    """SQLite's PRAGMA data_version on one long-lived connection changes whenever any *other*
    connection commits: Claude acting from its terminal, the CLI, a scan, or a decision made here
    (each request uses its own connection). The page polls this to refresh itself."""
    from ..store import _lock, connect
    with _lock:
        if _watch["con"] is None:
            _watch["con"] = connect()
        return _watch["con"].execute("PRAGMA data_version").fetchone()[0]

# Fixed prompts only: the page picks a task, it never sends text for Claude to run.
# CLAUDE.md's playbook defines what "clean up what I marked" means, one confirmed batch at a time.
CLAUDE_TASKS = {"marked": ("PC Cleaner: marked items", "clean up what I marked")}
_last_claude = {"ts": 0.0}


def open_claude(task: str) -> dict:
    """Open Claude Code in a new terminal window in the project folder, with the task as its first prompt.
    The UI still changes nothing itself; that session asks before each batch."""
    name, prompt = CLAUDE_TASKS[task]
    if time.time() - _last_claude["ts"] < 15:
        return {"opened": False, "note": "Claude Code is already opening."}
    if not shutil.which("claude"):
        return {"error": "Claude Code isn't installed (npm install -g @anthropic-ai/claude-code)."}
    claude = ["claude", "--name", name, prompt]
    wt = shutil.which("wt")
    if wt:
        cmd = [wt, "-w", "new", "new-tab", "-d", str(ROOT), "--title", "PC Cleaner - Claude", "cmd", "/k", *claude]
    else:  # no Windows Terminal: a plain console window
        cmd = ["cmd", "/c", "start", "PC Cleaner - Claude", "/D", str(ROOT), "cmd", "/k", *claude]
    subprocess.Popen(cmd, cwd=str(ROOT), creationflags=0 if wt else getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _last_claude["ts"] = time.time()
    return {"opened": True, "where": "Windows Terminal" if wt else "a console window"}


def _health_scan_if_stale() -> None:
    """On open: re-run the quick health scanners (updates...) if they're over an hour old.
    The page loads straight away with the last results and refreshes when this finishes."""
    from ..store import connect
    ages = {r["scanner"]: time.time() - r["ts"] for r in connect().execute("SELECT scanner, ts FROM scans")}
    stale = [n for n in scanners.HEALTH if ages.get(n, 1e9) > HEALTH_MAX_AGE_S]
    if stale and _scan["state"] != "running":
        threading.Thread(target=_run_scan, args=(stale,), daemon=True).start()


def _run_scan(names):
    _scan.update(state="running", started=time.time(), finished=None, result=None, names=names)
    try:
        _scan["result"] = scanners.run(names, progress=lambda n: _scan.update(current=n))
        _scan["state"] = "done"
    except Exception as e:
        _scan.update(state="error", result=str(e))
    _scan.update(current=None, finished=time.time())


_fc_cache: dict = {"ts": 0.0, "data": None}


def _hw_now() -> dict:
    """The Sensors page polls this every second: the full snapshot plus names and the fan-header table.
    FanControl's config is re-read at most every 10 s."""
    from ..hw import fancontrol, info, live
    snap = live.snapshot()
    if "error" in snap:
        return snap
    if time.time() - _fc_cache["ts"] > 10:
        _fc_cache.update(ts=time.time(), data=fancontrol.read())
    from ..hw import logdb
    names = live.names()
    a = logdb.active(logdb.connect())
    log = {"id": a["id"], "label": a["label"], "started": a["started"], "picked_up": bool(a["columns"])} if a else None
    return snap | {"names": names, "log": log, "fan_headers": [{k: v for k, v in r.items() if k != "fancontrol"}
                                                   for r in info.fan_headers(snap, _fc_cache["data"], names)],
                   "fancontrol": {k: _fc_cache["data"].get(k) for k in ("installed", "running", "config")}}


class Handler(BaseHTTPRequestHandler):
    server_version = "pcc-ui"

    def log_message(self, *a):  # keep the terminal quiet
        pass

    # ---- plumbing
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, default=str).encode(), "application/json")

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "bad host"}, 403)
            return False
        if self.path.startswith("/api/") and not secrets.compare_digest(
                self.headers.get("X-PCC-Token", ""), TOKEN):
            self._json({"error": "bad token"}, 403)
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- routes
    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/summary":
                self._json(queries.summary(q.get("drive")) | {"stale": _stale()})
            elif u.path == "/api/findings":
                self._json(queries.list_findings(
                    category=q.get("category"), status=q.get("status"), search=q.get("search"),
                    order=q.get("order", "size"), drive=q.get("drive"),
                    limit=int(q.get("limit", 500)), offset=int(q.get("offset", 0))))
            elif u.path == "/api/finding":
                self._json(queries.get_finding(q["id"]) or {"error": "not found"})
            elif u.path == "/api/du":
                self._json(diskindex.du(q["path"], int(q.get("top", 40))))
            elif u.path == "/api/index":
                self._json(diskindex.status())
            elif u.path == "/api/quarantine":
                self._json([x | {"size_h": human(x["size"])} for x in actions.quarantined()])
            elif u.path == "/api/scan":
                self._json(_scan)
            elif u.path == "/api/changes":
                self._json({"v": _data_version()})
            elif u.path == "/api/hw/now":
                self._json(_hw_now())
            elif u.path == "/api/hw/logs":
                from ..hw import logdb
                self._json(logdb.sessions(100))
            elif u.path == "/api/hw/report":
                from ..hw import live, report
                self._json(report.report(int(q["id"]) if q.get("id") else None, live.names()))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e), "trace": traceback.format_exc(limit=3)}, 500)

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/decide":
                self._json({"updated": queries.decide(b["ids"], b["status"], b.get("note"))})
            elif u.path == "/api/scan":
                if _scan["state"] != "running":
                    threading.Thread(target=_run_scan, args=(b.get("names"),), daemon=True).start()
                self._json({"started": True})
            elif u.path == "/api/index":
                self._json(diskindex.build_in_background(b["root"]))
            elif u.path == "/api/claude":
                if b.get("task") not in CLAUDE_TASKS:
                    return self._json({"error": "unknown task"}, 400)
                self._json(open_claude(b["task"]))
            elif u.path == "/api/open":  # a fixed Windows page (Optional updates, Device Manager); changes nothing
                if b.get("what") not in actions.SETTINGS_PAGES:
                    return self._json({"error": "unknown page"}, 400)
                self._json(actions.open_settings(b["what"]))
            elif u.path == "/api/hw/monitor":  # fixed command; Windows asks the user (UAC) before it runs
                from ..elevate import ElevationError
                from ..hw import live
                try:
                    self._json(live.start_monitor())
                except ElevationError as e:
                    self._json({"error": str(e)})
            elif u.path == "/api/hw/log":  # start/stop/mark/save a sensor log: only reads sensors
                from ..elevate import ElevationError
                from ..hw import live, logdb
                act, label, note = b.get("action"), str(b.get("label", ""))[:80], str(b.get("note", ""))[:200]
                try:
                    r = (live.start_log(label) if act == "start" else logdb.stop() if act == "stop"
                         else logdb.mark(note or "Marked in the UI") if act == "mark"
                         else live.request_save(label or "That was loud", note or "Marked in the UI") if act == "save"
                         else {"error": "unknown action"})
                except ElevationError as e:
                    r = {"error": str(e)}
                self._json(r)
            elif u.path == "/api/hw/name":
                from ..hw import live
                self._json(live.set_name(str(b["id"]), str(b.get("label", ""))[:60]))
            elif u.path == "/api/quit":  # a newer PC Cleaner is taking over
                self._json({"quitting": True})
                if _server["httpd"]:
                    threading.Thread(target=_server["httpd"].shutdown, daemon=True).start()
            elif u.path == "/api/reveal":
                p = b["path"]
                if not (len(p) > 2 and p[1:3] == ":\\" and os.path.exists(p)):
                    return self._json({"error": "no such path"}, 400)
                subprocess.Popen(["explorer", "/select,", p] if os.path.isfile(p) else ["explorer", p])
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except (KeyError, ValueError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e), "trace": traceback.format_exc(limit=3)}, 500)


def _open_window(url: str, own_profile: bool = False) -> subprocess.Popen | None:
    """Open as a chromeless app window if Edge/Chrome is around, else a normal tab.

    With own_profile the browser runs as a separate instance, so the returned
    process lives exactly as long as the window does.
    """
    candidates = [
        shutil.which("msedge"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if exe and os.path.exists(exe):
            args = [exe, f"--app={url}", "--window-size=1400,900"]
            if own_profile:
                _prepare_profile()
                args[1:1] = [f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check"]
            return subprocess.Popen(args)
    webbrowser.open(url)
    return None


def _profile_in_use() -> bool:
    """Edge holds <profile>/lockfile exclusively while any window of that profile is open."""
    try:
        open(PROFILE / "lockfile", "a").close()
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _exit_when_window_closes(httpd) -> None:
    # Watch the profile lock, not the launched process: Edge sometimes relaunches itself
    # into a new process, so the one we started can exit while the window stays open.
    deadline = time.time() + 30
    while not _profile_in_use():
        if time.time() > deadline:
            return  # never saw the window; keep serving rather than vanish under it
        time.sleep(0.5)
    while _profile_in_use():
        time.sleep(2)
    httpd.shutdown()


def _running_instance() -> str | None:
    """URL of an app-mode UI that is already serving, if any."""
    try:
        info = json.loads(INSTANCE.read_text())
        base, hdr = f"http://127.0.0.1:{info['port']}", {"X-PCC-Token": info["token"]}
        urllib.request.urlopen(urllib.request.Request(f"{base}/api/index", headers=hdr), timeout=2).close()
    except (OSError, ValueError, KeyError):
        return None
    if info.get("build", 0) >= BUILD - 1:
        return info["url"]
    # It's running older code than is on disk: replace it, window and all.
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/quit", data=b"{}", headers=hdr | {"Content-Type": "application/json"}), timeout=2).close()
    except OSError:
        _kill(_other_servers())  # too old to know /api/quit
    _kill(_profile_browsers())   # its window, and the background browser that kept the server alive
    time.sleep(1)
    return None


def _ps_pids(script: str) -> list[int]:
    return [r["ProcessId"] for r in powershell_json(script, timeout=60) if isinstance(r, dict) and r.get("ProcessId")]


def _other_servers() -> list[int]:
    """Other PC Cleaner app-mode servers (the shortcut's `pythonw -m pcc.ui.app`), not this one."""
    pids = _ps_pids("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                    "Where-Object { $_.CommandLine -match 'pcc\\.ui\\.app' } | Select-Object ProcessId")
    return [p for p in pids if p not in (os.getpid(), os.getppid())]


def _profile_browsers() -> list[int]:
    """Browser processes using PC Cleaner's own window profile. Nothing else uses it."""
    prof = str(PROFILE).replace("'", "''")
    return _ps_pids("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -like '*--user-data-dir={prof}*' -and "
                    "$_.CommandLine -notlike '*--type=*' } | Select-Object ProcessId")


def _kill(pids: list[int]) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _prepare_profile() -> None:
    """Edge's default is to keep running after its last window closes ("continue running background
    apps"). That holds the profile lock, so the server never notices the window closed and lives on
    with old code. Turn it off for PC Cleaner's own profile (it's read when the browser starts)."""
    if _profile_in_use():
        return
    f = PROFILE / "Local State"
    try:
        state = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        state = {}
    if state.get("background_mode", {}).get("enabled") is False:
        return
    state.setdefault("background_mode", {})["enabled"] = False
    PROFILE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state), encoding="utf-8")


def serve(port: int = 8765, open_window: bool = True, app: bool = False) -> None:
    """app=True is the desktop-shortcut mode: one instance, and it exits when its window closes."""
    if app:
        url = _running_instance()
        if url:
            _open_window(url, own_profile=True)  # hands off to the running window's browser
            return
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        if not app:
            raise
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port taken by something else
    url = f"http://127.0.0.1:{httpd.server_port}/#t={TOKEN}"
    print(f"PC Cleaner UI: {url}\n(Ctrl+C to stop)", flush=True)
    _server["httpd"] = httpd
    _health_scan_if_stale()
    if app:
        INSTANCE.write_text(json.dumps({"port": httpd.server_port, "token": TOKEN, "url": url,
                                        "pid": os.getpid(), "build": BUILD}))
        if _open_window(url, own_profile=True):
            threading.Thread(target=_exit_when_window_closes, args=(httpd,), daemon=True).start()
    elif open_window:
        _open_window(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if app:
            try:  # a newer instance may have taken over; only remove our own record
                if json.loads(INSTANCE.read_text()).get("pid") == os.getpid():
                    INSTANCE.unlink()
            except (OSError, ValueError):
                pass
