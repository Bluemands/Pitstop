"""`python -m pitstop hw setup`: get what full sensor readings need.

1. LibreHardwareMonitorLib (MPL-2.0) and its .NET Framework dependencies, from nuget.org into vendor/lhm.
   Every package is pinned by version and SHA-256; anything else is refused. (If FanControl is installed,
   its copy works too and this step is optional.)
2. The PawnIO driver, which LibreHardwareMonitor uses for CPU and motherboard sensors with admin. It's a
   signed driver installed separately; setup checks for it and says how to install it, but never installs
   a driver itself. The old WinRing0 driver is never used: Defender flags it as vulnerable.
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

from ..util import run

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "lhm"
NUGET = "https://www.nuget.org/api/v2/package/{id}/{ver}"
TOP = ("LibreHardwareMonitorLib", "0.9.6")

# (package, version) -> sha256 of the .nupkg, for LibreHardwareMonitorLib 0.9.6 and everything it needs on
# .NET Framework 4.7.2. Regenerate with `python -m pitstop hw setup --discover` after changing TOP, and review.
PINNED: dict[tuple[str, str], str] = {
    ("LibreHardwareMonitorLib", "0.9.6"): "60db5e0d9135f4658b385cae139d3b9eaeac0a8e867d322d20b5dbe4c4039197",
    ("DiskInfoToolkit", "1.1.2"): "a997f0b3701e20b2ab9296e07ee4f74f877e75015277fb6bd314ade1e2c4db13",
    ("HidSharp", "2.6.4"): "cf55d4b4d7b42dc1d8496b7b592ba00d1ecd48a70bc07bd8dabd3e9976d108e7",
    ("RAMSPDToolkit-NDD", "1.4.2"): "57c893824a68c71538055f242586616d4205a671c300897c21b2a6f88270ce64",
    ("System.Management", "10.0.2"): "bb40e7e33254bd2a5fb66b6f476e1e9ea28f0f1c403172696afdbfb1f8b06221",
    ("System.Memory", "4.6.3"): "26078aeb758c9ae985e8bf851f973026061da6a5eb4837204d0c2d2204c72955",
    ("System.Threading.AccessControl", "10.0.3"): "9f4f28238e13c6cc47b789a4a1d5647997747594dfcfd45c1526f9b671cde073",
    ("BlackSharp.Core", "1.0.7"): "4f713fe18ee7c85a7c49f28309adaa815714333326aa81fd4d4a81619ecaa0ab",
    ("System.CodeDom", "10.0.2"): "30028b346c7b99ccdc484ea7edbdc7bd038b3c2f007e4e4cbfa3d45768651c2d",
    ("System.Buffers", "4.6.1"): "b00451e91d016fbec091ad1e361f3a7015e1d91d4047f7e48a74455b2a673d79",
    ("System.Numerics.Vectors", "4.6.1"): "2bc500a86dcb02f2032d6d877f9e2d6e9e4a79080e57239b4198679d4031f2c7",
    ("System.Runtime.CompilerServices.Unsafe", "6.1.2"): "5f6a7f53af3465f92beb6da873ebe0e496206c313313b98badee4355a6b25937",
    ("System.Security.AccessControl", "6.0.0"): "a8ec961016cdaf7123c92f9eb451bcff331bb8f8c0f8ef9d8bbd7b24ff42c728",
    ("System.Security.Principal.Windows", "5.0.0"): "081390c25f6f78592b28ada853c24514488a221fe9f9a24efaaf5373643ff3d6",
}

# Frameworks a .NET Framework 4.8 process can load, best first.
_TFMS = ["net48", "net472", "net471", "net47", "net462", "net461", "net46", "net452", "net45",
         "netstandard2.0", "netstandard1.3"]
_GROUPS = [".NETFramework4.8", ".NETFramework4.7.2", ".NETFramework4.7.1", ".NETFramework4.7", ".NETFramework4.6.2",
           ".NETFramework4.6.1", ".NETFramework4.6", ".NETFramework4.5", ".NETStandard2.0", ".NETStandard1.3"]


class SetupError(Exception):
    pass


def _fetch(pid: str, ver: str) -> bytes:
    req = urllib.request.Request(NUGET.format(id=pid, ver=ver), headers={"User-Agent": "pitstop-setup"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _deps(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    spec = next(n for n in z.namelist() if n.endswith(".nuspec") and "/" not in n)
    text = z.read(spec).decode("utf-8", "replace")
    groups = dict(re.findall(r'<group targetFramework="([^"]+)"\s*(?:/>|>(.*?)</group>)', text, re.S))
    for g in _GROUPS:
        if g in groups:
            return [(i, v.strip("[]() ").split(",")[0]) for i, v in
                    re.findall(r'<dependency id="([^"]+)" version="([^"]+)"', groups[g] or "")]
    return []


def _dlls(z: zipfile.ZipFile) -> dict[str, bytes]:
    names = z.namelist()
    for base in ("runtimes/win-x64/lib/", "lib/"):
        for tfm in _TFMS:
            pre = f"{base}{tfm}/"
            got = {n[len(pre):]: z.read(n) for n in names if n.startswith(pre) and n.lower().endswith(".dll")
                   and "/" not in n[len(pre):]}
            if got or any(n == pre + "_._" for n in names):
                return got   # "_._" means the framework already has it
    return {}


def _resolve(verify: bool) -> list[tuple[str, str, str, dict]]:
    seen, out, queue = set(), [], [TOP]
    while queue:
        pid, ver = queue.pop(0)
        if pid.lower() in seen:
            continue
        seen.add(pid.lower())
        data = _fetch(pid, ver)
        sha = hashlib.sha256(data).hexdigest()
        if verify:
            want = PINNED.get((pid, ver))
            if want is None:
                raise SetupError(f"{pid} {ver} isn't in the pinned list; refusing to install it.")
            if want != sha:
                raise SetupError(f"{pid} {ver} doesn't match its pinned SHA-256; refusing to install it.")
        z = zipfile.ZipFile(io.BytesIO(data))
        out.append((pid, ver, sha, _dlls(z)))
        queue += _deps(z)
    return out


def discover() -> list[dict]:
    """Resolve without verifying and print what would be pinned (for maintainers)."""
    return [{"package": p, "version": v, "sha256": s, "dlls": sorted(d)} for p, v, s, d in _resolve(False)]


def install_lhm() -> dict:
    pkgs = _resolve(True)
    tmp = VENDOR.with_name("lhm.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    files = []
    for _, _, _, dlls in pkgs:
        for name, blob in dlls.items():
            (tmp / name).write_bytes(blob)
            files.append(name)
    shutil.rmtree(VENDOR, ignore_errors=True)
    tmp.rename(VENDOR)
    return {"installed": str(VENDOR), "packages": [f"{p} {v}" for p, v, _, _ in pkgs], "files": sorted(files)}


def pawnio_status() -> dict:
    out = run(["sc", "query", "PawnIO"], timeout=15)
    if "RUNNING" in out:
        return {"pawnio": "running"}
    if "STOPPED" in out:
        return {"pawnio": "installed (starts when a sensor reader needs it)"}
    return {"pawnio": "not installed",
            "how": "Install PawnIO (a signed driver by namazso) from https://pawnio.eu, or run "
                   "`winget install namazso.PawnIO`. Without it, CPU temperatures and motherboard fans can't be read."}


def setup(skip_download: bool = False) -> dict:
    from .lhm import find_lhm_dir
    r: dict = {}
    have = find_lhm_dir()
    if have:
        r["lhm"] = f"found in {have}"
    elif skip_download:
        r["lhm"] = "not found (run without --skip-download to fetch it)"
    else:
        r["lhm"] = install_lhm()
        r["note"] = ("Known limit of the NuGet copy: NVMe drive temperatures aren't read yet. Everything else is. "
                     "If FanControl is installed, its copy is used instead and reads drives too.")
    r |= pawnio_status()
    try:
        import subprocess
        import sys
        test = subprocess.run([sys.executable, "-c", "from pitstop.hw.lhm import Reader; r=Reader(); "
                               "print(len(list(r.read()['hardware'])))"], cwd=str(ROOT), capture_output=True,
                              text=True, timeout=90)
        r["test_read"] = (f"ok: {test.stdout.strip()} hardware groups" if test.returncode == 0
                          else f"failed: {test.stderr.strip().splitlines()[-1] if test.stderr.strip() else '?'}")
    except Exception as e:
        r["test_read"] = f"failed: {e}"
    return r
