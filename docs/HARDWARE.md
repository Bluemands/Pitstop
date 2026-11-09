# Tune: sensors, logs and fans

An HWiNFO-style view of what the hardware is doing, plus sensor logs that Claude can read and answer questions
about. The first job it was built for: **make fans quiet on light work without letting anything run hot.**

The model is the same as the rest of Pitstop: the tool measures, Claude and you decide, and nothing changes
without your OK.

## What it never does

- **It never sets a fan speed.** FanControl, the BIOS or your cooler's own software stay in charge. Pitstop
  reads their configuration and proposes changes, which you make yourself.
- **It never talks to USB coolers, fan hubs or power supplies.** Two programs driving the same USB device can
  garble its commands. LibreHardwareMonitor's Controller and PSU groups stay disabled.
- **No overclocking, undervolting, or power and voltage changes, and no BIOS changes.**
- **It never loads the WinRing0 driver.** Defender flags it as vulnerable. Full sensor access uses the signed
  **PawnIO** driver.

## Sensors

Pitstop reads sensors through [LibreHardwareMonitorLib](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
(MPL-2.0), loaded with `pythonnet` on .NET Framework 4.8. `python -m pitstop hw setup` fetches it from NuGet into
`vendor\lhm`, or it uses the copy FanControl ships if FanControl is installed.

| Without admin | With admin (through PawnIO) adds |
|---|---|
| GPUs (temperatures including hot spot and memory junction, fans, power, load) | CPU package and core temperatures, clocks, package power |
| NVMe/SSD temperatures and their own warning/critical limits | The motherboard's fan/temperature chip: every fan header's RPM and %, board temperatures, voltages |
| CPU load, RAM use | RAM temperatures (DDR5 SPD) |

- **Background reader.** Full readings come from an elevated background reader, which takes one UAC prompt. It
  writes the latest reading to `data\hw-live.json` every second and stops by itself 2 minutes after nothing asks
  for readings.
- **Separate from the database on purpose.** The UI reloads itself whenever the main database changes, so a
  write every second would make it reload constantly.

### Limits

Each temperature gets a *warn* level (worth a look) and a *crit* level (at or near where the part protects
itself):

| Part | Warn | Crit | Source |
|---|---|---|---|
| CPU | TjMax − 15 °C | TjMax − 5 °C | TjMax from the CPU itself (LHM's distance-to-TjMax sensors), else 100 °C |
| NVIDIA GPU core / hot spot / memory junction | 83 / 95 / 100 °C | 90 / 105 / 106 °C | Rules of thumb (`lhm.GPU_LIMITS`) |
| NVMe | its own warning temperature, at most 70 °C | its own critical temperature | The drive |
| RAM | 70 °C | the DIMM's critical-high limit, else 85 °C | The DIMM |

## Logs

- **Started when you need one.** A log records every sensor once a second until you stop it (4 hours at most),
  plus the top CPU and GPU programs every 5 seconds.
- **Marks.** While a log runs, "Mark: noisy now" flags the moment you mean.
- **"That was loud".** While the background reader runs, it keeps the last 5 minutes in memory. "That was loud:
  save the last 5 minutes" turns them into a log, so what led up to the noise is caught too.
- **Storage.** Logs live in `data\hw.sqlite`. Each second is one row holding every value as packed float32, so
  4 hours is under 10 MB.
- **Reports** (`hw report`, MCP `hw_report`) are what Claude reads first:
  - min, average, 95th percentile and max per sensor, and time over each limit
  - every **fan ramp**: a fan rising ≥150 RPM and ≥20 % within 15 s. Readings are median-filtered over 5
    samples, because tach lines misread for single seconds. Each ramp comes with the temperatures that rose in
    the 30 s before it, power and load at the peak, and the busiest programs.
  - your marks, with what the fans and programs were doing at that moment
  - the busiest programs over the whole log
  - FanControl's curves when the log started, for before/after comparisons.
- **`hw query`** returns a downsampled slice for chosen sensors and times. **`hw export`** writes CSV.

## Balancing fans

1. **Check the basics first.** A GPU that doesn't idle (stuck at full clocks: often NVIDIA's "Prefer maximum
   performance", or overlays) or a busy background program causes noise that no curve fixes.
2. **Log a normal session** and mark the noisy moments.
3. **Read the report.** For each loud moment, find which sensor the fan followed, which part heated up, and which
   program was running.
4. **Fix the cause before the curve.** A runaway process is a cleanup job. A GPU drawing more than the game needs
   is a frame-cap or undervolt job.
5. **Propose curve changes** as numbers you type into FanControl or your cooler's software. The rules:
   - only the low and middle of a curve get quieter
   - every curve still reaches 100 % by a safe temperature
   - radiator fans follow coolant temperature (it changes over minutes, so they stay steady)
   - case exhaust follows the hotter of CPU and GPU
   - pumps are never put on a curve.
6. **Log the same activity again** and compare the reports.

## Fan software

Pitstop reads FanControl's active configuration (which header follows which curve, and on which sensor)
and recognises other fan and RGB software that's running (iCUE, NZXT CAM, Armoury Crate, GIGABYTE Control
Center, MSI Center, Argus Monitor, SignalRGB, L-Connect, HWiNFO…). Other software is shown, never controlled.

## Not built yet

- **`fans status`, `fans propose`, `fans validate`:** curve proposals checked against the limits, and a guided
  load test to validate them.
- **Optional writing of FanControl's config:** backed up, journalled, and restored automatically if a validation
  run breaks a limit.
- **Charts on the Logs page.**
