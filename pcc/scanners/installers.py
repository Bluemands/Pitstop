"""Setup files, disk images and archives lying around in Downloads / Desktop."""
from __future__ import annotations

import os

from ..store import Finding
from ..util import HOME, age_days

EXTS = {
    ".exe": "installer", ".msi": "installer", ".msix": "installer", ".appx": "installer",
    ".iso": "disk-image", ".img": "disk-image", ".vhd": "disk-image", ".vhdx": "disk-image",
    ".zip": "archive", ".7z": "archive", ".rar": "archive", ".tar": "archive", ".gz": "archive",
    ".xz": "archive", ".tgz": "archive",
}
ROOTS = [HOME / "Downloads", HOME / "Desktop", HOME / "OneDrive" / "Desktop"]
MIN_BYTES = 20 * 1024 * 1024


def scan() -> list[Finding]:
    out: list[Finding] = []
    for root in ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Stay shallow: extracted program folders are the orphans scanner's job.
            if dirpath.count(os.sep) - str(root).count(os.sep) >= 2:
                dirnames[:] = []
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in EXTS:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size < MIN_BYTES:
                    continue
                kind = EXTS[ext]
                extracted = kind == "archive" and os.path.isdir(os.path.splitext(p)[0])
                out.append(Finding(
                    scanner="installers", category=kind, title=fn, path=p, size=st.st_size,
                    mtime=st.st_mtime, risk="low",
                    suggestion=("Already extracted next to it; the archive is redundant." if extracted else
                                "Installers can be re-downloaded; check the program is installed or no longer wanted."
                                if kind == "installer" else ""),
                    evidence={"age_days": age_days(st.st_mtime), "extracted_copy_exists": extracted},
                ))
    return out
