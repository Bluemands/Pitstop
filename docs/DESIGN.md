# PC Cleaner: design

A Windows maintenance tool designed to be driven *with* Claude. The tool does the fast, mechanical work:
inventory, sizing, cross-referencing and measuring. Claude does the judgement ("is this folder from something
you still use?") and talks it through with you. Nothing is removed or changed without an explicit decision.

The app has three sections:

| Section | What it covers | What you record |
|---|---|---|
| **Clean** | Space: leftover folders, programs, caches, SDKs, AI models, WSL/Docker disks, games, downloads, dead PATH entries | Remove / keep / review |
| **Health** | Problems: app updates, startup items, driver updates, device problems, out-of-support runtimes, broken startup entries | Update / disable / fix |
| **Tune** | Measurement: live sensors, sensor logs, fan headers (see [HARDWARE.md](HARDWARE.md)) | Logs and marks; fan changes are proposals |

## Why existing tools fall short

Disk Cleanup and Storage Sense only know about Windows' own junk. Uninstallers only know about what's still
registered. "PC optimiser" suites flag harmless things as critical to sell an upgrade. The space that actually
piles up is:

- folders left behind by programs that were uninstalled (AppData, ProgramData, Program Files)
- whole programs that were never registered, or whose uninstall entry is gone
- toolchains, SDKs and runtimes installed for one project (JDKs, CUDA, Python, Node, Android SDK, .NET SDKs)
- package and model caches (npm, pip, NuGet, Gradle, HuggingFace, Ollama…)
- WSL distros and Docker virtual disks, which only ever grow
- games not played in years
- environment variables, PATH entries and uninstall registry keys pointing at nothing

## Architecture

```
pcc/
  scanners/      read-only; each returns findings (path, size, evidence, risk, suggestion, command)
  diskindex      one walk of a drive -> per-folder sizes in SQLite, then instant drill-down
  footprint      read-only snapshot of everything a program owns, and the post-uninstall comparison
  actions        the only module that changes the machine; every change goes through the journal
  elevate        UAC: runs an uninstaller or a pcc command as admin and waits for it
  hw/            Tune: sensors, fan headers, logs (read-only)
  store          SQLite: findings, decisions, disk index, journal
  ui/            local web UI (127.0.0.1 only, per-launch token); reads and records decisions
  __main__       CLI: `python -m pcc ...` (--json for machine-readable output)
  mcp_server     the same capabilities as MCP tools, so Claude can call them directly
```

## Safety model

1. **Read-only by default.** Scans never modify anything, and every action is a dry run unless confirmed.
2. **Quarantine, not delete.** Removing a folder moves it into `<drive>:\PCC-Quarantine\` on the same volume, so
   it's instant and restorable. Space is freed only on `purge`.
3. **Journal everything.** Every action records the previous state, and `restore` reverses it.
4. **A hard deny-list.** Windows, System32, drive roots, profile roots, Program Files roots and similar are
   refused outright.
5. **Owning tools first.** Uninstalls and cache cleans go through the tool that owns them (the program's own
   uninstaller, `winget`, `npm cache clean`, `docker system prune`, `wsl --unregister`).
6. **No "registry cleaning".** Orphan registry keys take almost no space, and deleting them is how PCs get
   broken. Only broken uninstall entries and dead startup entries are removed, and only after `reg export`.
7. **No drivers from third-party databases.** Driver updates are the ones Windows Update offers; the user ticks
   them in Settings, after a restore point.
8. **The UI never acts.** It records decisions. Claude carries them out one confirmed batch at a time, and the UI
   window updates live as each item is done.

## Known limits

- **"Last used" is a hint, not a fact.** NTFS last-access is disabled on volumes over 128 GB. The signals
  available are folder modification time, UserAssist (GUI launches, which Windows sometimes stops updating),
  Steam and Epic last-played dates, and Prefetch (admin only).
- **Matching folders to programs is heuristic.** Name matching gets the easy ones; ambiguous ones are exactly
  what Claude is for.
- **Admin adds coverage.** Other users' folders, Prefetch, machine-wide PATH edits, some system folders, and the
  full hardware sensors need elevation. From an unelevated shell, `--elevate` runs one command as admin (one UAC
  prompt).
