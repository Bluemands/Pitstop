"""Synthetic sensor snapshots in LibreHardwareMonitor's shape, so tests run on any machine."""


def sensor(sid, type_, name, value, hidden=False, **kw):
    return {"id": sid, "type": type_, "name": name, "value": value, "min": value, "max": value,
            "hidden": hidden, **kw}


def snapshot(admin=True, cpu_temp=60.0, fan2=600.0, pump=4300.0, gpu_temp=50.0, nvme=40.0):
    board = {"id": "/lpc/it8689e/0", "type": "SuperIO", "name": "ITE IT8689E", "parent": "/motherboard", "sub": [],
             "sensors": [
                 sensor("/lpc/it8689e/0/fan/0", "Fan", "Fan #1", 0.0),
                 sensor("/lpc/it8689e/0/fan/1", "Fan", "Fan #2", fan2),
                 sensor("/lpc/it8689e/0/control/1", "Control", "Fan #2", 30.0),
                 sensor("/lpc/it8689e/0/fan/2", "Fan", "Fan #3", 550.0),
                 sensor("/lpc/it8689e/0/fan/4", "Fan", "Fan #5", pump),
                 sensor("/lpc/it8689e/0/temperature/2", "Temperature", "Temperature #3", 50.0),
             ]}
    return {"ts": 1000.0, "admin": admin, "source": "LibreHardwareMonitor test", "hardware": [
        {"id": "/motherboard", "type": "Motherboard", "name": "Test Board", "parent": None, "sensors": [],
         "sub": [board]},
        {"id": "/intelcpu/0", "type": "Cpu", "name": "Test CPU", "parent": None, "sub": [], "sensors": [
            sensor("/intelcpu/0/load/0", "Load", "CPU Total", 10.0),
            sensor("/intelcpu/0/temperature/0", "Temperature", "Core Max", cpu_temp),
            sensor("/intelcpu/0/temperature/1", "Temperature", "Core #1", cpu_temp),
            sensor("/intelcpu/0/temperature/9", "Temperature", "Core #1 Distance to TjMax", 105.0 - cpu_temp,
                   hidden=True),
            sensor("/intelcpu/0/temperature/22", "Temperature", "CPU Package", cpu_temp),
            sensor("/intelcpu/0/power/0", "Power", "CPU Package", 50.0),
        ]},
        {"id": "/gpu-nvidia/0", "type": "GpuNvidia", "name": "Test GPU", "parent": None, "sub": [], "sensors": [
            sensor("/gpu-nvidia/0/temperature/0", "Temperature", "GPU Core", gpu_temp),
            sensor("/gpu-nvidia/0/fan/1", "Fan", "GPU Fan 1", 900.0),
            sensor("/gpu-nvidia/0/control/1", "Control", "GPU Fan 1", 30.0),
            sensor("/gpu-nvidia/0/power/0", "Power", "GPU Package", 100.0),
        ]},
        {"id": "/nvme/0", "type": "Storage", "name": "Test SSD", "parent": None, "sub": [], "sensors": [
            sensor("/nvme/0/temperature/0", "Temperature", "Temperature", nvme),
            sensor("/nvme/0/temperature/10", "Temperature", "Warning Temperature", 82.0, hidden=True),
            sensor("/nvme/0/temperature/11", "Temperature", "Critical Temperature", 85.0, hidden=True),
        ]},
    ]}
