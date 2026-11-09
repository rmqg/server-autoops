import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from server_autoops import setup
from server_autoops.policy import validate_policy

ROOT = Path(__file__).resolve().parent.parent


class SetupWizardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.etc = Path(self.temp.name)
        config = json.loads((ROOT / "examples" / "config.example.json").read_text())
        config.update(root=str(self.etc / "var"), stream=str(self.etc / "var/alert-stream.jsonl"),
                      state_db=str(self.etc / "var/state/queue.sqlite3"), runs_dir=str(self.etc / "var/runs"),
                      policy=str(self.etc / "policy.json"), timezone="UTC")
        config["notify"] = {"kind": "none"}
        config["agent"]["model"]["env_file"] = str(self.etc / "model.env")
        config["agent"]["plugin_dir"] = str(ROOT / "plugin")
        self.config = self.etc / "config.json"
        self.config.write_text(json.dumps(config))
        (self.etc / "policy.json").write_text('{"version": 1, "services": {}, "faults": {}}')

    def tearDown(self):
        self.temp.cleanup()

    def answers(self, text):
        path = self.etc / "answers.env"
        path.write_text(text)
        path.chmod(0o600)
        return setup.read_answers(str(path))

    def test_quickstart_policy_is_valid(self):
        policy, errors, _ = validate_policy(setup.quickstart_policy("/usr/bin/docker"))
        self.assertEqual(errors, [])
        self.assertIn("quickstart-web:WEB_DOWN", policy["faults"])

    def test_quickstart_answers(self):
        self.assertEqual(setup.wizard(self.config, self.answers("AUTOOPS_AGENT=quickstart\n"), False), 0)
        config = json.loads(self.config.read_text())
        self.assertEqual(config["agent"]["kind"], "mock")
        self.assertEqual(config["agent"]["plans"]["quickstart-web:WEB_DOWN"][-1], {"op": "start", "target": "quickstart-web"})
        self.assertIn("quickstart-web", json.loads((self.etc / "policy.json").read_text())["services"])

    def test_quickstart_never_overwrites_a_policy(self):
        own = '{"version": 1, "services": {"x": {"container": "x"}}, "faults": {}}'
        (self.etc / "policy.json").write_text(own)
        self.assertEqual(setup.wizard(self.config, self.answers("AUTOOPS_AGENT=quickstart\n"), False), 0)
        self.assertEqual((self.etc / "policy.json").read_text(), own)

    def test_dsh_answers_write_secrets_600(self):
        text = ("AUTOOPS_AGENT=dsh\nAUTOOPS_MODEL_BASE_URL=https://api.test.invalid/v1\nAUTOOPS_MODEL=m1\n"
                "AUTOOPS_MODEL_API_KEY='sk-only-a-test'\nAUTOOPS_BARK_URL=https://api.day.app/NOTREAL\n")
        self.assertEqual(setup.wizard(self.config, self.answers(text), False), 0)
        config = json.loads(self.config.read_text())
        self.assertEqual(config["agent"]["model"]["model"], "m1")
        self.assertEqual(config["notify"]["kind"], "bark")
        for name in ("model.env", "bark.url"):
            self.assertEqual((self.etc / name).stat().st_mode & 0o777, 0o600)
        self.assertIn("AUTOOPS_MODEL_API_KEY=sk-only-a-test", (self.etc / "model.env").read_text())
        self.assertNotIn("sk-only-a-test", self.config.read_text())

    def test_bad_answers_change_nothing(self):
        before = self.config.read_text()
        self.assertEqual(setup.wizard(self.config, self.answers("AUTOOPS_AGENT=dsh\nAUTOOPS_MODEL_BASE_URL=ftp://x\n"), False), 1)
        self.assertEqual(self.config.read_text(), before)
        with self.assertRaises(SystemExit):
            self.answers("AUTOOPS_UNKNOWN=1\n")

    def test_check_only_runs_without_root(self):
        out = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "server-autoops.py"), "setup", "--check-only"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("检查环境", out.stdout)

    def test_unexpected_errors_are_explained_in_chinese(self):
        env = {k: v for k, v in os.environ.items() if k != "AUTOOPS_DEBUG"}
        out = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "server-autoops.py"), "status", "--config", str(self.etc / "missing.json")],
                             capture_output=True, text=True, timeout=60, env=env)
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("Traceback", out.stderr)


if __name__ == "__main__":
    unittest.main()
