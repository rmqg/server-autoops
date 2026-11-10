import json
import hashlib
import os
import subprocess
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server_autoops.broker import Broker, BrokerError, serve


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "service.json"
        self.config_path.write_text('{"limit": 3, "nested": {"enabled": true}}\n', encoding="utf-8")
        self.compose_path = self.root / "compose.yaml"
        self.compose_path.write_text("services: {}\n", encoding="utf-8")
        self.event = {"service": "api", "fault_code": "DOWN"}
        self.policy = {
            "services": {
                "api": {
                    "daemon": "main", "container": "api-container", "mutable": True,
                    "login_sensitive": False, "probe": {"kind": "running"},
                    "configs": {"main": {"path": str(self.config_path), "format": "json", "editable_keys": ["limit", "nested.enabled"], "bounds": {"limit": [1, 5]}}},
                    "compose": {"file": str(self.compose_path), "service": "api", "project_dir": str(self.root)},
                },
                "vm": {"daemon": "main", "container": "vm", "mutable": True, "login_sensitive": False},
            },
            "faults": {"api:DOWN": {"targets": ["api", "vm"]}},
            "protected_name_patterns": ["vm", "autoops", "broker", "listener"],
            "docker": {"bin": "/usr/bin/docker", "daemons": {"main": "unix:///var/run/docker.sock", "dsh": "unix:///run/second-docker.sock"}},
        }
        self.rebuild()

    def rebuild(self):
        """Broker validates and copies the policy, so tests rebuild it after editing self.policy."""
        self.broker = Broker(self.policy, self.root / "run", self.event)

    def tearDown(self):
        self.temp.cleanup()

    def patch_request(self, **kwargs):
        request = {
            "op": "patch_config", "target": "api", "config": "main",
            "expected_sha256": hashlib.sha256(self.config_path.read_bytes()).hexdigest(),
            "changes": {"limit": 4},
        }
        request.update(kwargs)
        return request

    def test_patch_recreates_compose_service_and_reports_verified_success(self):
        with patch.object(self.broker, "_recreate_command", return_value=subprocess.CompletedProcess([], 0)), patch.object(self.broker, "_probe", return_value={"ok": True, "kind": "running"}) as probe:
            result = self.broker.handle(self.patch_request())
        self.assertTrue(result["verified"])
        self.assertEqual(result["sha256"], hashlib.sha256(self.config_path.read_bytes()).hexdigest())
        self.assertEqual(probe.call_count, 1)

    def test_malicious_daemon_and_target_injection_are_rejected(self):
        self.policy["services"]["api"]["daemon"] = "main;sh"
        with patch("server_autoops.broker.subprocess.run") as run:
            with self.assertRaises(BrokerError):
                self.rebuild()
                self.broker.handle({"op": "inspect", "target": "api"})
            self.policy["services"]["api"]["daemon"] = "main"
            with self.assertRaises(BrokerError):
                self.broker.handle({"op": "inspect", "target": "api; rm -rf /"})
        run.assert_not_called()

    def test_failed_post_patch_probe_restores_file_then_recreates(self):
        original = self.config_path.read_bytes()
        with patch.object(self.broker, "_recreate_command", return_value=subprocess.CompletedProcess([], 0)), patch.object(
            self.broker, "_wait_for_probe", side_effect=[{"ok": False, "kind": "running"}, {"ok": True, "kind": "running"}]
        ):
            with self.assertRaises(BrokerError):
                self.broker.handle(self.patch_request())
        self.assertEqual(self.config_path.read_bytes(), original)
        with patch.object(self.broker, "_probe", return_value={"ok": True, "kind": "running"}):
            result = self.broker.handle({"op": "finish", "summary": "回滚后验证"})
        self.assertTrue(result["rolled_back"])
        self.assertTrue(result["verified"])

    def test_cas_mismatch_and_non_allowlisted_key_are_rejected(self):
        with self.assertRaises(BrokerError):
            self.broker.handle(self.patch_request(expected_sha256="0" * 64))
        with self.assertRaises(BrokerError):
            self.broker.handle(self.patch_request(changes={"command": "unsafe"}))

    def test_symlink_and_hardlink_config_are_rejected(self):
        link = self.root / "linked.json"
        link.symlink_to(self.config_path)
        with self.assertRaises(BrokerError):
            self.broker._read_regular(str(link))
        hard = self.root / "hard.json"
        os.link(self.config_path, hard)
        with self.assertRaises(BrokerError):
            self.broker._read_regular(str(self.config_path))
    def test_symlinked_parent_directory_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / "settings.json"
        target.write_text('{"safe": true}\n', encoding="utf-8")
        linked_parent = self.root / "linked-dir"
        linked_parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(BrokerError):
            self.broker._read_regular(str(linked_parent / "settings.json"))


    def test_vm_target_and_scan_only_fault_cannot_mutate(self):
        with self.assertRaises(BrokerError):
            self.broker.handle({"op": "restart", "target": "vm"})
        self.broker._event_fault["needs_scan"] = True
        with self.assertRaises(BrokerError):
            self.broker.handle({"op": "recreate", "target": "api"})
    def test_patch_rejects_bound_violations_and_type_changes(self):
        with self.assertRaises(BrokerError):
            self.broker.handle(self.patch_request(changes={"limit": 6}))
        with self.assertRaises(BrokerError):
            self.broker.handle(self.patch_request(changes={"limit": "4"}))

    def test_finish_keeps_model_diagnosis_separate_from_probe_result(self):
        with patch.object(self.broker, "_probe", return_value={"ok": False, "kind": "running"}):
            result = self.broker.handle({"op": "finish", "summary": "连接超时，怀疑配置异常"})
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["verified"])
        self.assertIn("连接超时，怀疑配置异常", result["summary"])
        self.assertIn("api", result["summary"])


    def test_finish_downgrades_container_only_probe_to_business_unverified(self):
        with patch.object(self.broker, "_probe", return_value={"ok": True, "kind": "running", "running": True}):
            result = self.broker.handle({"op": "finish", "summary": "已重启"})
        self.assertTrue(result["verified"])
        self.assertFalse(result["business_verified"])
        self.assertEqual(result["status"], "healthy")
        self.assertIn("容器在跑、业务未验证", result["summary"])

    def test_finish_with_business_probes_is_business_verified(self):
        with patch.object(self.broker, "_probe", return_value={"ok": True, "kind": "http", "running": True, "http_status": 200}):
            result = self.broker.handle({"op": "finish", "summary": "已修复"})
        self.assertTrue(result["verified"])
        self.assertTrue(result["business_verified"])
        self.assertNotIn("业务未验证", result["summary"])
        with patch.object(self.broker, "_probe", return_value={"ok": False, "kind": "http", "running": True}):
            failed = self.broker.handle({"op": "finish", "summary": "未恢复"})
        self.assertFalse(failed["business_verified"])
        self.assertEqual(failed["status"], "failed")

    def test_restart_waits_for_probe_readiness(self):
        with patch.object(self.broker, "_probe", side_effect=[{"ok": False}, {"ok": False}, {"ok": True}]), patch.object(
            self.broker, "_docker", return_value=subprocess.CompletedProcess([], 0)
        ), patch("server_autoops.broker.time.sleep"):
            result = self.broker.handle({"op": "restart", "target": "api"})
        self.assertTrue(result["ok"])

    def test_protected_target_can_be_inspected_but_not_mutated(self):
        with patch.object(self.broker, "_docker_json", return_value={"Running": True}):
            result = self.broker.handle({"op": "inspect", "target": "vm"})
        self.assertTrue(result["probe"]["ok"])
        with self.assertRaises(BrokerError):
            self.broker.handle({"op": "restart", "target": "vm"})

    def test_default_protected_patterns_block_self_management(self):
        own = {"daemon": "main", "container": "autoops-canary", "mutable": True, "login_sensitive": False}
        policy = {"services": {"autoops-canary": own}, "faults": {"canary:DOWN": {"targets": ["autoops-canary"]}}}
        broker = Broker(policy, self.root / "canary-run", {"service": "canary", "fault_code": "DOWN"})
        with self.assertRaises(BrokerError):
            broker.handle({"op": "restart", "target": "autoops-canary"})
        policy["protected_name_patterns"] = []
        broker = Broker(policy, self.root / "canary-run2", {"service": "canary", "fault_code": "DOWN"})
        with patch.object(broker, "_probe", return_value={"ok": True}), patch.object(broker, "_docker", return_value=subprocess.CompletedProcess([], 0)):
            result = broker.handle({"op": "restart", "target": "autoops-canary"})
        self.assertTrue(result["ok"])

    def test_dsh_service_is_eligible_for_mapped_mutation(self):
        self.policy["services"]["api"]["daemon"] = "dsh"
        self.rebuild()
        with patch.object(self.broker, "_probe", return_value={"ok": True}), patch.object(self.broker, "_docker", return_value=subprocess.CompletedProcess([], 0)):
            result = self.broker.handle({"op": "restart", "target": "api"})
        self.assertTrue(result["ok"])

    def test_read_config_and_logs_redact_sensitive_values(self):
        secret_path = self.root / "secrets.json"
        secret_path.write_text('{"bark_url":"https://example.test/bark/token-abc","password":"hidden"}\n', encoding="utf-8")
        self.policy["services"]["api"]["configs"]["secrets"] = {"path": str(secret_path), "format": "json", "editable_keys": []}
        log_path = self.root / "service.log"
        log_path.write_text("Authorization: Bearer abc\n", encoding="utf-8")
        self.policy["services"]["api"]["log_files"] = [str(log_path)]
        self.rebuild()
        config = self.broker.handle({"op": "read_config", "target": "api", "config": "secrets"})
        with patch.object(self.broker, "_docker", return_value=subprocess.CompletedProcess([], 0, "Authorization: Bearer xyz\n", "")):
            logs = self.broker.handle({"op": "logs", "target": "api"})
        self.assertEqual(config["values"]["bark_url"], "[REDACTED]")
        self.assertEqual(config["editable_keys"], [])
        self.assertNotIn("abc", next(iter(logs["files"].values())))

    def test_socket_jsonl_auth_and_shutdown_interface(self):
        socket_path = self.root / "broker.sock"
        server = serve(self.broker, socket_path, "local-token")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(socket_path))
                client.sendall((json.dumps({"token": "local-token", "request": {"op": "inventory"}}) + "\n").encode())
                response = json.loads(client.makefile("rb").readline())
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"]["targets"][0]["target"], "api")
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(socket_path.exists())


    def test_run_action_executes_whitelisted_action_and_audits_repaired(self):
        self.policy["services"]["api"]["actions"] = {
            "wake": {"argv": ["/bin/echo", "woke"], "scope": "container", "description": "唤醒窗口"},
            "sync": {"argv": ["/bin/echo", "synced"], "scope": "host", "description": "同步导出"},
        }
        self.rebuild()
        inv = self.broker.handle({"op": "inventory"})
        self.assertIn("wake", inv["targets"][0]["actions"])
        with patch.object(self.broker, "_probe", return_value={"ok": True, "kind": "http", "running": True, "http_status": 200}), patch(
            "server_autoops.broker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "woke\n", "")
        ):
            act_res = self.broker.handle({"op": "run_action", "target": "api", "action": "wake"})
            fin_res = self.broker.handle({"op": "finish", "summary": "通过 wake 动作恢复正常"})
        self.assertTrue(act_res["ok"])
        self.assertEqual(fin_res["status"], "repaired")
        with self.assertRaises(BrokerError):
            self.broker.handle({"op": "run_action", "target": "api", "action": "unlisted"})



if __name__ == "__main__":
    unittest.main()
