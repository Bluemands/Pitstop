"""Desktop entry point: `pythonw -m pcc.ui.app` (what the PC Cleaner shortcut runs).

No console window; the program exits when its window is closed. `python -m pcc shortcut`
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

ICON = Path(__file__).with_name("pcc.ico")
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
            None, f"PC Cleaner couldn't start:\n\n{e}\n\nDetails in {DATA / 'ui.log'}", "PC Cleaner", 0x10)


def install_shortcuts() -> list[str]:
    """Create 'PC Cleaner' shortcuts on the Desktop and in the Start menu."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    places = [Path(os.path.expanduser("~")) / "Desktop",
              Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"]
    made = []
    for folder in places:
        lnk = folder / "PC Cleaner.lnk"
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PCC_LNK);"
            "$s.TargetPath = $env:PCC_EXE; $s.Arguments = '-m pcc.ui.app';"
            "$s.WorkingDirectory = $env:PCC_DIR; $s.IconLocation = $env:PCC_ICO;"
            "$s.Description = 'Find forgotten space and decide what to clean'; $s.Save()"
        )
        env = os.environ | {"PCC_LNK": str(lnk), "PCC_EXE": str(pythonw), "PCC_DIR": str(PROJECT), "PCC_ICO": str(ICON)}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        made.append(str(lnk))
    return made


if __name__ == "__main__":
    main()
