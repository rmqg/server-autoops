#!/usr/bin/env python3
"""Generic application watchdog example: container running + optional HTTP health check.

Run it every minute (cron, systemd timer or procd). After --fail-threshold consecutive
failures it writes ONE failure alert (``<service>:<CODE>``) through ``server-autoops alert``;
when the check passes again it writes one recovery alert. State lives in --state-file.

  container_http_watchdog.py --service app --container app-web \\
      --health-url http://127.0.0.1:8080/healthz --json-key ok \\
      --down-code WEB_DOWN --state-file /var/lib/server-autoops/watchdog-web.json

The matching policy entry is ``faults["app:WEB_DOWN"]``. This script only observes:
it never restarts anything itself, so the scheduler is the only component that acts.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path


def container_running(docker: str, host: str, name: str) -> bool:
    try:
        out = subprocess.run([docker, "-H", host, "inspect", "-f", "{{.State.Running}}", name],
                             capture_output=True, text=True, timeout=8, check=False)
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def http_ok(url: str, json_key: str | None) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as resp:
            if not 200 <= resp.status < 300:
                return False
            if not json_key:
                return True
            value = json.loads(resp.read(65536).decode("utf-8"))
            for part in json_key.split("."):
                value = value[part]
            return value is True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def alert(cmd: list[str], service: str, code: str, level: str, title: str, summary: str) -> None:
    subprocess.run(cmd + ["--service", service, "--fault-code", code, "--level", level,
                          "--title", title, "--summary", summary], timeout=30, check=False)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--service", required=True, help="alert service name (first half of the policy fault key)")
    p.add_argument("--container", required=True)
    p.add_argument("--docker", default="/usr/bin/docker")
    p.add_argument("--docker-host", default="unix:///var/run/docker.sock")
    p.add_argument("--health-url")
    p.add_argument("--json-key", help="dotted key that must be true in the JSON body, e.g. ok")
    p.add_argument("--down-code", default="DOWN", help="fault code when the container is not running")
    p.add_argument("--unhealthy-code", default="UNHEALTHY", help="fault code when the HTTP check fails")
    p.add_argument("--fail-threshold", type=int, default=2)
    p.add_argument("--state-file", required=True)
    p.add_argument("--alert-cmd", default="server-autoops alert", help="command used to write alerts")
    args = p.parse_args()

    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{state_path}.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        if not container_running(args.docker, args.docker_host, args.container):
            code, why = args.down_code, f"容器 {args.container} 没有运行"
        elif args.health_url and not http_ok(args.health_url, args.json_key):
            code, why = args.unhealthy_code, f"{args.health_url} 健康检查失败"
        else:
            code, why = "", ""
        cmd = args.alert_cmd.split()
        if code:
            state["fail_count"] = int(state.get("fail_count", 0)) + 1
            if state["fail_count"] >= args.fail_threshold and not state.get("failed"):
                alert(cmd, args.service, code, "failure", f"{args.service} 异常", why)
                state.update(failed=True, fault_code=code, alerted_at=time.time())
        else:
            if state.get("failed"):
                alert(cmd, args.service, f"{state.get('fault_code', args.down_code)}_RECOVERED", "recovery",
                      f"{args.service} 已恢复", "检查重新通过")
            state = {"fail_count": 0, "failed": False}
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
