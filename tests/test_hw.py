"""Tune: limits, fan-header classification, FanControl config parsing, log storage and reports.
No hardware, no admin and no FanControl needed."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pitstop.hw import fancontrol, info, logdb, report
from pitstop.hw.lhm import add_limits, all_sensors
from tests.helpers import snapshot


def by_name(snap, part_type, name):
    return next(s for h, s in all_sensors(snap) if h["type"] == part_type and s["name"] == name)


class Limits(unittest.TestCase):
    def test_cpu_limits_come_from_tjmax(self):
        snap = snapshot(cpu_temp=60.0)       # distance to TjMax 45 -> TjMax 105
        add_limits(snap)
        s = by_name(snap, "Cpu", "CPU Package")
        self.assertEqual((s["warn"], s["crit"]), (90, 100))
        self.assertEqual(snap["hardware"][1]["tjmax"], 105)

    def test_nvme_uses_its_own_limits_capped_at_70(self):
        snap = snapshot()
        add_limits(snap)
        s = by_name(snap, "Storage", "Temperature")
        self.assertEqual((s["warn"], s["crit"]), (70, 85))

    def test_gpu_core(self):
        snap = snapshot()
        add_limits(snap)
        s = by_name(snap, "GpuNvidia", "GPU Core")
        self.assertEqual((s["warn"], s["crit"]), (83, 90))


class FanControlConfig(unittest.TestCase):
    CONFIG = {"FanControl": {
        "Controls": [
            {"Identifier": "/lpc/nct6798d/control/1", "Enable": True, "SelectedFanCurve": {"Name": "Case"},
             "MinimumPercent": 0},
            {"Identifier": "/lpc/nct6798d/control/2", "Enable": False},
        ],
        "FanCurves": [
            {"Name": "Case", "SelectedTempSource": {"Identifier": "NVApiWrapper/0-GA102-A/sensor/0"},
             "Points": ["30,5", "70,40", "100,100"],
             "HysteresisConfig": {"HysteresisValueUp": 2, "ResponseTimeUp": 1}},
            {"Name": "Flat", "Percent": 40},
            {"Name": "Lin", "SelectedTempSource": {"Identifier": "/intelcpu/0/temperature/22"},
             "IdleTemperature": 40, "MinFanSpeed": 20, "LoadTemperature": 80, "MaxFanSpeed": 100},
            {"Name": "Mix", "SelectedFanCurves": [{"Name": "Case"}, {"Name": "Lin"}], "SelectedMixFunction": 1},
        ],
        "TemperatureSensors": []}}

    def read(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Main.json"
            p.write_text(json.dumps(self.CONFIG), encoding="utf-8")
            with mock.patch.object(fancontrol, "config_path", return_value=p), \
                    mock.patch.object(fancontrol, "running", return_value=True):
                return fancontrol.read()

    def test_norm_id_drops_chip_index(self):
        self.assertEqual(fancontrol.norm_id("/lpc/nct6798d/0/control/1"), "/lpc/nct6798d/control/1")
        self.assertEqual(fancontrol.norm_id("/gpu-nvidia/0/control/1"), "/gpu-nvidia/0/control/1")

    def test_curves_and_controls(self):
        fc = self.read()
        kinds = {c["name"]: c["kind"] for c in fc["curves"]}
        self.assertEqual(kinds, {"Case": "graph", "Flat": "flat", "Lin": "linear", "Mix": "mix"})
        case = next(c for c in fc["curves"] if c["name"] == "Case")
        self.assertEqual(case["points"], [[30.0, 5.0], [70.0, 40.0], [100.0, 100.0]])
        self.assertEqual(case["source_name"], "GPU temperature")
        ctl = fc["controls"]["/lpc/nct6798d/control/1"]
        self.assertTrue(ctl["enabled"])
        self.assertEqual(ctl["curve"], "Case")
        self.assertIsNone(fc["controls"]["/lpc/nct6798d/control/2"]["curve"])

    def test_fan_headers(self):
        fc = self.read()
        rows = {r["header"]: r for r in info.fan_headers(snapshot(), fc, {"/lpc/nct6798d/0/fan/1": "Top exhaust"})}
        self.assertIn("FanControl", rows["Fan #2"]["controlled_by"])
        self.assertEqual(rows["Fan #2"]["label"], "Top exhaust")
        self.assertEqual(rows["Fan #1"]["controlled_by"], "Nothing connected")
        self.assertEqual(rows["Fan #3"]["controlled_by"], "Motherboard (BIOS fan curve)")
        self.assertIsNotNone(rows["Fan #5"]["hint"])          # 4,300 RPM: a pump
        self.assertIn("Graphics card", rows["GPU Fan 1"]["controlled_by"])

    def test_missing_config(self):
        with mock.patch.object(fancontrol, "config_path", return_value=None), \
                mock.patch.object(fancontrol, "install_dir", return_value=None):
            self.assertEqual(fancontrol.read()["controls"], {})


class TempDB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(logdb, "DB", Path(self.dir.name) / "hw.sqlite")
        self.p.start()
        self.fc = mock.patch.object(fancontrol, "read", return_value={"controls": {}, "curves": []})
        self.fc.start()

    def tearDown(self):
        self.fc.stop()
        self.p.stop()
        import gc
        gc.collect()   # close sqlite connections before Windows deletes the folder
        self.dir.cleanup()

    def fill(self, sid, n=120, ramp_at=50, ramp_len=20, glitch_at=None):
        """A log where Fan #2 ramps 600 -> 1200 RPM after the CPU heats up."""
        con = logdb.connect()
        snap0 = snapshot()
        cols, meta = logdb.columns(snap0)
        row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        logdb.attach(con, row, cols, meta, snap0)
        with con:
            for i in range(n):
                hot = ramp_at - 5 <= i < ramp_at + ramp_len
                fan = 1200.0 if ramp_at <= i < ramp_at + ramp_len else 600.0
                pump = 7300.0 if i == glitch_at else 4300.0
                s = snapshot(cpu_temp=80.0 if hot else 50.0, fan2=fan, pump=pump)
                con.execute("INSERT INTO samples VALUES (?,?,?)", (sid, 1000.0 + i, logdb.pack(s, cols)))
            con.execute("INSERT INTO procs VALUES (?,?,?)",
                        (sid, 1000.0 + ramp_at, json.dumps({"cpu": [["busy.exe", 40.0]], "gpu": []})))
        return cols


class Logs(TempDB):
    def test_pack_roundtrip_and_missing_values(self):
        snap = snapshot()
        cols, _ = logdb.columns(snap)
        vals = logdb.unpack(logdb.pack(snap, cols + ["/not/a/sensor"]))
        self.assertEqual(vals[-1], None)
        self.assertEqual(vals[cols.index("/lpc/nct6798d/0/fan/1")], 600.0)

    def test_columns_skip_hidden_and_unreadable(self):
        cols, _ = logdb.columns(snapshot(admin=False))
        self.assertNotIn("/intelcpu/0/temperature/9", cols)     # hidden (distance to TjMax)
        self.assertNotIn("/intelcpu/0/power/0", cols)           # reads 0 without admin
        self.assertIn("/intelcpu/0/load/0", cols)

    def test_session_lifecycle(self):
        r = logdb.start("test", max_hours=1)
        self.assertTrue(r["started"])
        self.assertFalse(logdb.start("again")["started"])     # one at a time
        self.assertTrue(logdb.mark("loud")["marked"])
        self.assertTrue(logdb.stop()["stopped"])
        self.assertFalse(logdb.mark("too late")["marked"])
        s = logdb.sessions()[0]
        self.assertEqual((s["label"], s["marks"], s["active"]), ("test", 1, False))
        self.assertTrue(logdb.delete(s["id"])["deleted"])
        self.assertEqual(logdb.sessions(), [])


class Reports(TempDB):
    def setUp(self):
        super().setUp()
        self.sid = logdb.start("ramp test")["session"]
        logdb.stop()

    def test_ramp_found_with_cause(self):
        self.fill(self.sid)
        r = report.report(self.sid)
        self.assertEqual(r["fan_ramps_total"], 1)
        ramp = r["fan_ramps"][0]
        self.assertEqual(ramp["fans"][0], {"fan": "Fan #2", "from_rpm": 600, "to_rpm": 1200})
        self.assertGreaterEqual(ramp["lasted_s"], 19)
        self.assertTrue(any("CPU Package" in t["sensor"] for t in ramp["temps_rose_30s_before"]))
        self.assertEqual(ramp["top_cpu"][0][0], "busy.exe")

    def test_single_sample_glitch_is_not_a_ramp(self):
        self.fill(self.sid, ramp_at=500, glitch_at=60)          # no real ramp, one pump misread
        self.assertEqual(report.report(self.sid)["fan_ramps_total"], 0)

    def test_stats_and_names(self):
        self.fill(self.sid)
        r = report.report(self.sid, names={"/lpc/nct6798d/0/fan/1": "Top exhaust"})
        fan = next(f for f in r["fans"] if f["fan"] == "Top exhaust")
        self.assertEqual((fan["rpm"]["min"], fan["rpm"]["max"]), (600.0, 1200.0))
        self.assertEqual(r["session"]["samples"], 120)

    def test_query_and_export(self):
        self.fill(self.sid)
        q = report.query(self.sid, ["Fan #2"], points=10)
        series = next(iter(q["series"].values()))
        self.assertLessEqual(len(series), 13)
        out = Path(self.dir.name) / "log.csv"
        r = report.export_csv(self.sid, str(out))
        self.assertEqual(r["rows"], 120)
        self.assertTrue(out.read_text(encoding="utf-8").startswith("time,"))

    def test_no_logs(self):
        logdb.delete(self.sid)
        self.assertIn("error", report.report())


if __name__ == "__main__":
    unittest.main()
