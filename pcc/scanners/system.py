"""Windows' own reclaimable space. Mostly report-only: the right tool to clean
these is usually Windows itself (Disk Cleanup, DISM, powercfg)."""
from __future__ import annotations

import glob
import os
import string

from ..store import Finding
from ..util import HOME, LOCAL, PROGRAMDATA, WINDIR, age_days, dir_size, file_size

# (title, glob, risk, command, suggestion)
DIRS = [
    ("User temp folder", r"%TEMP%", "low", None, "Delete contents older than a few days; in-use files will be skipped."),
    ("Windows temp folder", r"%WINDIR%\Temp", "low", None, "Needs admin. Disk Cleanup > Temporary files."),
    ("Windows Update download cache", r"%WINDIR%\SoftwareDistribution\Download", "low", "cleanmgr /sageset", "Disk Cleanup > Windows Update Cleanup."),
    ("Delivery Optimization cache", r"%WINDIR%\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache", "low", "Delete-DeliveryOptimizationCache -Force", ""),
    ("Previous Windows installation", r"C:\Windows.old", "low", None, "Disk Cleanup > Previous Windows installation(s). Removes ability to roll back."),
    ("Windows upgrade leftovers", r"C:\$WINDOWS.~BT", "low", None, "Disk Cleanup > Temporary Windows installation files."),
    ("Windows upgrade leftovers", r"C:\$Windows.~WS", "low", None, ""),
    ("Windows error reports", r"%ProgramData%\Microsoft\Windows\WER", "low", None, "Disk Cleanup > Windows error reports."),
    ("App crash dumps", r"%LOCALAPPDATA%\CrashDumps", "low", None, "Only useful for debugging crashes."),
    ("Kernel minidumps", r"%WINDIR%\Minidump", "low", None, ""),
    ("Installer patch cache", r"%WINDIR%\Installer\$PatchCache$", "medium", None, "Removing breaks patch uninstall for some MSI products."),
    ("DirectX shader cache", r"%LOCALAPPDATA%\D3DSCache", "low", None, "Rebuilt automatically."),
    ("NVIDIA shader cache", r"%LOCALAPPDATA%\NVIDIA\DXCache", "low", None, "Rebuilt automatically (games may stutter briefly)."),
    ("NVIDIA GL cache", r"%LOCALAPPDATA%\NVIDIA\GLCache", "low", None, ""),
    ("NVIDIA driver extract leftovers", r"C:\NVIDIA", "low", None, "Unpacked driver installers."),
    ("AMD driver extract leftovers", r"C:\AMD", "low", None, "Unpacked driver installers."),
    ("Intel driver extract leftovers", r"C:\Intel", "low", None, ""),
    ("Thumbnail cache", r"%LOCALAPPDATA%\Microsoft\Windows\Explorer", "low", None, "Disk Cleanup > Thumbnails."),
    ("Chrome cache", r"%LOCALAPPDATA%\Google\Chrome\User Data\*\Cache", "low", None, "Browser cache; clear from the browser."),
    ("Chrome code cache", r"%LOCALAPPDATA%\Google\Chrome\User Data\*\Code Cache", "low", None, ""),
    ("Edge cache", r"%LOCALAPPDATA%\Microsoft\Edge\User Data\*\Cache", "low", None, ""),
    ("Vivaldi cache", r"%LOCALAPPDATA%\Vivaldi\User Data\*\Cache", "low", None, ""),
    ("Brave cache", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\*\Cache", "low", None, ""),
    ("Firefox cache", r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\cache2", "low", None, ""),
    ("Discord cache", r"%APPDATA%\discord\Cache", "low", None, ""),
    ("Spotify cache", r"%LOCALAPPDATA%\Spotify\Storage", "low", None, "Offline/streaming cache; size can be capped in Spotify settings."),
    ("Steam shader cache", r"C:\Program Files (x86)\Steam\steamapps\shadercache", "low", None, ""),
]
MIN_BYTES = 50 * 1024 * 1024


def covered_paths() -> list[str]:
    return [p for _, pat, *_ in DIRS for p in glob.glob(os.path.expandvars(pat))]


def scan() -> list[Finding]:
    out: list[Finding] = []
    for title, pattern, risk, cmd, sugg in DIRS:
        for path in glob.glob(os.path.expandvars(pattern)):
            if not os.path.isdir(path):
                continue
            size, files, newest, complete = dir_size(path, budget_s=60)
            if size < MIN_BYTES:
                continue
            out.append(Finding(
                scanner="system", category="system-cache", title=title, path=path, size=size,
                mtime=newest or None, risk=risk, suggestion=sugg, command=cmd,
                evidence={"files": files, "size_complete": complete},
            ))

    # Big system files at drive roots, and Recycle Bins
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        for fname, title, sugg, cmd in [
            ("hiberfil.sys", "Hibernation file", "Disable hibernation if you never hibernate (also disables Fast Startup).", "powercfg /hibernate off"),
            ("pagefile.sys", "Page file", "System-managed; can be capped or moved to another drive. Don't disable outright.", None),
            ("swapfile.sys", "Swap file", "Managed by Windows.", None),
        ]:
            size = file_size(root + fname)
            if size:
                out.append(Finding(scanner="system", category="system-file", title=f"{title} ({letter}:)",
                                   path=root + fname, size=size, risk="medium", suggestion=sugg, command=cmd))
        rb = root + "$Recycle.Bin"
        if os.path.isdir(rb):
            size, files, newest, _ = dir_size(rb, budget_s=30)
            if size >= MIN_BYTES:
                out.append(Finding(scanner="system", category="recycle-bin", title=f"Recycle Bin ({letter}:)",
                                   path=rb, size=size, mtime=newest or None, risk="low",
                                   suggestion="Empty the Recycle Bin (only your own items are counted without admin).",
                                   command=f"Clear-RecycleBin -DriveLetter {letter} -Force",
                                   evidence={"files": files}))
        # Stray temp files dumped at a drive root by installers/recovery tools.
        for p in glob.glob(root + "*.tmp"):
            if (size := file_size(p)) >= 10 * 1024 * 1024:
                out.append(Finding(scanner="system", category="junk-file", title=f"Temp file at drive root: {os.path.basename(p)}",
                                   path=p, size=size, mtime=os.path.getmtime(p), risk="low",
                                   suggestion="Leftover temp file at a drive root. Check it isn't open (e.g. by a recovery/imaging tool), then remove."))

    # Java heap dumps written on crashes (Android Studio, IntelliJ, Minecraft...)
    for p in glob.glob(str(HOME / "*.hprof")) + glob.glob(str(HOME / "*" / "*.hprof")):
        out.append(Finding(scanner="system", category="junk-file", title=f"Java heap dump: {os.path.basename(p)}",
                           path=p, size=file_size(p), mtime=os.path.getmtime(p), risk="low",
                           suggestion="Written when a Java app ran out of memory. Only useful for debugging that crash."))

    dump = WINDIR / "MEMORY.DMP"
    if size := file_size(dump):
        out.append(Finding(scanner="system", category="system-file", title="Full memory dump", path=str(dump),
                           size=size, risk="low", suggestion="Left by a past BSOD. Disk Cleanup > System error memory dump files."))
    return out
