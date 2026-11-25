"""Run with: python3 -m unittest discover tests"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import build  # noqa: E402
import config  # noqa: E402
import demo  # noqa: E402
import redact  # noqa: E402
import scan  # noqa: E402
import schedule  # noqa: E402


class Schedule(unittest.TestCase):
    def test_cron(self):
        self.assertAlmostEqual(schedule.parse("*/15 * * * *")[0], 96)
        self.assertAlmostEqual(schedule.parse("0 9 * * 1-5")[0], 5 / 7)
        self.assertAlmostEqual(schedule.parse("30 7 * * *")[0], 1)
        self.assertEqual(schedule.parse("0 9 * * 1-5")[1], "weekdays 09:00")

    def test_words(self):
        self.assertAlmostEqual(schedule.parse("every 30m")[0], 48)
        self.assertAlmostEqual(schedule.parse("hourly")[0], 24)
        self.assertEqual(schedule.parse("always-on")[2], "resident")
        self.assertEqual(schedule.parse("manual")[2], "manual")

    def test_gap(self):
        self.assertGreater(schedule.expected_gap_hours(1), 24)
        self.assertIsNone(schedule.expected_gap_hours(None))


class Config(unittest.TestCase):
    def test_subset_parser_reads_the_example(self):
        with open(os.path.join(HERE, "examples", "agents.example.toml"), encoding="utf-8") as fh:
            cfg = config.parse(fh.read())
        self.assertEqual(cfg["city"]["name"], "My Agent City")
        self.assertEqual(len(cfg["district"]), 3)
        self.assertEqual(cfg["agent"][0]["writes"], ["~/agents/notes/inbox/triage.md"])
        self.assertEqual(cfg["hosts"]["homelab"]["ssh"], "me@homelab.local")
        self.assertEqual(cfg["yards"]["rates"]["output"], 25.0)
        self.assertEqual(cfg["discover"]["launchd_prefixes"], ["com.example."])

    def test_comments_and_strings(self):
        cfg = config.parse('a = "x # not a comment"  # comment\nb = [1,\n  2, # two\n 3]\n')
        self.assertEqual(cfg, {"a": "x # not a comment", "b": [1, 2, 3]})


class Redact(unittest.TestCase):
    def test_secrets_go_paths_stay(self):
        out = redact.scrub("key sk-ant-abcdefghijklmnopqrstuvwx at /var/folders/ab/"
                           "cdefghijklmnopqrstuvwxyz0123456789abcdef/x.log, mail a@b.co")
        self.assertNotIn("sk-ant-abc", out)
        self.assertNotIn("a@b.co", out)
        self.assertIn("/var/folders/ab/cdefghijklmnopqrstuvwxyz0123456789abcdef/x.log", out)

    def test_timestamps_are_not_phones(self):
        self.assertEqual(redact.scrub("2026-01-02 03:04:05 ok"), "2026-01-02 03:04:05 ok")


class Paths(unittest.TestCase):
    def test_meet(self):
        self.assertTrue(scan.paths_meet("~/a/notes/", "~/a/notes/x.md"))
        self.assertTrue(scan.paths_meet("~/a/r.md", "~/a/r.md"))
        self.assertFalse(scan.paths_meet("~/a/r.md", "~/a/s.md"))
        self.assertFalse(scan.paths_meet("/tmp/", "/tmp/x"))   # a bare top-level folder is too broad


class Scan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def log(self, name, age_h, lines):
        p = os.path.join(self.root, name)
        with open(p, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        t = time.time() - age_h * 3600
        os.utime(p, (t, t))
        return p

    def run_scan(self, agents):
        cfg = {"agent": agents, "discover": {"cron": False}}
        return scan.scan(cfg, with_sessions=False, quiet=True,
                         snapshots_dir=os.path.join(self.root, "snaps"))

    def test_states(self):
        snap = self.run_scan([
            {"id": "ok", "schedule": "hourly", "log": self.log("ok.log", 0.5, ["fine"])},
            {"id": "err", "schedule": "hourly", "log": self.log("err.log", 0.5, ["ERROR boom"])},
            {"id": "quiet", "schedule": "hourly", "log": self.log("q.log", 30, ["fine"])},
            {"id": "never", "launchd": "com.example.agent-city-test.never", "tier": 3},
            {"id": "off", "paused": "on purpose"},
            {"id": "nolog", "schedule": "daily"},
            {"id": "chk", "schedule": "always-on", "check": "true"},
            {"id": "badchk", "schedule": "always-on", "check": "false"},
        ])
        st = {a["id"]: a["state"] for a in snap["agents"]}
        self.assertEqual(st, {"ok": "live", "err": "failing", "quiet": "ruin", "never": "scaffold",
                              "off": "paused", "nolog": "unknown", "chk": "live",
                              "badchk": "failing"})
        titles = " ".join(f["title"] for f in snap["findings"])
        self.assertIn("err is failing", titles)
        self.assertIn("quiet has gone quiet", titles)

    def test_traceback_reports_the_exception(self):
        snap = self.run_scan([{"id": "t", "schedule": "hourly", "log": self.log("t.log", 0.2, [
            "Traceback (most recent call last):", '  File "x.py", line 1', "KeyError: 'k'"])}])
        self.assertEqual(snap["agents"][0]["run_errors"][0], "KeyError: 'k'")

    def test_roads(self):
        notes = os.path.join(self.root, "notes") + "/"
        snap = self.run_scan([
            {"id": "w", "schedule": "manual", "writes": [notes + "a.md"]},
            {"id": "r", "schedule": "manual", "reads": [notes]},
        ])
        self.assertEqual([(e["from"], e["to"]) for e in snap["edges"]], [("w", "r")])


class Demo(unittest.TestCase):
    def test_demo_builds(self):
        snap = demo.snapshot()
        self.assertGreater(len(snap["agents"]), 20)
        states = {a["state"] for a in snap["agents"]}
        self.assertTrue({"live", "failing", "ruin", "scaffold", "paused"} <= states)
        self.assertTrue(snap["sites"])
        html = build.render(snap)
        self.assertIn("window.__CITY__ = {", html)
        self.assertNotIn("/* The snapshot is injected here", html)
        # nothing from the throwaway folder leaks into the page
        self.assertNotIn("agent-city-demo-", html)
        json.dumps(snap)


if __name__ == "__main__":
    unittest.main()
