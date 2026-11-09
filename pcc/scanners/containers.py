"""WSL distros, Docker Desktop and VM disk images."""
from __future__ import annotations

import glob
import os
import winreg

from ..store import Finding
from ..util import HOME, LOCAL, age_days, dir_size, file_size, run


def wsl_distros() -> list[dict]:
    out = []
    try:
        lxss = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss")
    except OSError:
        return out
    i = 0
    while True:
        try:
            guid = winreg.EnumKey(lxss, i)
        except OSError:
            break
        i += 1
        try:
            with winreg.OpenKey(lxss, guid) as k:
                name = winreg.QueryValueEx(k, "DistributionName")[0]
                base = winreg.QueryValueEx(k, "BasePath")[0]
        except OSError:
            continue
        base = base.removeprefix("\\\\?\\")
        vhdx = os.path.join(base, "ext4.vhdx")
        out.append({"name": name, "base": base, "vhdx": vhdx if os.path.exists(vhdx) else None})
    return out


def covered_paths() -> list[str]:
    paths = [d["base"] for d in wsl_distros()]
    paths += [str(LOCAL / "Docker"), str(HOME / "VirtualBox VMs")]
    return paths


def scan() -> list[Finding]:
    out: list[Finding] = []
    for d in wsl_distros():
        if d["vhdx"]:
            size, mtime = file_size(d["vhdx"]), os.path.getmtime(d["vhdx"])
        else:
            size, _, mtime, _ = dir_size(d["base"], budget_s=30)
        docker = d["name"].startswith("docker-desktop")
        out.append(Finding(
            scanner="containers", category="wsl", title=f"WSL distro: {d['name']}",
            path=d["vhdx"] or d["base"], size=size, mtime=mtime, risk="high",
            suggestion=("Docker Desktop's internal distro. Shrink with 'docker system prune' then compact the vhdx; "
                        "don't unregister directly." if docker else
                        "Virtual disks only grow. If unused: 'wsl --export' as a backup, then 'wsl --unregister'. "
                        "If used: free space inside, then compact (wsl --manage <name> --set-sparse true, or Optimize-VHD)."),
            command=None if docker else f"wsl --unregister {d['name']}",
            evidence={"base_path": d["base"], "last_modified_days_ago": age_days(mtime)},
        ))

    # Docker Desktop data disks (location varies by version)
    for pat in [r"%LOCALAPPDATA%\Docker\wsl\disk\*.vhdx", r"%LOCALAPPDATA%\Docker\wsl\data\*.vhdx",
                r"%LOCALAPPDATA%\Docker\wsl\main\*.vhdx"]:
        for vhdx in glob.glob(os.path.expandvars(pat)):
            out.append(Finding(
                scanner="containers", category="docker", title=f"Docker Desktop disk: {os.path.basename(vhdx)}",
                path=vhdx, size=file_size(vhdx), mtime=os.path.getmtime(vhdx), risk="high",
                suggestion="Holds all images, containers, volumes and build cache. Prune inside Docker, "
                           "then compact the disk (Docker Desktop > Troubleshoot > Clean / Purge data).",
                command="docker system df ; docker system prune -a",
                evidence=_docker_df(),
            ))

    vbox = HOME / "VirtualBox VMs"
    if vbox.is_dir():
        for vm in vbox.iterdir():
            if vm.is_dir():
                size, _, mtime, _ = dir_size(vm, budget_s=30)
                out.append(Finding(scanner="containers", category="vm", title=f"VirtualBox VM: {vm.name}",
                                   path=str(vm), size=size, mtime=mtime, risk="high",
                                   suggestion="Remove from VirtualBox Manager (Remove > Delete all files) if unused.",
                                   evidence={"last_modified_days_ago": age_days(mtime)}))
    return out


def _docker_df() -> dict:
    txt = run(["docker", "system", "df"], timeout=20)
    return {"docker_system_df": txt.strip()} if txt.strip() else {"docker_system_df": "docker not running"}
