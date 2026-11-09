"""Desktop entry point: `pythonw -m pitstop.ui.app` (what the Pitstop shortcut runs).

No console window; the program exits when its window is closed. `python -m pitstop shortcut`
creates the Desktop and Start menu shortcuts.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import traceback
from pathlib import Path

from .server import DATA, serve

ICON = Path(__file__).with_name("pitstop.ico")
PROJECT = DATA.parent


def main() -> None:
    DATA.mkdir(exist_ok=True)
    # pythonw has no console, so print() would fail; keep a log instead.
    sys.stdout = sys.stderr = open(DATA / "ui.log", "a", encoding="utf-8", buffering=1)
    try:
        serve(app=True)
    except Exception as e:
        traceback.print_exc()
        ctypes.windll.user32.MessageBoxW(
            None, f"Pitstop couldn't start:\n\n{e}\n\nDetails in {DATA / 'ui.log'}", "Pitstop", 0x10)


def install_shortcuts() -> list[str]:
    """Create 'Pitstop' shortcuts on the Desktop and in the Start menu."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    places = [Path(os.path.expanduser("~")) / "Desktop",
              Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"]
    made = []
    for folder in places:
        lnk = folder / "Pitstop.lnk"
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PITSTOP_LNK);"
            "$s.TargetPath = $env:PITSTOP_EXE; $s.Arguments = '-m pitstop.ui.app';"
            "$s.WorkingDirectory = $env:PITSTOP_DIR; $s.IconLocation = $env:PITSTOP_ICO;"
            "$s.Description = 'Find forgotten space and decide what to clean'; $s.Save()"
        )
        env = os.environ | {"PITSTOP_LNK": str(lnk), "PITSTOP_EXE": str(pythonw), "PITSTOP_DIR": str(PROJECT), "PITSTOP_ICO": str(ICON)}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        made.append(str(lnk))
    return made


if __name__ == "__main__":
    main()
