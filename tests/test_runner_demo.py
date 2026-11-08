import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from server_autoops import runner

ROOT = Path(__file__).resolve().parent.parent


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def agent(self):
        env_file = self.base / "model.env"
        env_file.write_text("AUTOOPS_MODEL_API_KEY='sk-test-only'\nOTHER=1\n")
        env_file.chmod(0o600)
        return runner.validate_agent_config({"kind": "dsh", "image": "local/dsh-autoops:test", "model": {
            "base_url": "https://llm.example/v1", "model": "some-model", "env_file": str(env_file)}}, str(self.base))

    def test_dsh_container_is_locked_down(self):
        agent = self.agent()
        run_dir = self.base / "runs" / "r1"
        (run_dir / "bridge").mkdir(parents=True)
        cmd, env = runner._agent_command({}, agent, run_dir, run_dir / "bridge", {"service": "web"}, "r1")
        joined = " ".join(cmd)
        for flag in ("--read-only", "--cap-drop ALL", "no-new-privileges:true", "--memory 512m", "--pids-limit 96",
                     "--network bridge", "--user 1000:1000", "com.docker.compose.project="):
            self.assertIn(flag, joined)
        self.assertNotIn("docker.sock,dst", joined)
        self.assertNotIn("sk-test-only", joined)          # the key travels only through the environment
        self.assertEqual(env["AUTOOPS_MODEL_API_KEY"], "sk-test-only")
        patch = (run_dir / "home" / ".dsh" / "profiles" / "headless" / "cordis.patch.yml").read_text()
        for plugin in ("tool-bash", "tool-fs", "tool-web", "tool-subagent", "plugin-manager"):
            self.assertIn(f"- id: {plugin}\n  disabled: true", patch)
        self.assertIn('baseURL: "https://llm.example/v1"', patch)

    def test_world_readable_model_key_is_refused(self):
        agent = self.agent()
        os.chmod(agent["model"]["env_file"], 0o644)
        with self.assertRaises(RuntimeError):
            runner.credential(Path(agent["model"]["env_file"]), "AUTOOPS_MODEL_API_KEY")

    def test_invalid_agent_configs(self):
        for bad in ({"kind": "shell"}, {"kind": "dsh"}, {"kind": "dsh", "image": "x", "user": "0:0", "model": {}}):
            with self.assertRaises(ValueError):
                runner.validate_agent_config(bad, str(self.base))


class DemoTest(unittest.TestCase):
    def test_offline_demo_passes_end_to_end(self):
        with tempfile.TemporaryDirectory() as work:
            result = subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "server-autoops.py"), "demo", "--workdir", work],
                                    cwd=str(ROOT), capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
            report = json.loads((Path(work) / "report.json").read_text())
            self.assertTrue(report["passed"])
            self.assertGreaterEqual(len(report["checks"]), 20)
