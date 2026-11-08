import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path

from server_autoops import scheduler

TZ = dt.timezone(dt.timedelta(hours=8))


def _event(code, level="failure"):
    return {"service": "svc", "fault_code": code, "level": level, "title": "t", "summary": "s"}


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.now = dt.datetime(2026, 11, 8, 14, 0, tzinfo=TZ).timestamp()
        (self.base / "alerts.jsonl").write_text("")
        (self.base / "policy.json").write_text(json.dumps({
            "services": {"t": {"container": "t", "mutable": True}},
            "faults": {f"svc:{c}": {"targets": ["t"]} for c in ("A", "B", "C")}}))
        self.runs = []
        self.notes = []
        self.sched = self.new()

    def new(self):
        config = {"root": str(self.base / "root"), "stream": str(self.base / "alerts.jsonl"),
                  "state_db": str(self.base / "state.sqlite3"), "policy": str(self.base / "policy.json"),
                  "timezone": "UTC+08:00", "cooldown_seconds": 0, "service_hourly_limit": 0, "daily_limit": 0,
                  "agent_timeout": 5, "min_available_mb": 0, "poll_seconds": 0.01, "daily_hour": 8,
                  "notifications_enabled": True}
        return scheduler.Scheduler(config, clock=lambda: self.now, memory_probe=lambda: 4096,
                                   runner_executor=self.execute, notifier=lambda t, b, k: self.notes.append((t, k)))

    def execute(self, claim):
        self.runs.append(claim["fault_code"])
        return {"exit_code": 0, "result": {"status": "repaired", "verified": True, "business_verified": True,
                                           "summary": "ok"}, "timed_out": False}

    def tearDown(self):
        self.sched.close()
        self.temp.cleanup()

    def statuses(self):
        with contextlib.closing(self.sched._connect()) as conn:
            return {r["fault_code"]: r["status"] for r in conn.execute("SELECT fault_code,status FROM queue")}

    def test_alerts_are_recorded_but_no_agent_starts(self):
        info = self.sched.maintenance_on(duration=3600, reason="切换 wechat-export")
        self.assertEqual(info["running_now"], 0)
        self.sched.enqueue_event(_event("A"))
        self.assertIsNone(self.sched.run_one())
        self.assertEqual(self.runs, [])
        self.assertEqual(self.statuses(), {"A": "pending"})
        self.assertEqual(self.sched.status()["maintenance"]["reason"], "切换 wechat-export")
        # a recovery during maintenance still cancels the queued alert
        self.sched.enqueue_event(_event("A_RECOVERED", level="recovery"))
        self.assertEqual(self.statuses(), {"A": "recovered"})
        self.sched.maintenance_off()
        self.assertIsNone(self.sched.status()["maintenance"])

    def test_off_keeps_or_drops_alerts_from_the_window(self):
        self.sched.enqueue_event(_event("A"))          # before maintenance
        self.now += 5
        self.sched.maintenance_on(duration=0)
        self.now += 10
        self.sched.enqueue_event(_event("B"))
        result = self.sched.maintenance_off(drop_pending=True)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(self.statuses(), {"A": "pending", "B": "blocked"})
        self.sched.drain()
        self.assertEqual(self.runs, ["A"])
        self.sched.maintenance_on(duration=0)
        self.sched.enqueue_event(_event("C"))
        self.sched.maintenance_off()                    # default: keep and repair afterwards
        self.sched.drain()
        self.assertEqual(self.runs, ["A", "C"])

    def test_window_expires_by_itself_with_a_notice(self):
        self.sched.maintenance_on(duration=1800, reason="x")
        self.sched.enqueue_event(_event("A"))
        self.assertIsNone(self.sched.run_one())
        self.now += 1801
        self.assertIsNone(self.sched.status()["maintenance"])
        self.assertIsNotNone(self.sched.run_one())
        self.assertEqual(self.runs, ["A"])
        self.sched.deliver_notifications()
        self.assertIn(("AutoOps 维护模式已到期结束", "maintenance"), self.notes)

    def test_state_survives_restart_and_daily_report_mentions_it(self):
        self.sched.maintenance_on(duration=0, reason="长期")
        self.sched.close()
        self.sched = self.new()
        self.sched.enqueue_event(_event("A"))
        self.assertIsNone(self.sched.run_one())
        report = self.sched.create_daily_report("2026-11-07")
        self.assertIn("维护模式仍然开启", report["body"])

    def test_cli_on_status_off(self):
        cfg = self.base / "config.json"
        cfg.write_text(json.dumps({"root": str(self.base / "cli"), "stream": str(self.base / "alerts.jsonl"),
                                   "policy": str(self.base / "policy.json"), "agent": {"kind": "disabled"}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(scheduler.main(["--config", str(cfg), "maintenance", "on", "--for", "2h", "--reason", "演练"]), 0)
            self.assertEqual(scheduler.main(["--config", str(cfg), "maintenance", "status"]), 0)
            self.assertEqual(scheduler.main(["--config", str(cfg), "maintenance", "off"]), 0)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertIsNotNone(lines[0]["until_iso"])
        self.assertEqual(lines[1]["maintenance"]["reason"], "演练")
        self.assertTrue(lines[2]["was_on"])

    def test_duration_parsing(self):
        self.assertEqual(scheduler.parse_duration("30m"), 1800)
        self.assertEqual(scheduler.parse_duration("6h"), 21600)
        self.assertEqual(scheduler.parse_duration("0"), 0)
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.parse_duration("forever")
