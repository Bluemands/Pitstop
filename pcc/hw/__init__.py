"""Hardware monitoring (the Tune section): sensors, fan headers, inventory.

Read-only. PC Cleaner never sets a fan speed or talks to a USB cooler: fan control stays with
FanControl / the BIOS / the cooler's own software. See docs/HARDWARE.md.

  lhm        reads sensors through LibreHardwareMonitorLib (pythonnet), plus safe limits per sensor
  fancontrol reads FanControl's active configuration (which header follows which curve)
  live       the elevated background monitor and the latest-reading file the UI polls
  info       static inventory and the fan-header table
"""
