"""Run something elevated (UAC prompt) from an unelevated process, and wait for it.

Claude's terminal and the MCP server run unelevated, so anything that needs admin (an
uninstaller, quarantining from Program Files, HKLM edits) goes through here.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

ERROR_CANCELLED = 1223          # the user said No to the UAC prompt
WAIT_TIMEOUT = 0x102
SEE_MASK_NOCLOSEPROCESS = 0x40
SEE_MASK_NOASYNC = 0x100


class ElevationError(Exception):
    pass


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
_shell32.ShellExecuteExW.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def run_elevated(exe: str, params: str = "", cwd: str | None = None,
                 timeout_s: float | None = None, show: int = 1) -> int | None:
    """Start exe with the 'runas' verb and wait. Returns its exit code, or None if it is
    still running at timeout_s. Raises ElevationError if the UAC prompt is declined."""
    sei = _ShellExecuteInfo()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params or None
    sei.lpDirectory = cwd
    sei.nShow = show
    if not _shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise ElevationError("The admin (UAC) prompt was declined; nothing was run.")
        raise ElevationError(f"Could not start {exe}: {ctypes.FormatError(err).strip()}")
    if not sei.hProcess:
        return None
    try:
        ms = 0xFFFFFFFF if timeout_s is None else int(timeout_s * 1000)
        if _kernel32.WaitForSingleObject(sei.hProcess, ms) == WAIT_TIMEOUT:
            return None
        code = wintypes.DWORD()
        _kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return code.value
    finally:
        _kernel32.CloseHandle(sei.hProcess)


def pitstop_elevated(args: list[str], timeout_s: float = 3600) -> str:
    """Run `python -m pitstop <args> --json` elevated (hidden window) and return what it printed."""
    root = Path(__file__).resolve().parent.parent
    fd, out = tempfile.mkstemp(prefix="pitstop-elevated-", suffix=".txt")
    os.close(fd)
    try:
        params = subprocess.list2cmdline(["-m", "pitstop", *args, "--json", "--out", out])
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        if run_elevated(exe, params, cwd=str(root), timeout_s=timeout_s, show=0) is None:
            raise ElevationError(f"Elevated run did not finish within {timeout_s:.0f} s")
        return Path(out).read_text(encoding="utf-8")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def parse_json_stream(text: str) -> list:
    """The CLI prints one JSON document per target; return them as a list."""
    dec, i, out = json.JSONDecoder(), 0, []
    text = text.strip()
    while i < len(text):
        obj, end = dec.raw_decode(text, i)
        out.append(obj)
        i = end
        while i < len(text) and text[i].isspace():
            i += 1
    return out
