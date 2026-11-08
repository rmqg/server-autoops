"""Deterministic behavior regressions for the persistent AutoOps scheduler.

These tests intentionally exercise temporary SQLite/file state and stub the
runner/Bark/Docker boundaries. They are authored for the main session to run.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server_autoops import scheduler

SHANGHAI = dt.timezone(dt.timedelta(hours=8), name="UTC+08:00")


def _event(service: str, code: str, **extra):
    return {
        "timestamp": 1794117600.0,
        "iso_time": "2026-11-08T12:00:00+08:00",
        "service": service,
        "fault_code": code,
        "level": "failure",
        "title": f"{service} fault",
        "summary": "probe failed",
        "details": "deterministic fixture",
        "bark_sent": False,
        "auto_recovery_handled": False,
        **extra,
    }


def _policy_json(pairs, *, scan=(), extra=None):
    """Fixture policy: unmapped faults are now blocked before any Agent starts."""
    services = {"svc-target": {"daemon": "main", "container": "svc-target", "mutable": True,
                               "login_sensitive": False, "probe": {"kind": "running"}}}
    faults = {f"{service}:{code}": {"targets": ["svc-target"]} for service, code in pairs}
    for service, code in scan:
        faults[f"{service}:{code}"] = {"targets": ["svc-target"], "needs_scan": True}
    policy = {"services": services, "faults": faults}
    policy.update(extra or {})
    return json.dumps(policy)


MAPPED = [("svc", code) for code in ("A", "B", "C", "D", "LOWMEM", "EXIT", "SCAN", "PATCH", "SAFE", "INTERRUPTED")]


class SchedulerBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autoops-scheduler-test-")
        self.base = Path(self.temp.name)
        self.now = dt.datetime(2026, 11, 8, 14, 0, tzinfo=SHANGHAI).timestamp()
        self.stream = self.base / "alerts.jsonl"
        self.stream.write_bytes(b"")
        self.policy = self.base / "policy.json"
        self.policy.write_text(_policy_json(MAPPED), encoding="utf-8")
        self.runner = self.base / "runner.py"
        self.runner.write_text("", encoding="utf-8")
        self.notifications = []
        self.scheduler = self._new_scheduler()

    def tearDown(self):
        self.scheduler.close()
        self.temp.cleanup()

    def _new_scheduler(self, overrides=None, *, executor=None, memory=None, notifier=None, db_name="state.sqlite3"):
        root = self.base / f"root-{db_name}"
        config = {
            "root": str(root),
            "stream": str(self.stream),
            "state_db": str(self.base / db_name),
            "policy": str(self.policy),
            "runner": str(self.runner),
            "cooldown_seconds": 0,
            "service_hourly_limit": 3,
            "daily_limit": 20,
            "agent_timeout": 2,
            "min_available_mb": 0,
            "poll_seconds": 0.01,
            "daily_hour": 8,
            "timezone": "UTC+08:00",
            "notifications_enabled": False,
        }
        config.update(overrides or {})
        return scheduler.Scheduler(
            config,
            clock=lambda: self.now,
            memory_probe=memory or (lambda: 4096),
            runner_executor=executor,
            notifier=notifier or (lambda title, body, level: self.notifications.append((title, body, level))),
        )

    @staticmethod
    def _queue_rows(instance):
        with instance._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM queue ORDER BY id")]

    @staticmethod
    def _run_rows(instance):
        with instance._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY started_at,run_id")]

    @staticmethod
    def _notice_rows(instance):
        with instance._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM notifications ORDER BY id")]

    def test_first_start_tails_existing_history_and_replay_is_explicit(self):
        old = _event("svc", "OLD")
        self.stream.write_bytes((json.dumps(old) + "\n").encode())
        self.scheduler.close()
        self.scheduler = self._new_scheduler()

        self.scheduler.scan_stream()
        self.assertEqual(self.scheduler.status()["queue"]["pending"], 0)

        with self.stream.open("ab") as stream:
            stream.write((json.dumps(_event("svc", "NEW")) + "\n").encode())
        self.scheduler.scan_stream()
        self.assertEqual(self.scheduler.status()["queue"]["pending"], 1)
        self.scheduler.scan_stream(replay=True)
        rows = self._queue_rows(self.scheduler)
        self.assertEqual({row["fault_code"] for row in rows}, {"OLD", "NEW"})
        self.assertEqual(sum(row["status"] == "pending" for row in rows), 2)

    def test_half_utf8_line_resumes_and_invalid_rows_are_quarantined_redacted(self):
        self.scheduler.scan_stream()
        event = _event("svc", "HALF", details="诊断文本")
        encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
        split_at = encoded.index("诊".encode("utf-8")) + 1
        with self.stream.open("ab") as stream:
            stream.write(encoded[:split_at])
        self.scheduler.scan_stream()
        self.assertEqual(self.scheduler.status()["queue"]["pending"], 0)

        with self.stream.open("ab") as stream:
            stream.write(encoded[split_at:] + b"\n")
            stream.write(b'{"level":"failure","service":"bad","fault_code":"BAD","details":"token=abc123"}\xff\n')
        self.scheduler.scan_stream()
        self.assertEqual(self.scheduler.status()["queue"]["pending"], 1)
        self.assertEqual(self.scheduler.status()["quarantined_lines"], 1)
        records = [json.loads(line) for line in self.scheduler.error_log.read_text(encoding="utf-8").splitlines()]
        raw = base64.b64decode(records[0]["raw_b64"]).decode("utf-8", "replace")
        self.assertNotIn("abc123", raw)
        self.assertIn("[REDACTED]", raw)

    def test_duplicates_merge_and_recovery_only_cancels_correct_service(self):
        first = self.scheduler.enqueue_event(_event("svc", "A"))
        duplicate = self.scheduler.enqueue_event(_event("svc", "A", summary="repeat"))
        self.scheduler.enqueue_event(_event("svc", "B"))
        other = self.scheduler.enqueue_event(_event("other", "A"))
        self.assertEqual(first["action"], "queued")
        self.assertEqual(duplicate["action"], "merged")
        self.assertEqual(other["action"], "queued")
        self.assertEqual(self.scheduler.enqueue_event(_event("svc", "WARN", level="warning"))["action"], "ignored")
        self.assertEqual(self.scheduler.enqueue_event(_event("svc", "HANDLED", auto_recovery_handled=True))["action"], "ignored")

        recovery = _event("svc", "svc_RECOVERED", level="recovery", auto_recovery_handled=True)
        result = self.scheduler.enqueue_event(recovery)
        self.assertEqual(result["cancelled"], 2)
        rows = self._queue_rows(self.scheduler)
        self.assertEqual(sum(row["status"] == "recovered" for row in rows), 2)
        self.assertEqual([(row["service"], row["status"]) for row in rows if row["status"] == "pending"], [("other", "pending")])
        self.assertEqual(rows[0]["merged_count"], 1)

    def test_rotation_reads_new_inode_from_zero_and_drains_deleted_old_fd_tail(self):
        self.scheduler.scan_stream()
        old_line = json.dumps(_event("old-svc", "OLD"), ensure_ascii=False).encode("utf-8")
        cut = len(old_line) // 2
        with self.stream.open("ab") as stream:
            stream.write(old_line[:cut])
        self.scheduler.scan_stream()
        self.assertEqual(self.scheduler.status()["queue"]["pending"], 0)

        rotated = self.base / "alerts.jsonl.1"
        self.stream.rename(rotated)
        with rotated.open("ab") as stream:
            stream.write(old_line[cut:] + b"\n")
        rotated.unlink()
        new_line = json.dumps(_event("new-svc", "NEW"), ensure_ascii=False).encode("utf-8")
        self.stream.write_bytes(new_line + b"\n")
        self.scheduler.scan_stream()

        pending = [row for row in self._queue_rows(self.scheduler) if row["status"] == "pending"]
        self.assertEqual({(row["service"], row["fault_code"]) for row in pending}, {("old-svc", "OLD"), ("new-svc", "NEW")})

    def test_truncate_rewrite_same_inode_is_detected_by_checkpoint(self):
        self.scheduler.scan_stream()
        with self.stream.open("ab") as stream:
            stream.write((json.dumps(_event("svc", "FIRST")) + "\n").encode())
        self.scheduler.scan_stream()
        original_size = self.stream.stat().st_size

        replacement = _event("svc", "SECOND", summary="replacement content " + ("x" * 200))
        replacement_line = (json.dumps(replacement) + "\n").encode()
        self.assertGreater(len(replacement_line), original_size)
        self.stream.write_bytes(replacement_line)
        self.scheduler.scan_stream()
        self.assertEqual({row["fault_code"] for row in self._queue_rows(self.scheduler)}, {"FIRST", "SECOND"})

    def test_missing_old_inode_after_process_restart_is_quarantined_and_notified(self):
        self.scheduler.scan_stream()
        # Process stops first: its open descriptor would otherwise keep the old inode alive.
        self.scheduler.close()
        rotated = self.base / "lost.jsonl"
        old_inode = self.stream.stat().st_ino
        self.stream.rename(rotated)
        rotated.unlink()
        # Some file systems hand the freed inode to the next new file; a filler file takes it first.
        # (in a subdirectory, so the stream directory scan cannot mistake it for the lost file).
        (self.base / "filler").mkdir()
        (self.base / "filler" / "inode-filler").write_bytes(b"")
        self.stream.write_bytes((json.dumps(_event("svc", "NEW")) + "\n").encode())
        if self.stream.stat().st_ino == old_inode:
            self.skipTest("file system reused the deleted inode; the loss cannot be observed here")
        self.scheduler = self._new_scheduler()
        self.scheduler.scan_stream()

        self.assertEqual(self.scheduler.status()["queue"]["pending"], 1)
        self.assertEqual(self.scheduler.status()["quarantined_lines"], 1)
        notice = self._notice_rows(self.scheduler)
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0]["kind"], "stream_loss")
        self.assertEqual(notice[0]["state"], "suppressed")

    def test_low_memory_delays_without_consuming_budget_or_dropping_alert(self):
        available = [0]
        self.scheduler.config["min_available_mb"] = 1024
        self.scheduler.memory_probe = lambda: available[0]
        self.scheduler.enqueue_event(_event("svc", "LOWMEM"))
        self.assertIsNone(self.scheduler.run_one())
        self.assertEqual(self.scheduler.status()["queue"]["pending"], 1)
        self.assertEqual(len(self._run_rows(self.scheduler)), 0)

        available[0] = 4096
        self.now += 5
        self.scheduler.runner_executor = lambda claim: {
            "exit_code": 0,
            "result": {"status": "healthy", "summary": "healthy", "verified": True},
            "timed_out": False,
        }
        result = self.scheduler.run_one()
        self.assertTrue(result["success"])
        self.assertEqual(self.scheduler.status()["queue"]["resolved"], 1)


    def test_verified_result_with_nonzero_exit_requires_review(self):
        self.scheduler.runner_executor = lambda claim: {
            "exit_code": 1,
            "result": {"status": "repaired", "summary": "probe passed", "verified": True},
            "timed_out": False,
        }
        self.scheduler.enqueue_event(_event("svc", "EXIT"))
        result = self.scheduler.run_one()
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "needs_scan")
        self.assertEqual(self.scheduler.status()["queue"]["resolved"], 0)
        self.assertEqual(self.scheduler.status()["queue"]["needs_scan"], 1)

    def test_hourly_budget_persists_single_notification_and_keeps_fourth_pending(self):
        self.scheduler.config["notifications_enabled"] = True
        self.scheduler.runner_executor = lambda claim: {
            "exit_code": 1,
            "result": {"status": "needs_scan", "summary": "manual review", "verified": False},
            "timed_out": False,
        }
        for code in ("A", "B", "C", "D"):
            self.scheduler.enqueue_event(_event("svc", code))

        results = self.scheduler.drain()
        self.assertEqual(len(results), 3)
        rows = self._queue_rows(self.scheduler)
        self.assertEqual(sum(row["status"] == "needs_scan" for row in rows), 3)
        self.assertEqual(sum(row["status"] == "pending" for row in rows), 1)
        notices = self._notice_rows(self.scheduler)
        rate_notices = [row for row in notices if row["kind"] == "rate_limit"]
        self.assertEqual(len(rate_notices), 1)
        self.assertTrue(rate_notices[0]["dedupe_key"].startswith("rate-limit:service:svc:"))

    def test_daily_budget_and_persistent_circuit_breaker_keep_queue_items(self):
        daily = self._new_scheduler(
            {"service_hourly_limit": 0, "daily_limit": 2, "notifications_enabled": True},
            executor=lambda claim: {"exit_code": 1, "result": {"status": "needs_scan", "verified": False}, "timed_out": False},
            db_name="daily-budget.sqlite3",
        )
        self.addCleanup(daily.close)
        for code in ("A", "B", "C"):
            daily.enqueue_event(_event("svc", code))
        self.assertEqual(len(daily.drain()), 2)
        self.assertEqual(daily.status()["queue"]["pending"], 1)
        self.assertTrue(any(row["dedupe_key"] == f"rate-limit:daily:{scheduler._local_day(self.now)}" for row in self._notice_rows(daily)))

        breaker = self._new_scheduler(
            {"service_hourly_limit": 0, "daily_limit": 0, "notifications_enabled": True},
            executor=lambda claim: {"exit_code": 1, "result": {"status": "failed", "summary": "fixture failure", "verified": False}, "timed_out": False},
            db_name="breaker.sqlite3",
        )
        self.addCleanup(breaker.close)
        for code in ("A", "B", "C", "D"):
            breaker.enqueue_event(_event("svc", code))
        self.assertEqual(len(breaker.drain()), 3)
        self.assertEqual(breaker.status()["queue"]["pending"], 4)
        self.assertEqual(len([row for row in self._notice_rows(breaker) if row["kind"] == "breaker"]), 1)
        self.assertTrue(breaker.status()["breakers"][0]["open_until"])

    def test_notification_failure_retries_without_creating_queue_failures(self):
        self.scheduler.config["notifications_enabled"] = True
        self.scheduler.runner_executor = lambda claim: {
            "exit_code": 1,
            "result": {"status": "needs_scan", "summary": "inspect locally", "verified": False},
            "timed_out": False,
        }
        self.scheduler.notifier = lambda *_args: (_ for _ in ()).throw(RuntimeError("token=must-not-be-stored"))
        self.scheduler.enqueue_event(_event("svc", "SCAN"))
        self.scheduler.run_one()
        first = self.scheduler.deliver_notifications()
        self.assertEqual(first["failed"], 1)
        row = self._notice_rows(self.scheduler)[0]
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertNotIn("must-not-be-stored", row["last_error"])
        self.assertEqual(self.scheduler.deliver_notifications()["failed"], 0)
        self.assertEqual(self.scheduler.status()["queue"]["needs_scan"], 1)
        self.assertEqual(len(self._queue_rows(self.scheduler)), 1)

    def test_daily_report_includes_root_cause_and_rollback_and_heartbeat(self):
        self.scheduler.runner_executor = lambda claim: self._runner_result_file(claim)
        self.scheduler.enqueue_event(_event("svc", "PATCH", summary="root cause summary"))
        self.scheduler.run_one()
        report = self.scheduler.create_daily_report("2026-11-08")
        self.assertTrue(report["created"])
        self.assertIn("svc/PATCH", report["body"])
        self.assertIn("根因:root cause summary", report["body"])
        self.assertIn("回滚=是", report["body"])
        self.assertFalse(self.scheduler.create_daily_report("2026-11-08")["created"])

        empty = self._new_scheduler({"notifications_enabled": False}, db_name="heartbeat.sqlite3")
        self.addCleanup(empty.close)
        heartbeat = empty.create_daily_report("2026-11-08")
        self.assertIn("每日健康心跳", heartbeat["body"])
        self.assertEqual(empty.status()["notifications"]["suppressed"], 1)
        self.assertEqual(empty.deliver_notifications()["sent"], 0)

    def _runner_result_file(self, claim):
        result = {"status": "repaired", "summary": "root cause summary", "verified": True,
                  "actions": [], "rolled_back": True}
        (Path(claim["event_path"]).parent / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return {"exit_code": 0, "result": result, "timed_out": False}

    def test_restart_requeues_unmodified_work_but_never_repeats_mutation(self):
        self.scheduler.enqueue_event(_event("svc", "SAFE"))
        self.scheduler._claim_next()
        with mock.patch.object(scheduler.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(self.scheduler._resolve_running_on_restart(), 1)
        self.assertEqual(self.scheduler.status()["queue"]["pending"], 1)
        self.assertEqual(self._run_rows(self.scheduler)[0]["outcome"], "interrupted")

        mutating = self.scheduler._claim_next()
        actions = Path(mutating["event_path"]).parent / "actions.jsonl"
        actions.write_text('{"op":"restart","result":true}\n', encoding="utf-8")
        with mock.patch.object(scheduler.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(self.scheduler._resolve_running_on_restart(), 1)
        self.assertEqual(self.scheduler.status()["queue"]["needs_scan"], 1)
        self.assertEqual(self.scheduler.status()["queue"]["pending"], 0)


    def test_live_interruption_with_broker_mutation_requires_review(self):
        self.scheduler.enqueue_event(_event("svc", "INTERRUPTED"))
        claim = self.scheduler._claim_next()
        actions = Path(claim["event_path"]).parent / "actions.jsonl"
        actions.write_text('{"op":"patch_config_success","verified":true}\n', encoding="utf-8")
        execution = self.scheduler._reconcile_stopped_claim(
            claim, {"exit_code": None, "result": None, "interrupted": True, "timed_out": False}
        )
        self.assertEqual(execution["result"]["status"], "needs_scan")
        result = self.scheduler._finish_claim(claim, execution)
        self.assertFalse(result["success"])
        self.assertEqual(self.scheduler.status()["queue"]["needs_scan"], 1)


    def test_scheduler_cli_enqueue_and_run_once_are_real_behavior(self):
        config_path = self.base / "cli-config.json"
        cli_root = self.base / "cli-root"
        cli_stream = self.base / "cli-alerts.jsonl"
        cli_stream.write_bytes(b"")
        cli_db = self.base / "cli.sqlite3"
        state_file = self.base / "cli-state.json"
        state_file.write_text(json.dumps({"Running": True, "Health": "healthy"}), encoding="utf-8")
        cli_policy = self.base / "cli-policy.json"
        cli_policy.write_text(json.dumps({
            "allow_simulated": True,
            "services": {"cli-target": {"driver": "simulated", "state_file": str(state_file), "mutable": True,
                                        "probe": {"kind": "health"}}},
            "faults": {"cli-svc:CLI": {"targets": ["cli-target"]}}}), encoding="utf-8")
        config = {
            "root": str(cli_root), "stream": str(cli_stream), "state_db": str(cli_db),
            "policy": str(cli_policy), "timezone": "UTC+08:00", "agent": {"kind": "disabled"},
            "cooldown_seconds": 0, "service_hourly_limit": 3, "daily_limit": 20,
            "agent_timeout": 20, "agent_grace_seconds": 5, "min_available_mb": 0, "poll_seconds": 0.01,
            "daily_hour": 23, "notifications_enabled": False,
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        event_file = self.base / "event.json"
        event_file.write_text(json.dumps(_event("cli-svc", "CLI")), encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = scheduler.main(["--config", str(config_path), "enqueue", "--event-file", str(event_file)])
        self.assertEqual(result, 0)
        self.assertIn('"queue_id"', stdout.getvalue())
        self.assertIn('"action": "queued"', stdout.getvalue())

        # A separate empty state verifies tail-on-first-start, then a second --once
        # synchronously waits for the real runner subprocess and commits its final result.
        cli_db = self.base / "cli-run.sqlite3"
        config["state_db"] = str(cli_db)
        config["root"] = str(self.base / "cli-run-root")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.main(["--config", str(config_path), "run", "--once"]), 0)
        with cli_stream.open("ab") as stream:
            stream.write((json.dumps(_event("cli-svc", "CLI")) + "\n").encode())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.main(["--config", str(config_path), "run", "--once"]), 0)
        with sqlite3.connect(cli_db) as conn:
            row = conn.execute("SELECT status FROM queue WHERE service='cli-svc' AND fault_code='CLI'").fetchone()
            run = conn.execute("SELECT outcome,verified,exit_code FROM runs").fetchone()
        self.assertEqual(row, ("resolved",))
        self.assertEqual(run, ("healthy", 1, 0))

    def test_container_cleanup_is_run_label_scoped(self):
        from server_autoops import runner
        agent = {"kind": "dsh", "docker_bin": "/usr/bin/docker", "docker_host": "unix:///run/agent-docker.sock"}
        labels = json.dumps({runner.LABEL_ROLE: "agent", runner.LABEL_RUN: "expected-run"})
        inspected = mock.Mock(returncode=0, stdout=labels)
        with mock.patch.object(runner.subprocess, "run", side_effect=[inspected, mock.Mock(returncode=0)]) as run:
            runner.remove_agent_container(agent, "expected-run")
        self.assertEqual(run.call_count, 2)
        self.assertIn("unix:///run/agent-docker.sock", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["-f", runner.AGENT_NAME])

        other = mock.Mock(returncode=0, stdout=json.dumps({runner.LABEL_ROLE: "agent", runner.LABEL_RUN: "other-run"}))
        with mock.patch.object(runner.subprocess, "run", return_value=other) as run:
            runner.remove_agent_container(agent, "expected-run")
        self.assertEqual(run.call_count, 1)

        foreign = mock.Mock(returncode=0, stdout=json.dumps({"some.label": "x"}))
        with mock.patch.object(runner.subprocess, "run", return_value=foreign):
            with self.assertRaises(RuntimeError):
                runner.remove_agent_container(agent)

    # ---- 2026-11-08 review changes -------------------------------------------------

    @staticmethod
    def _failing(claim):
        return {"exit_code": 1, "result": {"status": "failed", "summary": "still down", "verified": False}, "timed_out": False}

    @staticmethod
    def _healthy(claim):
        return {"exit_code": 0, "result": {"status": "repaired", "summary": "fixed", "verified": True,
                                            "business_verified": True}, "timed_out": False}

    def _breaker_row(self, instance, service="svc"):
        with contextlib.closing(instance._connect()) as conn:
            return dict(conn.execute("SELECT * FROM breakers WHERE service=?", (service,)).fetchone())

    def test_breaker_backoff_doubles_to_24h_cap_persists_and_resets_on_success(self):
        mode = {"run": self._failing}
        sched = self._new_scheduler({"service_hourly_limit": 0, "daily_limit": 0, "notifications_enabled": True},
                                    executor=lambda claim: mode["run"](claim), db_name="backoff.sqlite3")
        sched.enqueue_event(_event("svc", "A"))
        for _ in range(3):
            self.assertIsNotNone(sched.run_one())
        self.assertIsNone(sched.run_one(), "breaker must stop the fourth immediate attempt")
        observed, open_days = [], set()
        for step in range(7):
            row = self._breaker_row(sched)
            observed.append(round(row["open_until"] - row["opened_at"]))
            open_days.add(scheduler._local_day(row["opened_at"]))
            self.now = row["open_until"] + 1
            if step == 2:  # restart in the middle: backoff state must survive
                sched.close()
                sched = self._new_scheduler({"service_hourly_limit": 0, "daily_limit": 0, "notifications_enabled": True},
                                            executor=lambda claim: mode["run"](claim), db_name="backoff.sqlite3")
            self.assertIsNotNone(sched.run_one(), "the item must be retried automatically after each backoff")
        self.assertEqual(observed, [3600, 7200, 14400, 28800, 57600, 86400, 86400])
        self.assertEqual(self._breaker_row(sched)["failures"], 10)
        open_days.add(scheduler._local_day(self._breaker_row(sched)["opened_at"]))
        breaker_notes = [row for row in self._notice_rows(sched) if row["kind"] == "breaker"]
        self.assertEqual(len(breaker_notes), len({row["dedupe_key"] for row in breaker_notes}))
        self.assertEqual({row["dedupe_key"] for row in breaker_notes}, {f"breaker:svc:{day}" for day in open_days})
        self.assertLess(len(breaker_notes), 10, "one notice per service per Shanghai day, not per reopening")
        self.assertEqual(sched.status()["queue"]["pending"], 1, "the alert is never dropped")

        mode["run"] = self._healthy
        self.now = self._breaker_row(sched)["open_until"] + 1
        self.assertTrue(sched.run_one()["success"])
        self.assertEqual(self._breaker_row(sched)["failures"], 0)
        mode["run"] = self._failing
        sched.enqueue_event(_event("svc", "B"))
        sched.run_one()
        row = self._breaker_row(sched)
        self.assertEqual((row["failures"], row["open_until"]), (1, 0), "counter restarts from zero after a repair")
        sched.close()

    def test_daily_budget_still_applies_while_breaker_backs_off(self):
        sched = self._new_scheduler({"service_hourly_limit": 0, "daily_limit": 4, "notifications_enabled": False},
                                    executor=self._failing, db_name="budget-backoff.sqlite3")
        self.addCleanup(sched.close)
        sched.enqueue_event(_event("svc", "A"))
        for _ in range(3):
            sched.run_one()
        self.now = self._breaker_row(sched)["open_until"] + 1       # 15:00, fourth run of the day
        self.assertIsNotNone(sched.run_one())
        self.now = self._breaker_row(sched)["open_until"] + 1       # 17:00, daily budget exhausted
        self.assertIsNone(sched.run_one())
        self.assertEqual(len(self._run_rows(sched)), 4)
        due = self._queue_rows(sched)[0]["due_at"]
        self.assertEqual(due, scheduler._next_local_midnight(self.now))

    def test_unmapped_alert_is_blocked_without_agent_budget_or_memory(self):
        calls = []
        self.scheduler.runner_executor = lambda claim: calls.append(claim) or self._healthy(claim)
        self.scheduler.config["min_available_mb"] = 1024
        self.scheduler.memory_probe = lambda: 0
        self.scheduler.enqueue_event(_event("bot-stack", "cookie-expired"))
        self.assertIsNone(self.scheduler.run_one())
        rows = self._queue_rows(self.scheduler)
        self.assertEqual([(row["status"], row["last_error"]) for row in rows], [("blocked", scheduler.UNMAPPED_REASON)])
        self.assertEqual(calls, [])
        self.assertEqual(self._run_rows(self.scheduler), [])
        report = self.scheduler.create_daily_report("2026-11-08")["body"]
        self.assertIn("当日受阻：1（其中无映射、未启动 Agent：1）", report)

        self.scheduler.memory_probe = lambda: 4096
        self.scheduler.enqueue_event(_event("svc", "A"))
        self.assertTrue(self.scheduler.run_one()["success"])
        self.assertEqual(len(calls), 1, "mapped alerts still start the Agent")

    def test_combined_watchdog_codes_are_split_aliased_and_scan_first(self):
        self.policy.write_text(_policy_json(
            [("bot-stack", code) for code in ("docker", "sidecar", "gateway_ws", "gateway_api")],
            scan=[("bot-stack", "login_lost")],
            extra={"code_aliases": {"bot-stack:ALL": ["login_lost", "gateway_ws", "gateway_api"]}}), encoding="utf-8")
        sched = self._new_scheduler(db_name="split.sqlite3")
        self.addCleanup(sched.close)
        result = sched.enqueue_event(_event("bot-stack", "docker\nsidecar"))
        self.assertEqual([part["action"] for part in result["parts"]], ["queued", "queued"])
        sched.enqueue_event(_event("bot-stack", "login_lost gateway_ws "))
        sched.enqueue_event(_event("bot-stack", "gateway_api"))
        rows = {row["fault_code"]: row for row in self._queue_rows(sched)}
        self.assertEqual(set(rows), {"docker", "sidecar", "login_lost", "gateway_api"})
        scan_event = json.loads(rows["login_lost"]["event_json"])
        self.assertEqual(scan_event["split_symptoms"], ["gateway_ws"])
        self.assertEqual(json.loads(rows["docker"]["event_json"])["split_from"], "docker\nsidecar")

        sched.enqueue_event(_event("bot-stack", "ALL", level="recovery"))
        sched.enqueue_event(_event("bot-stack", "docker\nsidecar", level="recovery"))
        statuses = {row["fault_code"]: row["status"] for row in self._queue_rows(sched)}
        self.assertEqual(statuses, {"docker": "recovered", "sidecar": "recovered",
                                    "login_lost": "recovered", "gateway_api": "recovered"})

        sched.scan_stream()
        with self.stream.open("ab") as stream:
            stream.write((json.dumps(_event("bot-stack", "docker\nsidecar")) + "\n").encode())
        self.assertEqual(sched.scan_stream()["events"], 1)
        self.assertEqual(sum(row["status"] == "pending" for row in self._queue_rows(sched)), 2)

    def test_container_only_probe_resolves_but_is_reported_unverified(self):
        self.scheduler.runner_executor = lambda claim: {
            "exit_code": 0, "timed_out": False,
            "result": {"status": "repaired", "summary": "restarted", "verified": True, "business_verified": False}}
        with self.scheduler._transaction() as conn:
            conn.execute("INSERT INTO breakers(service,failures,open_until) VALUES('svc',2,0)")
        self.scheduler.enqueue_event(_event("svc", "A"))
        result = self.scheduler.run_one()
        self.assertTrue(result["success"])
        self.assertFalse(result["business_verified"])
        self.assertEqual(result["outcome"], "repaired_unverified")
        self.assertEqual(self.scheduler.status()["queue"]["resolved"], 1)
        self.assertEqual(self._breaker_row(self.scheduler)["failures"], 0)
        self.assertEqual(self._run_rows(self.scheduler)[0]["outcome"], "repaired_unverified")
        body = self.scheduler.create_daily_report("2026-11-08")["body"]
        self.assertIn("业务已验证成功：0；容器在跑、业务未验证：1；未成功：0", body)
        self.assertIn("已处理(容器在跑,业务未验证)", body)

    def test_retention_prunes_only_expired_finished_state(self):
        present = self.now
        old = present - 100 * 86400
        self.now = old
        self.scheduler.runner_executor = self._healthy
        self.scheduler.enqueue_event(_event("svc", "A"))
        old_run = self.scheduler.run_one()["run_id"]
        old_dir = self.scheduler.run_root / old_run
        with self.scheduler._transaction() as conn:
            self.scheduler._enqueue_notification(conn, "old-sent", "daily", "t", "b")
            self.scheduler._enqueue_notification(conn, "old-pending", "breaker", "t", "b")
            conn.execute("UPDATE notifications SET state='sent' WHERE dedupe_key='old-sent'")
            conn.execute("UPDATE notifications SET state='pending' WHERE dedupe_key='old-pending'")
            conn.execute("INSERT INTO quarantines(created_at,path,raw_b64,reason,logged) VALUES(?,?,?,?,1)", (old, "p", "", "r"))
            conn.execute("INSERT INTO breakers(service,failures,open_until) VALUES('backing-off',5,?)", (old,))
            conn.execute("INSERT INTO breakers(service,failures,open_until) VALUES('healthy-svc',0,?)", (old,))
        self.scheduler.error_log.write_text(
            json.dumps({"at": scheduler._utc_iso(old), "reason": "old"}) + "\n"
            + json.dumps({"at": scheduler._utc_iso(present), "reason": "new"}) + "\n", encoding="utf-8")
        drill = self.scheduler.run_root / "drill-old"
        drill.mkdir()
        (drill / "report.json").write_text("{}", encoding="utf-8")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        (self.scheduler.run_root / "linked-old").symlink_to(outside, target_is_directory=True)
        for directory in (old_dir, drill):
            for child in directory.iterdir():
                os.utime(child, (old, old), follow_symlinks=False)
            os.utime(directory, (old, old))

        self.now = present
        self.scheduler.enqueue_event(_event("svc", "B"))
        self.scheduler.runner_executor = None
        running = self.scheduler._claim_next()
        self.assertEqual(running["fault_code"], "B")
        self.scheduler.enqueue_event(_event("svc", "C"))

        counts = self.scheduler.maybe_prune()
        self.assertIsNotNone(counts)
        self.assertIsNone(self.scheduler.maybe_prune(), "retention runs at most every 6 hours")
        self.assertEqual(counts["runs"], 1)
        self.assertEqual(counts["run_dirs"], 2)
        self.assertFalse(old_dir.exists())
        self.assertFalse(drill.exists())
        self.assertTrue(Path(running["event_path"]).parent.exists(), "an unfinished run is never pruned")
        self.assertTrue((self.scheduler.run_root / "linked-old").is_symlink())
        self.assertTrue((outside / "keep.txt").exists(), "symlinks are never followed")
        self.assertEqual({row["fault_code"]: row["status"] for row in self._queue_rows(self.scheduler)},
                         {"B": "running", "C": "pending"})
        self.assertEqual({row["dedupe_key"] for row in self._notice_rows(self.scheduler)}, {"old-pending"})
        self.assertEqual(self.scheduler.status()["quarantined_lines"], 0)
        with contextlib.closing(self.scheduler._connect()) as conn:
            self.assertEqual({row[0] for row in conn.execute("SELECT service FROM breakers")}, {"backing-off"})
        kept = [json.loads(line)["reason"] for line in self.scheduler.error_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kept, ["new"])


if __name__ == "__main__":
    unittest.main()
