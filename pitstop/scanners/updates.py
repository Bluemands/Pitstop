"""Apps with a newer version available (winget), and runtimes past end of support.

Read-only: lists what `winget upgrade` would do and tags each update so the user can
decide quickly. Updating goes through actions.app_update, one confirmed batch at a time.
"""
from __future__ import annotations

import datetime
import re
import sys

from .. import inventory as inv
from ..store import Finding
from ..util import norm_key, run

WINGET_LIST = ["winget", "upgrade", "--include-unknown", "--accept-source-agreements", "--disable-interactivity"]

# Opens files or network content, or other software runs on it: worth updating promptly.
SECURITY = ("7zip.", "RARLab.", "Git.Git", "OpenJS.NodeJS", "Python.Python.", "Notepad++.", "Microsoft.OpenJDK",
            "EclipseAdoptium.", "Oracle.Java", "Amazon.Corretto", "Microsoft.DotNet.", "Microsoft.WSL",
            "Docker.DockerDesktop", "Mozilla.Firefox", "Google.Chrome", "Brave.", "Opera.", "VideoLAN.",
            "Transmission.", "qBittorrent.", "WinSCP.", "PuTTY.", "Adobe.Acrobat", "Zoom.", "OpenVPNTechnologies.",
            "WireGuard.", "Microsoft.PowerShell", "Microsoft.Edge", "TheDocumentFoundation.", "Foxit.",
            "WiresharkFoundation.", "KeePassXCTeam.", "Bitwarden.", "Telegram.", "OpenWhisperSystems.",
            "Microsoft.VCRedist", "Microsoft.Teams", "SlackTechnologies.")
# Update themselves when opened; winget just sees the version from the last time they ran.
SELF_UPDATING = ("Spotify.", "Discord.", "Microsoft.Office", "Valve.Steam", "EpicGames.", "ElectronicArts.", "GOG.",
                 "Ubisoft.", "RockstarGames.", "Notion.", "Anysphere.Cursor", "Microsoft.VisualStudioCode",
                 "Google.Antigravity", "Meta.Oculus", "ByteDance.", "Microsoft.Teams", "SlackTechnologies.", "Zoom.",
                 "Google.Chrome", "Mozilla.Firefox", "Opera.", "Brave.", "ThePokemonCompany.", "Ollama.",
                 "Playnite.", "Stremio.", "Blizzard.", "RiotGames.", "Overwolf.", "Microsoft.Edge", "Docker.DockerDesktop")
# Have their own updater, which handles them better than winget.
OWN_INSTALLER = {"Microsoft.VisualStudio.": "the Visual Studio Installer",
                 "Microsoft.Office": "any Office app: File > Account > Update Options > Update Now"}
_SOURCES = {"winget", "msstore"}


def _starts(wid: str, prefixes) -> bool:
    return any(wid.lower().startswith(p.lower()) for p in prefixes)


def _major(v: str | None) -> int | None:
    m = re.search(r"\d+", v or "")
    return int(m.group()) if m else None


def parse_upgrade_table(text: str) -> list[dict]:
    """Rows of the first table `winget upgrade` prints. Later tables are pinned packages."""
    lines = [ln.rstrip() for ln in re.split(r"[\r\n]+", text)]
    out: list[dict] = []
    starts: list[int] | None = None
    for i, line in enumerate(lines):
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if starts is None:
            if re.fullmatch(r"-{20,}", nxt) and re.search(r"\bId\b", line):
                starts = [m.start() for m in re.finditer(r"\S+", line)]
            continue
        if re.fullmatch(r"-+", line.strip()):
            continue
        row = _parse_row(line, starts)
        if row is None:
            if out:
                break  # end of the first table ("55 upgrades available.")
            continue
        out.append(row)
    return out


_ROW = re.compile(r"^(?P<name>.+?)\s+(?P<id>\S+)\s+(?P<ver>(?:[<>]\s)?\S+)\s+(?P<avail>\S+)\s+(?P<src>\S+)$")


def _parse_row(line: str, starts: list[int]) -> dict | None:
    if len(starts) < 4 or len(line) <= starts[3]:
        return None
    bounds = list(zip(starts, starts[1:] + [None]))
    parts = [line[a:b].strip() for a, b in bounds]
    if len(parts) >= 5 and parts[1] and " " not in parts[1] and parts[4] in _SOURCES:
        name, wid, ver, avail, src = parts[:5]
    else:
        # Names with wide characters push the columns out of line; fall back to splitting from the right.
        m = _ROW.match(line.strip())
        if not m or m["src"] not in _SOURCES:
            return None
        name, wid, ver, avail, src = m["name"], m["id"], m["ver"], m["avail"], m["src"]
    if not (name and wid and avail):
        return None
    return {"name": name, "id": wid, "installed": ver, "available": avail, "source": src}


def _update_finding(row: dict) -> Finding:
    wid, ver, avail = row["id"], row["installed"], row["available"]
    tags: list[str] = []
    if _starts(wid, SECURITY):
        tags.append("security")
    a, b = _major(ver), _major(avail)
    major = bool(a is not None and b is not None and b > a and a < 1990)  # 2025 -> 2026 is CalVer, not major
    if major:
        tags.append("major")
    if _starts(wid, SELF_UPDATING):
        tags.append("self-updating")
    own = next((how for p, how in OWN_INSTALLER.items() if wid.lower().startswith(p.lower())), None)
    if own:
        tags.append("use-own-installer")
    unsure = ver.startswith(("<", ">")) or ver.lower() == "unknown"

    notes = []
    if own:
        notes.append(f"Update it from {own}; winget handles it less well.")
    if "security" in tags:
        notes.append("Security-relevant: it opens files or network content, or other software runs on it. "
                     "Worth updating soon.")
    if major:
        notes.append(f"Major version jump ({a} → {b}): expect changes to the interface or settings. "
                     "Skim the release notes first.")
    if "self-updating" in tags and not own:
        notes.append("Updates itself when you open it, so opening it works too.")
    if unsure:
        notes.append("winget can't read the installed version exactly; updating is still fine.")
    return Finding(
        scanner="updates", category="app-update", title=row["name"], path=None, size=0,
        risk="medium" if (major or own) else "low",
        suggestion=" ".join(notes) or "Newer version available.",
        command=None if own else (f"winget upgrade --id {wid} --exact --silent "
                                  "--accept-package-agreements --accept-source-agreements"),
        evidence={"winget_id": wid, "installed": ver, "available": avail, "source": row["source"],
                  "tags": tags, "own_installer": own},
        key=f"winget:{wid}@{avail}",
    )


# ---- runtimes past end of support ------------------------------------------------
# (family, pattern on the Apps & Features name, {major/minor: end of support})
_EOL = [
    ("Python", re.compile(r"^Python (3\.\d+)\.\d+ \((?:64|32)-bit\)$"),
     {"3.7": "2023-06-27", "3.8": "2024-10-07", "3.9": "2025-10-31", "3.10": "2026-10-31", "3.11": "2027-10-31"},
     "3.13"),
    (".NET", re.compile(r"^Microsoft (?:Windows Desktop Runtime|\.NET (?:Runtime|SDK)|ASP\.NET Core)\D*?(\d+)\.\d+"),
     {"3": "2022-12-13", "5": "2022-05-10", "6": "2024-11-12", "7": "2024-05-14", "8": "2026-11-10",
      "9": "2026-11-10"},
     "10"),
    ("Node.js", re.compile(r"^Node\.js$"),
     {"16": "2023-09-11", "18": "2025-04-30", "20": "2026-04-30", "22": "2027-04-30"},
     "24"),
]
SOON_DAYS = 90


def _python_home(ver: str) -> str | None:
    """The Python installer's Apps & Features entry has no InstallLocation; the py launcher's registry does."""
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            return winreg.QueryValue(hive, rf"Software\Python\PythonCore\{ver}\InstallPath").rstrip("\\") or None
        except OSError:
            continue
    return None


def _eol_findings() -> list[Finding]:
    today = datetime.date.today()
    own_python = norm_key(getattr(sys, "_base_executable", sys.executable)).rsplit("\\", 1)[0]
    out = []
    for p in inv.programs():
        name = p["name"] or ""
        if not name or p["system_component"] or p["is_update"]:
            continue
        for family, pat, table, current in _EOL:
            m = pat.match(name)
            if not m:
                continue
            ver = m.group(1) if m.groups() else str(_major(p["version"]) or "")
            end = table.get(ver)
            if not end:
                continue
            end_d = datetime.date.fromisoformat(end)
            if end_d > today + datetime.timedelta(days=SOON_DAYS):
                continue
            ended = end_d <= today
            when = f"stopped getting security fixes on {end}" if ended else f"stops getting security fixes on {end}"
            home = p["install_dir"] or (_python_home(ver) if family == "Python" else None)
            uses_pitstop = bool(home) and own_python.startswith(norm_key(home))
            text = (f"{family} {ver} {when}. Anything built on it keeps working, it just won't get fixes. "
                    f"Don't uninstall it while things still use it: ")
            text += {"Python": "virtual environments (.venv) made from it break if it's removed. Move projects to "
                               f"Python {current} as you touch them.",
                     ".NET": "apps built for it need it to start. Developers move apps to newer .NET over time; "
                             "remove it only if nothing asks for it.",
                     "Node.js": f"switch projects to Node {current} LTS."}[family]
            if uses_pitstop:
                text += " Pitstop's own .venv runs on this Python."
            out.append(Finding(
                scanner="updates", category="eol-runtime", title=name, path=home, size=0,
                risk="high", suggestion=text, command=None,
                evidence={"version": p["version"], "end_of_support": end, "ended": ended,
                          "supported_version": f"{family} {current}", "used_by_pitstop": uses_pitstop or None},
                key=f"{family}:{name}",
            ))
            break
    return out


def scan() -> list[Finding]:
    text = run(WINGET_LIST, timeout=300)
    if not text.strip():
        raise RuntimeError("winget produced no output (is App Installer installed?)")
    return [_update_finding(r) for r in parse_upgrade_table(text)] + _eol_findings()
