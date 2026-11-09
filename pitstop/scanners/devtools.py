"""Known developer caches, SDKs, toolchains and model stores.

Each rule is a glob (env vars and ~ expanded). Caches are regenerable and low
risk; SDKs/toolchains are medium (something may still need that version);
models are re-downloadable but often huge.
"""
from __future__ import annotations

import glob
import os
import re
from collections import defaultdict

from ..store import Finding
from ..util import SYSDRIVE, age_days, dir_size

# (label, glob, kind, command-or-None, note)
RULES: list[tuple[str, str, str, str | None, str]] = [
    # --- package manager caches
    ("npm cache", r"%LOCALAPPDATA%\npm-cache", "cache", "npm cache clean --force", ""),
    ("Yarn cache", r"%LOCALAPPDATA%\Yarn\Cache", "cache", "yarn cache clean", ""),
    ("pnpm store", r"%LOCALAPPDATA%\pnpm\store", "cache", "pnpm store prune", "Prune only removes packages no project references."),
    ("Bun cache", r"~\.bun\install\cache", "cache", "bun pm cache rm", ""),
    ("npm global packages", r"%APPDATA%\npm", "sdk", "npm ls -g --depth=0", "Globally installed CLIs. Review with 'npm ls -g' and 'npm uninstall -g <pkg>'."),
    ("pip cache", r"%LOCALAPPDATA%\pip\cache", "cache", "pip cache purge", ""),
    ("uv cache", r"%LOCALAPPDATA%\uv\cache", "cache", "uv cache clean", ""),
    ("uv-managed Pythons / tools", r"%APPDATA%\uv", "sdk", "uv python list --only-installed ; uv tool list", ""),
    ("Playwright MCP browsers", r"%LOCALAPPDATA%\ms-playwright-mcp", "cache", None, ""),
    ("Poetry cache", r"%LOCALAPPDATA%\pypoetry\Cache", "cache", None, ""),
    ("conda package cache", r"~\*conda*\pkgs", "cache", "conda clean --all", ""),
    ("NuGet packages", r"~\.nuget\packages", "cache", "dotnet nuget locals all --clear", ""),
    ("NuGet HTTP cache", r"%LOCALAPPDATA%\NuGet\v3-cache", "cache", "dotnet nuget locals http-cache --clear", ""),
    ("Gradle caches", r"~\.gradle\caches", "cache", None, "Safe to delete; rebuilt on next build."),
    ("Gradle wrapper dists", r"~\.gradle\wrapper\dists", "cache", None, "Old Gradle versions downloaded by projects."),
    ("Maven repository", r"~\.m2\repository", "cache", None, ""),
    ("Cargo registry", r"~\.cargo\registry", "cache", "cargo cache --autoclean (cargo-cache)", ""),
    ("Go module cache", r"~\go\pkg\mod", "cache", "go clean -modcache", ""),
    ("Go build cache", r"%LOCALAPPDATA%\go-build", "cache", "go clean -cache", ""),
    ("Dart/Flutter pub cache", r"%LOCALAPPDATA%\Pub\Cache", "cache", "dart pub cache clean", ""),
    ("Composer cache", r"%LOCALAPPDATA%\Composer", "cache", "composer clear-cache", ""),
    ("Electron download cache", r"%LOCALAPPDATA%\electron\Cache", "cache", None, ""),
    ("electron-builder cache", r"%LOCALAPPDATA%\electron-builder\Cache", "cache", None, ""),
    ("node-gyp headers", r"%LOCALAPPDATA%\node-gyp\Cache", "cache", None, ""),
    ("Playwright browsers", r"%LOCALAPPDATA%\ms-playwright", "cache", "npx playwright uninstall --all", "Old browser builds pile up per Playwright version."),
    ("Puppeteer browsers", r"~\.cache\puppeteer", "cache", None, ""),
    ("Cypress cache", r"%LOCALAPPDATA%\Cypress\Cache", "cache", "npx cypress cache prune", ""),
    ("TypeScript cache", r"%LOCALAPPDATA%\Microsoft\TypeScript", "cache", None, ""),
    ("VS Code cached VSIXs", r"%APPDATA%\Code\CachedExtensionVSIXs", "cache", None, ""),
    ("VS Code cached data", r"%APPDATA%\Code\CachedData", "cache", None, "One folder per past VS Code version."),
    ("VS Code workspace storage", r"%APPDATA%\Code\User\workspaceStorage", "cache", None, "Per-workspace state, including for folders that no longer exist."),
    ("Cursor cached data", r"%APPDATA%\Cursor\CachedData", "cache", None, ""),
    ("Visual Studio package cache", r"%ProgramData%\Microsoft\VisualStudio\Packages", "cache", None, "Used for repair/modify. Removable via VS Installer settings."),
    ("Package Cache (installers)", r"%ProgramData%\Package Cache", "cache", None, "Cached installers used for repair/uninstall. Do NOT delete wholesale; uninstalls can break."),
    ("Unity cache", r"%LOCALAPPDATA%\Unity\cache", "cache", None, ""),
    ("Unity asset store downloads", r"%APPDATA%\Unity\Asset Store-5.x", "cache", None, ""),
    ("JetBrains caches", r"%LOCALAPPDATA%\JetBrains\*", "cache", None, "One folder per IDE version; old versions are dead weight."),
    ("Android emulator images (AVDs)", r"~\.android\avd", "vm", None, "Delete unused devices in Android Studio's Device Manager."),
    ("Docker build cache (Windows)", r"%LOCALAPPDATA%\Docker\buildx", "cache", "docker builder prune", ""),

    # --- SDKs / toolchains / runtimes (one finding per version folder)
    ("Android SDK", r"%LOCALAPPDATA%\Android\Sdk", "sdk", None, "Use SDK Manager to remove old platforms/system images."),
    ("Android SDK system images", r"%LOCALAPPDATA%\Android\Sdk\system-images\*\*", "sdk", None, ""),
    ("Flutter SDK", rf"{SYSDRIVE}\src\flutter", "sdk", None, ""),
    ("Rust toolchain", r"~\.rustup\toolchains\*", "sdk", "rustup toolchain uninstall <name>", ""),
    ("Python install", r"%LOCALAPPDATA%\Programs\Python\Python*", "sdk", None, "Check nothing (venvs, PATH, py launcher) still uses this version."),
    ("Python install", rf"{SYSDRIVE}\Python*", "sdk", None, ""),
    ("pyenv version", r"~\.pyenv\pyenv-win\versions\*", "sdk", "pyenv uninstall <version>", ""),
    ("Conda env", r"~\*conda*\envs\*", "sdk", "conda env remove -n <name>", ""),
    ("nvm Node version", r"%APPDATA%\nvm\v*", "sdk", "nvm uninstall <version>", ""),
    ("fnm Node version", r"%APPDATA%\fnm\node-versions\*", "sdk", "fnm uninstall <version>", ""),
    (".NET SDK", r"%ProgramFiles%\dotnet\sdk\*", "sdk", None, "Remove old SDKs via Apps & Features or the dotnet-core-uninstall tool."),
    (".NET SDK (user)", r"~\.dotnet\sdk\*", "sdk", None, ""),
    ("JDK", r"%ProgramFiles%\Java\*", "sdk", None, ""),
    ("JDK", r"%ProgramFiles%\Eclipse Adoptium\*", "sdk", None, ""),
    ("JDK", r"%ProgramFiles%\Microsoft\jdk-*", "sdk", None, ""),
    ("JDK", r"%ProgramFiles%\Zulu\*", "sdk", None, ""),
    ("JDK (user)", r"~\.jdks\*", "sdk", None, "JDKs downloaded by IntelliJ."),
    ("CUDA toolkit", r"%ProgramFiles%\NVIDIA GPU Computing Toolkit\CUDA\v*", "sdk", None, "Uninstall via Apps & Features (several components per version)."),
    ("NVIDIA Nsight", r"%ProgramFiles%\NVIDIA Corporation\Nsight*", "sdk", None, ""),
    ("Windows SDK", r"%ProgramFiles(x86)%\Windows Kits\10", "sdk", None, ""),
    ("Emscripten SDK", r"~\emsdk", "sdk", None, ""),
    ("Strawberry Perl", rf"{SYSDRIVE}\Strawberry", "sdk", None, ""),
    ("MSYS2", rf"{SYSDRIVE}\msys64", "sdk", None, ""),

    # --- AI / ML model stores
    ("Hugging Face cache", r"~\.cache\huggingface", "models", "huggingface-cli delete-cache", ""),
    ("PyTorch hub cache", r"~\.cache\torch", "models", None, ""),
    ("Ollama models", r"~\.ollama\models", "models", "ollama rm <model>", ""),
    ("LM Studio models", r"~\.lmstudio\models", "models", None, ""),
    ("LM Studio models (old)", r"~\.cache\lm-studio\models", "models", None, ""),
    ("GPT4All models", r"%LOCALAPPDATA%\nomic.ai\GPT4All", "models", None, ""),
    ("Whisper models", r"~\.cache\whisper", "models", None, ""),
    ("pip/torch wheels in temp", r"%LOCALAPPDATA%\Temp\pip-*", "cache", None, ""),
]

RISK = {"cache": "low", "sdk": "medium", "models": "medium", "vm": "medium"}
MIN_BYTES = 1 * 1024 * 1024


def _expand(pattern: str) -> list[str]:
    p = os.path.expanduser(os.path.expandvars(pattern))
    return sorted(x for x in glob.glob(p) if os.path.isdir(x))


def covered_paths() -> list[str]:
    """Paths this scanner reports, so the orphan scanner doesn't double count."""
    return [p for _, pat, *_ in RULES for p in _expand(pat)]


def scan() -> list[Finding]:
    out: list[Finding] = []
    for label, pattern, kind, cmd, note in RULES:
        for path in _expand(pattern):
            size, files, newest, complete = dir_size(path, budget_s=60)
            if size < MIN_BYTES:
                continue
            name = os.path.basename(path)
            out.append(Finding(
                scanner="devtools", category=kind,
                title=f"{label}: {name}" if "*" in pattern else label,
                path=path, size=size, mtime=newest or None, risk=RISK[kind],
                suggestion=note or ("Regenerable cache; safe to clear." if kind == "cache" else ""),
                command=cmd.replace("<name>", name).replace("<version>", name.lstrip("v")) if cmd else None,
                evidence={"files": files, "last_modified_days_ago": age_days(newest), "size_complete": complete},
            ))
    out += _vscode_old_extensions()
    return out


def _vscode_old_extensions() -> list[Finding]:
    """Superseded versions of the same VS Code extension that weren't cleaned up."""
    root = os.path.expanduser(r"~\.vscode\extensions")
    if not os.path.isdir(root):
        return []
    groups: dict[str, list[tuple[tuple, str]]] = defaultdict(list)
    for name in os.listdir(root):
        m = re.match(r"(.+?)-(\d+(?:\.\d+)+)(?:-.*)?$", name)
        if m:
            groups[m.group(1)].append((tuple(int(x) for x in m.group(2).split(".")), name))
    out = []
    for ext, versions in groups.items():
        if len(versions) < 2:
            continue
        versions.sort()
        for _, name in versions[:-1]:
            path = os.path.join(root, name)
            size, _, newest, _ = dir_size(path, budget_s=10)
            out.append(Finding(
                scanner="devtools", category="cache", title=f"Old VS Code extension version: {name}",
                path=path, size=size, mtime=newest or None, risk="low",
                suggestion=f"Superseded by {versions[-1][1]}. VS Code normally removes these; leftovers are safe to delete when VS Code is closed.",
            ))
    return out
