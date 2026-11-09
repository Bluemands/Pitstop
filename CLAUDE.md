# PC Cleaner

Windows cleanup tool built to be driven with Claude. The tool inventories; Claude
and the user decide together. See docs/DESIGN.md for the design and safety model, and
docs/HARDWARE.md for the Tune section (sensors, logs, fans).

Run everything with the project venv: `.venv\Scripts\python.exe -m pcc <cmd>` (add `--json` for
machine-readable output). The same capabilities are exposed as the `pc-cleaner` MCP server (`.mcp.json`, written
by `pcc setup`). Tests: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`. This is a public repo:
anything specific to one PC goes in `CLAUDE.local.md` or `notes\`, both gitignored.

The user browses and marks findings in the local UI: the "PC Cleaner" Desktop/Start menu shortcut
(`pythonw -m pcc.ui.app`: no console, exits when its window closes; `python -m pcc shortcut` recreates it),
or `python -m pcc ui` from a terminal (pcc/ui/). The UI only reads and
records decisions; it never deletes. When the user says "clean up what I marked", list `status=remove`
findings and work through them per the playbook below. Notes the user typed in the UI are in each finding's `note`.

## Cleanup session playbook

1. `summary` — drive free space, size per category, how stale each scan is. Re-run `scan` if older than a day.
2. If the user wants the "where did it all go" view: `index C:\` (a minute or two per TB), then `du <path>` to drill.
3. Walk categories biggest first. For each batch, show the user a short table (title, size, last modified /
   played / launched, why it's flagged) and ask. Don't dump hundreds of rows.
4. Record answers with `decide keep|remove|review <ids>` so the next session doesn't re-ask.
5. Act on `remove` items, **one confirmed batch at a time**:
   - Programs: `uninstall <id>` (dry run lists its whole footprint), then `uninstall <id> --yes`. That
     snapshots everything the program owns, runs its own uninstaller elevated (the user clicks through
     UAC and its window), and records what it left as `uninstall-leftover` findings. Show those to the
     user, then quarantine the folders and shortcuts (`--elevate` for Program Files / ProgramData), and
     `leftover-remove` tasks, startup and right-click entries. If it asked for a restart, run
     `leftovers <journal_id>` afterwards. Vendor registry keys are reported, not removed.
   - Games: run the finding's `command` (store uninstall), then re-scan `orphans`.
   - App updates (`app-update`, marked "to update" in the UI): `update <ids> --yes`, a few at a time.
     Installers may show UAC prompts, so the user must be at the PC. Ask them to close the apps first.
     Updates marked "Don't update" (status `keep`): offer `update-pin <ids> --yes` so winget stops offering them.
     Do major-version jumps one at a time. Visual Studio and Office are refused: they use their own updaters.
   - `eol-runtime` findings are for information. Never uninstall a runtime without checking what uses it
     (Python venvs, .NET apps).
   - Startup items marked "Disable at startup" (`startup`): `startup-disable <ids> --yes`.
     - Apps get Task Manager's off switch, tasks are disabled, and services are set to Manual.
     - All-users entries and services need `--elevate`.
     - Security software is refused. Warn before hardware (fan, RGB, GPU) items.
     - Each change is undone with `restore <journal_id>`.
   - Broken startup entries (`startup-broken`): `startup-fix <ids> --yes`. Everything is backed up first;
     use `--elevate` for HKLM switches and dead services.
   - Driver updates (`driver-update`): PC Cleaner never installs drivers.
     1. `restore-point --yes --elevate` (Windows allows one per 24 h).
     2. `open optional-updates`, and tell the user exactly which boxes to tick (device and version).
     3. After they've installed, `scan drivers`. Installed ones drop off, and the window updates live.
   - Device problems (`device-problem`): help the user find the driver. The finding names the vendor and
     motherboard; `open device-manager` shows details. The user installs it; then `scan drivers`.
   - Old driver copies (`driver-superseded`): `driver-remove <ids> --yes --elevate`. Each is exported to
     quarantine, then pnputil removes it, refusing if a device uses it. It's freed on purge, and
     `restore` re-adds it.
   - Caches with a `command` (npm, pip, docker, conda…): prefer the command over deleting files.
   - Everything else: `quarantine <id> --yes`. Tell the user it frees nothing until `purge`.
   - WSL / Docker disks: never delete the vhdx directly. Prune inside, then compact; `wsl --unregister`
     only after `wsl --export` if the user might want it back.
   - After running a finding's own `command` yourself (store uninstall, cache clean), record it with
     `decide done <ids>`. The pcc actions mark their findings done themselves. The UI window refreshes
     live from the database, so the user sees each item go as it's done.
6. Suggest `purge` once the user has used the PC for a while without missing anything.

## Tune: sensors and fans (see docs/HARDWARE.md)

- `hw now` (MCP `hw_now`): key readings per part, the fan-header table (RPM, %, and what controls each:
  FanControl curve, BIOS or GPU), and anything over its warn limit. `--all` for every sensor.
- Full readings need admin. `hw monitor --background` (or `hw_now(elevate=True)`, or "Read all sensors" in
  the UI) starts an elevated reader: one UAC prompt. It writes `data\hw-live.json` every second and stops
  2 minutes after the last request. Without it, only the GPU, drives and CPU load are read.
- `hw info`: inventory (board/BIOS, CPU, RAM, GPU driver, drive health, fan software running, FanControl config).
- `hw name <sensor_id> "<label>"`: name a header ("Top exhaust"). It's PC Cleaner's own label only.
  In Git Bash, prefix with `MSYS_NO_PATHCONV=1`, because ids start with `/`.
- Logs (`data\hw.sqlite`; MCP `hw_log`, `hw_report`, `hw_query`):
  - `hw log start --label "Gaming: WoW"` logs every second (4 h max) and starts the admin reader (UAC).
    `hw log mark "loud now"`, `hw log stop`, `hw log list`.
  - `hw log save` (UI: "That was loud") saves the reader's last 5 minutes as a log.
  - `hw report [id]` is the summary to read first: stats, each fan ramp with the temperatures that rose before
    it and the top CPU/GPU programs, the user's marks, limit breaches, busiest programs, and FanControl's curves
    at the start.
  - `hw query "CPU Package" "Fan #2" --session N --from 14:02 --to 14:10` gives a downsampled slice.
    `hw export N file.csv` writes a log to CSV.
- Fan balancing: explain the noise from a labelled log first. A busy background program is a cleanup job, not a
  fan job. Only then propose curve changes, as numbers the user types into FanControl or iCUE. Check them
  against the limits in docs/HARDWARE.md, and compare a new log afterwards.
- PC Cleaner never sets a fan speed and never talks to a USB cooler. Fan changes are proposals that the user
  makes in FanControl or iCUE.

## Rules

- Nothing destructive without the user's explicit OK for those specific items. Dry runs are the default.
- `unrecognised-root-folder` findings are often the user's own data. Ask, never assume.
- "Last launch" (UserAssist) is a weak hint: Windows sometimes stops updating it. Folder mtime and Steam
  last-played are more reliable. NTFS last-access is disabled on big volumes.
- No generic registry "cleaning". Only `broken-uninstall-entry` findings, which are exported to
  `data\backups\*.reg` before deletion.
- Machine PATH and HKLM edits need an elevated terminal. From Claude's unelevated shell, add `--elevate`
  to any command (one UAC prompt; the user must be at the PC). The MCP tools take `elevate=True`.

## Code layout

- `pcc/scanners/*` — read-only, each returns `Finding`s. Add a scanner by registering it in
  `scanners/__init__.py`; if it claims folders, expose `covered_paths()` and add it to `orphans.py`.
- `pcc/actions.py` — the only module that changes the machine; everything goes through `_journal`.
- `pcc/footprint.py` — read-only snapshot of everything a program owns (folders, shortcuts, services,
  drivers, tasks, startup, right-click handlers, vendor keys), and the post-uninstall comparison.
- `pcc/elevate.py` — UAC: runs an uninstaller or a `pcc` command as admin and waits for it.
- `pcc/util.py: protection_reason` — hard deny-list for quarantine.
- `pcc/hw/*` — Tune, read-only:
  - `lhm.py`: LibreHardwareMonitorLib via pythonnet (FanControl's copy, `vendor\lhm`, or `$PCC_LHM_DIR`), plus
    warn/crit limits per sensor. The Controller and PSU groups stay off.
  - `fancontrol.py`: FanControl's active config.
  - `live.py`: the elevated monitor, `hw-live.json`, and sensor names.
  - `info.py`: inventory, fan-header table and the compact report.
  - `logdb.py`: log sessions in `data\hw.sqlite`, kept apart from pcc.sqlite so the UI doesn't reload every
    second. Each sample is one row of packed float32 values.
  - `procs.py`: top CPU/GPU processes via psutil and PDH, in-process (no PowerShell spawning).
  - `report.py`: ramp detection, report, query and CSV export.
- `data/pcc.sqlite` — findings, decisions, disk index, journal (gitignored).
