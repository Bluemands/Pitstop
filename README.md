# PC Cleaner

A Windows maintenance tool built to be used **with Claude**. PC Cleaner does the inventory and measuring; you
and Claude decide what to do about it, one confirmed batch at a time. Nothing is removed or changed without
your explicit OK, and almost everything can be undone.

| Section | What it does |
|---|---|
| **Clean** | Finds forgotten space: folders left by uninstalled programs, old SDKs and toolchains, package and AI-model caches, WSL/Docker disks, games you haven't played in years, installers in Downloads, dead PATH entries. Full uninstalls that also clean up what a program's own uninstaller leaves behind. |
| **Health** | App updates (via winget), startup items, driver updates offered by Windows Update, devices with problems, runtimes past end of support, broken startup entries. |
| **Tune** | An HWiNFO-style live view of temperatures, fans, power and load; sensor logs you can mark ("this is the noise"); and reports that explain every time a fan sped up: which part heated, which program was running. |

It's built for people who'd rather understand their PC than click a "Fix 61 issues!" button. There are no
scores, no red gauges and no upsells.

## How it keeps your PC safe

- **Scans are read-only.** Every action is a dry run until you confirm it.
- **Quarantine, not delete.** Removed folders are moved aside on the same drive and can be restored. Space is
  freed only when you purge.
- **Everything is journalled** and can be reversed with `restore <id>`.
- **A hard deny-list.** Windows, Program Files roots, profile folders and drive roots can never be removed.
- **Owning tools first.** Programs uninstall through their own uninstallers; caches are cleaned by their own
  tools (npm, pip, Docker, WSL).
- **No registry "cleaning".** No third-party driver databases.
- **Tune never sets a fan speed, never talks to USB coolers, and never changes BIOS, voltage or power
  settings.** Fan changes are proposals you make yourself in FanControl or your cooler's software.

More in [docs/DESIGN.md](docs/DESIGN.md) and [docs/HARDWARE.md](docs/HARDWARE.md).

## Requirements

- Windows 10 or 11
- [Python](https://www.python.org/downloads/) 3.12 or 3.13
- [Claude Code](https://claude.com/claude-code), to have Claude drive it. The UI and CLI also work on their own.
- For the Tune section, all optional:
  - .NET Framework 4.8. It's already part of Windows 10 1903+ and Windows 11.
  - The [PawnIO](https://pawnio.eu) driver, for CPU temperatures and motherboard fans (`winget install namazso.PawnIO`).
    Without it you still get GPU, drive and CPU-load readings.
  - [FanControl](https://getfancontrol.com), if you want PC Cleaner to read your fan curves.

## Install

```powershell
git clone https://github.com/<you>/PC-Cleaner.git
cd PC-Cleaner
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pcc setup
```

`setup` does three things:
- writes `.mcp.json`, so Claude Code can use PC Cleaner's tools
- adds a **PC Cleaner** shortcut to the Desktop and Start menu
- sets up sensors: it uses FanControl's copy of LibreHardwareMonitor if FanControl is installed, otherwise it
  downloads a pinned, hash-checked copy from nuget.org.

## Use

**With Claude:** open Claude Code in the project folder and ask, for example:

- "Let's do a cleanup session." Claude scans, shows the biggest items in small batches and asks about each.
- "Clean up what I marked." Claude works through the items you marked in the UI.
- "My fans are loud when they shouldn't be." Claude reads your sensors and fan setup, asks you to log a session,
  and explains each fan ramp.

**The UI:** open PC Cleaner from the Start menu. Browse findings, mark them (remove / keep / review later),
watch live sensors, and start or mark sensor logs. The UI only records decisions; Claude acts on them, and the
window updates live as it does.

**The command line:** everything is also available directly. Add `--json` for machine-readable output, or
`--elevate` to run one command as admin.

```powershell
.venv\Scripts\python.exe -m pcc summary
.venv\Scripts\python.exe -m pcc scan
.venv\Scripts\python.exe -m pcc list --category cache
.venv\Scripts\python.exe -m pcc quarantine <id>            # dry run; add --yes to do it
.venv\Scripts\python.exe -m pcc hw now
.venv\Scripts\python.exe -m pcc hw log start --label "Gaming"
.venv\Scripts\python.exe -m pcc hw report
```

## Tests

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

## Disclaimer

PC Cleaner is provided as is, without warranty (see [LICENSE](LICENSE)). It's careful by design: dry runs,
quarantine, a journal and a deny-list. But it changes real systems when you tell it to, and the decisions are
yours. Keep backups of anything you care about. The hardware features only read sensors. Advice about fan
curves is advice: you own your hardware and the changes you make to it.

## Credits

- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) (MPL-2.0) for sensor readings
- [pythonnet](https://github.com/pythonnet/pythonnet), [psutil](https://github.com/giampaolo/psutil), and the
  [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [PawnIO](https://pawnio.eu) by namazso for safe low-level hardware access

## Licence

MIT. See [LICENSE](LICENSE).
