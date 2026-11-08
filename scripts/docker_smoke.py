#!/usr/bin/env python3
"""Real-Docker smoke test: the broker repairs a disposable canary container.

Needs: Python 3.10+, a docker CLI and daemon, and permission to use them.
It creates one container (sa-smoke-canary) and removes it at the end; it touches
nothing else. The scripted mock agent stands in for the model.

  python3 scripts/docker_smoke.py --workdir /tmp/sa-smoke            # probe via 127.0.0.1 port
  python3 scripts/docker_smoke.py --workdir /tmp/sa-smoke --network sa-net   # when running inside a container

--workdir must be a path the Docker daemon can bind-mount (the canary reads its config there).
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server_autoops.broker import Broker, BrokerError  # noqa: E402
from server_autoops.policy import load_policy  # noqa: E402
from server_autoops.relay import dispatch  # noqa: E402
from server_autoops.scheduler import Scheduler, load_config  # noqa: E402

NAME = "sa-smoke-canary"
CANARY = """import json, http.server
ok = json.load(open('/canary/config.json')).get('enabled') is True   # read once: a restart is required
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'ok': ok}).encode()
        self.send_response(200 if ok else 503); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
print('canary started enabled=%s api_key=hunter2' % ok, flush=True)
http.server.HTTPServer(('0.0.0.0', 8000), H).serve_forever()
"""


def sh(*argv: str, check: bool = True) -> str:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if check and r.returncode:
        raise RuntimeError(f"{argv[:3]} failed: {r.stderr[-500:]}")
    return r.stdout.strip()


def http(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--image", default="python:3.12-alpine")
    ap.add_argument("--network", help="join this docker network and probe the canary by name")
    ap.add_argument("--docker-bin", default=shutil.which("docker") or "/usr/bin/docker")
    ap.add_argument("--docker-host", default="unix:///var/run/docker.sock")
    args = ap.parse_args()
    docker = [args.docker_bin, "-H", args.docker_host]
    base = Path(args.workdir).resolve()
    if base.exists():
        shutil.rmtree(base)
    (base / "canary").mkdir(parents=True)
    cfg = base / "canary" / "config.json"
    cfg.write_text('{"enabled": false, "api_key": "hunter2"}\n')
    cfg.chmod(0o640)
    (base / "canary" / "server.py").write_text(CANARY)
    sh(*docker, "rm", "-f", NAME, check=False)
    run = [*docker, "run", "-d", "--name", NAME, "--memory", "64m", "-v", f"{base / 'canary'}:/canary:ro"]
    if args.network:
        sh(*docker, "network", "create", args.network, check=False)
        run += ["--network", args.network]
        url = f"http://{NAME}:8000/"
    else:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        run += ["-p", f"127.0.0.1:{port}:8000"]
        url = f"http://127.0.0.1:{port}/"
    sh(*run, args.image, "python3", "/canary/server.py")
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  [{'通过' if ok else '失败'}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    try:
        for _ in range(60):
            if http(url) in (200, 503):
                break
            time.sleep(0.5)
        policy = {"version": 1, "docker": {"bin": args.docker_bin, "daemons": {"main": args.docker_host}},
                  "services": {"canary": {"container": NAME, "mutable": True, "probe_timeout_seconds": 30,
                                          "probe": {"kind": "http", "url": url, "json_key": "ok", "expected": True},
                                          "configs": {"app": {"path": str(cfg), "format": "json", "editable_keys": ["enabled"]}}}},
                  "faults": {"smoke:CONFIG_DISABLED": {"targets": ["canary"]}, "smoke:PROCESS_DOWN": {"targets": ["canary"]}}}
        (base / "policy.json").write_text(json.dumps(policy, indent=2))
        plans = {"smoke:CONFIG_DISABLED": [
                     {"op": "logs", "target": "canary"},
                     {"op": "read_config", "target": "canary", "config": "app"},
                     {"op": "patch_config", "target": "canary", "config": "app",
                      "expected_sha256": "$sha256:canary/app", "changes": {"enabled": True}}],
                 "smoke:PROCESS_DOWN": [{"op": "inspect", "target": "canary"}, {"op": "start", "target": "canary"}]}
        (base / "alerts.jsonl").write_text("")
        (base / "config.json").write_text(json.dumps({
            "root": str(base), "stream": str(base / "alerts.jsonl"), "policy": str(base / "policy.json"),
            "timezone": "UTC+08:00", "cooldown_seconds": 0, "min_available_mb": 0, "agent_timeout": 120,
            "agent_grace_seconds": 60, "notify": {"kind": "file", "path": str(base / "notify.jsonl")},
            "agent": {"kind": "mock", "plans": plans}}, indent=2))
        config = load_config(base / "config.json")
        sched = Scheduler(config)
        sched.scan_stream()

        print(f"docker smoke: {sh(*docker, 'version', '--format', '{{.Server.Version}} {{.Server.Arch}}')}, canary {url}")
        started = sh(*docker, "inspect", "-f", "{{.State.StartedAt}}", NAME)
        check("修复前 503", http(url) == 503, f"HTTP {http(url)}")
        dispatch(config, "smoke", "CONFIG_DISABLED", "failure", "t", "s", push=False)
        sched.scan_stream()
        r = (sched.drain() or [{}])[0]
        log = (base / "runs" / r.get("run_id", "x") / "agent.jsonl").read_text() if r else ""
        check("改配置 + docker restart + 探针 → repaired", r.get("outcome") == "repaired", r.get("outcome", ""))
        check("容器确实被重启", sh(*docker, "inspect", "-f", "{{.State.StartedAt}}", NAME) != started)
        check("修复后 200", http(url) == 200)
        check("docker logs 里的密钥已脱敏", "hunter2" not in log and "[REDACTED]" in log)
        check("配置文件属主权限保持 0640", (cfg.stat().st_mode & 0o777) == 0o640)

        sh(*docker, "stop", "-t", "2", NAME)
        dispatch(config, "smoke", "PROCESS_DOWN", "failure", "t", "s", push=False)
        sched.scan_stream()
        r = (sched.drain() or [{}])[0]
        check("容器停止 → docker start → repaired", r.get("outcome") == "repaired" and http(url) == 200, r.get("outcome", ""))

        pol, _ = load_policy(base / "policy.json")
        broker = Broker(pol, base / "runs" / "manual", {"service": "smoke", "fault_code": "CONFIG_DISABLED"})
        sha = broker.handle({"op": "read_config", "target": "canary", "config": "app"})["sha256"]
        before = cfg.read_bytes()
        try:
            broker.handle({"op": "patch_config", "target": "canary", "config": "app", "expected_sha256": sha,
                           "changes": {"enabled": False}})
            msg = "unexpected success"
        except BrokerError as exc:
            msg = str(exc)
        check("改坏 → 探针失败 → 恢复原文件、重启后 200", msg == "patch failed; original configuration restored"
              and cfg.read_bytes() == before and http(url) == 200, msg)
        try:
            broker.handle({"op": "restart", "target": "other"})
            denied = False
        except BrokerError:
            denied = True
        check("名单外目标被拒绝", denied)
        sched.close()
    finally:
        sh(*docker, "rm", "-f", NAME, check=False)
        if args.network:
            sh(*docker, "network", "rm", args.network, check=False)
    ok = all(x[1] for x in results)
    print(f"\n结果：{sum(x[1] for x in results)}/{len(results)} 项通过；canary 已删除")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
