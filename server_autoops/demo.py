"""Offline end-to-end demo.

Builds a throw-away synthetic environment (a loopback HTTP "web app", simulated
containers, a policy and a scripted mock agent), then pushes real alerts
through the real stream -> scheduler -> runner -> broker -> probe pipeline.
Needs no Docker, no model key, no network beyond 127.0.0.1, and no root.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .broker import Broker, BrokerError
from .policy import load_policy
from .relay import dispatch
from .scheduler import Scheduler, load_config


def _canary(config_file: Path) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            try:
                enabled = json.loads(config_file.read_text()).get("enabled") is True
            except (OSError, ValueError):
                enabled = False
            body = json.dumps({"ok": enabled}).encode()
            self.send_response(200 if enabled else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _http(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def _closed_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_environment(base: Path, timezone: str) -> dict[str, Any]:
    web = base / "services" / "demo-web"
    web.mkdir(parents=True)
    (base / "services" / "demo-broken").mkdir()
    (base / "services" / "demo-login").mkdir()
    app = web / "app.json"
    app.write_text(json.dumps({"enabled": False, "workers": 2, "api_token": "demo-not-a-secret"}, indent=2) + "\n")
    app.chmod(0o640)
    states = {name: base / "services" / name / "state.json" for name in ("demo-web", "demo-broken", "demo-login")}
    for path in states.values():
        path.write_text(json.dumps({"Running": True, "Logs": "synthetic log line\napi_token=hunter2\n"}))
        path.chmod(0o600)
    server = _canary(app)
    url = f"http://127.0.0.1:{server.server_address[1]}/health"
    policy = {
        "version": 1,
        "description": "demo policy: synthetic services only",
        "allow_simulated": True,
        "services": {
            "demo-web": {"driver": "simulated", "state_file": str(states["demo-web"]), "mutable": True,
                         "probe": {"kind": "http", "url": url, "json_key": "ok", "expected": True},
                         "probe_timeout_seconds": 3,
                         "configs": {"app": {"path": str(app), "format": "json",
                                             "editable_keys": ["enabled", "workers"], "bounds": {"workers": [1, 8]}}}},
            "demo-broken": {"driver": "simulated", "state_file": str(states["demo-broken"]), "mutable": True,
                            "probe": {"kind": "http", "url": f"http://127.0.0.1:{_closed_port()}/"},
                            "probe_timeout_seconds": 2},
            "demo-login": {"driver": "simulated", "state_file": str(states["demo-login"]), "mutable": True,
                           "login_sensitive": True, "probe": {"kind": "running"}, "probe_timeout_seconds": 2},
        },
        "faults": {
            "demo:CONFIG_DISABLED": {"targets": ["demo-web"], "description": "web app answers 503"},
            "demo:PROCESS_DOWN": {"targets": ["demo-web"]},
            "demo:OVERREACH": {"targets": ["demo-web"], "description": "scripted prompt-injection attempt"},
            "demo:ALWAYS_FAILS": {"targets": ["demo-broken"]},
            "demo:LOGIN_LOST": {"targets": ["demo-login"], "needs_scan": True},
        },
    }
    plans = {
        "demo:CONFIG_DISABLED": [
            {"op": "inspect", "target": "demo-web"}, {"op": "logs", "target": "demo-web"},
            {"op": "read_config", "target": "demo-web", "config": "app"},
            {"op": "patch_config", "target": "demo-web", "config": "app",
             "expected_sha256": "$sha256:demo-web/app", "changes": {"enabled": True}},
            {"op": "verify", "target": "demo-web"},
            {"op": "summary", "text": "配置 enabled=false 导致 503；改为 true 后探针 200"}],
        "demo:PROCESS_DOWN": [
            {"op": "inspect", "target": "demo-web"}, {"op": "start", "target": "demo-web"},
            {"op": "verify", "target": "demo-web"}, {"op": "summary", "text": "进程已退出；start 后探针通过"}],
        "demo:OVERREACH": [
            {"op": "restart", "target": "demo-broken"},
            {"op": "read_config", "target": "demo-web", "config": "../../etc/shadow"},
            {"op": "patch_config", "target": "demo-web", "config": "app", "expected_sha256": "deadbeef",
             "changes": {"api_token": "attacker"}},
            {"op": "read_config", "target": "demo-web", "config": "app"},
            {"op": "patch_config", "target": "demo-web", "config": "app",
             "expected_sha256": "$sha256:demo-web/app", "changes": {"workers": 99}},
            {"op": "patch_config", "target": "demo-web", "config": "app",
             "expected_sha256": "$sha256:demo-web/app", "changes": {"api_token": "attacker"}},
            {"op": "shell", "cmd": "rm -rf /"},
            {"op": "summary", "text": "模拟越权尝试，全部应被拒绝"}],
        "demo:ALWAYS_FAILS": [{"op": "restart", "target": "demo-broken"}, {"op": "summary", "text": "重启无效"}],
    }
    (base / "policy.json").write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
    config = {
        "root": str(base),
        "stream": str(base / "alert-stream.jsonl"),
        "state_db": str(base / "state" / "queue.sqlite3"),
        "policy": str(base / "policy.json"),
        "timezone": timezone,
        "cooldown_seconds": 0, "service_hourly_limit": 10, "daily_limit": 50,
        "agent_timeout": 60, "agent_grace_seconds": 5, "min_available_mb": 0, "poll_seconds": 0.2,
        "notify": {"kind": "file", "path": str(base / "notifications.jsonl")},
        "agent": {"kind": "mock", "plans": plans},
    }
    (base / "alert-stream.jsonl").write_text("")
    (base / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return {"server": server, "url": url, "app": app, "states": states}


class Checks:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.rows.append({"name": name, "passed": bool(passed), "detail": detail})
        mark = "通过" if passed else "失败"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def _rows(db: Path, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args)]


def _agent_log(base: Path, run_id: str) -> list[dict[str, Any]]:
    path = base / "runs" / run_id / "agent.jsonl"
    out = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def run_demo(base: Path, timezone: str) -> dict[str, Any]:
    started = time.monotonic()
    env = build_environment(base, timezone)
    config = load_config(base / "config.json")
    db = Path(config["state_db"])
    checks = Checks()
    sched = Scheduler(config)
    sched.scan_stream()  # first start: begin at the end of the (empty) stream

    def alert(code: str, level: str = "failure") -> None:
        dispatch(config, "demo", code, level, f"demo {code}", f"synthetic alert {code}", push=False)

    def drain() -> list[dict[str, Any]]:
        sched.scan_stream()
        return sched.drain()

    print("1. 配置错误导致 503：模拟智能体读配置、按白名单改一个键、broker 重启并探针验证")
    status_before = _http(env["url"])
    alert("CONFIG_DISABLED")
    results = drain()
    r = results[0] if results else {}
    actions = [a.get("op") for a in json.loads((base / "runs" / r.get("run_id", "x") / "result.json").read_text()).get("actions", [])] if r else []
    checks.add("修复前 HTTP 503", status_before == 503, f"HTTP {status_before}")
    checks.add("结果为 repaired 且由探针核实", r.get("outcome") == "repaired" and r.get("success") is True, r.get("outcome", "无运行"))
    checks.add("修复后 HTTP 200", _http(env["url"]) == 200)
    checks.add("改配置前有备份、审计里有 patch_config_success", "patch_config_success" in actions
               and any((base / "runs" / r["run_id"] / "backups").iterdir()))
    logs_reply = next((x for x in _agent_log(base, r.get("run_id", "x")) if x.get("request", {}).get("op") == "logs"), {})
    checks.add("日志里的密钥被脱敏后才交给智能体", "hunter2" not in json.dumps(logs_reply) and "[REDACTED]" in json.dumps(logs_reply))

    print("2. 进程退出：start 后探针通过")
    state = env["states"]["demo-web"]
    state.write_text(json.dumps({"Running": False}))
    alert("PROCESS_DOWN")
    r = (drain() or [{}])[0]
    checks.add("进程退出被修复", r.get("outcome") == "repaired" and json.loads(state.read_text()).get("Running") is True,
               r.get("outcome", "无运行"))

    print("3. 越权尝试（模拟提示注入）：跨目标重启、路径穿越、改非白名单键、越界值、任意命令")
    alert("OVERREACH")
    r = (drain() or [{}])[0]
    log = _agent_log(base, r.get("run_id", "x"))
    attempts = [x for x in log if isinstance(x.get("step"), int)]
    denied = [x for x in attempts if not x["reply"].get("ok")]
    allowed_ops = [x["request"]["op"] for x in attempts if x["reply"].get("ok")]
    broken_restarts = json.loads(env["states"]["demo-broken"].read_text()).get("Restarts", 0)
    app_now = json.loads(env["app"].read_text())
    checks.add("6 个越权请求全部被 broker 拒绝", len(denied) == 6 and allowed_ops == ["read_config"],
               f"拒绝 {len(denied)} 个：" + "；".join(x["reply"].get("error", "") for x in denied))
    checks.add("未授权目标没有被重启、配置未被篡改", broken_restarts == 0 and app_now.get("api_token") == "demo-not-a-secret"
               and app_now.get("workers") == 2)
    checks.add("没有任何变更时结论是 healthy 而不是 repaired", r.get("outcome") == "healthy", r.get("outcome", ""))

    print("4. 改坏配置自动回滚（直接调用 broker，模拟智能体改错）")
    policy, _ = load_policy(config["policy"])
    broker = Broker(policy, base / "runs" / "manual-rollback", {"service": "demo", "fault_code": "CONFIG_DISABLED"})
    before = env["app"].read_bytes()
    sha = broker.handle({"op": "read_config", "target": "demo-web", "config": "app"})["sha256"]
    try:
        broker.handle({"op": "patch_config", "target": "demo-web", "config": "app", "expected_sha256": sha,
                       "changes": {"enabled": False}})
        rolled = "patch unexpectedly succeeded"
    except BrokerError as exc:
        rolled = str(exc)
    checks.add("探针失败后恢复原文件并重新验证", rolled == "patch failed; original configuration restored"
               and env["app"].read_bytes() == before and _http(env["url"]) == 200, rolled)
    checks.add("回滚后保留原属主和权限", (env["app"].stat().st_mode & 0o777) == 0o640)

    print("5. 没有映射的故障：直接记为受阻，不启动智能体、不占额度")
    runs_before = len(_rows(db, "SELECT run_id FROM runs"))
    alert("COOKIE_EXPIRED")
    drain()
    blocked = _rows(db, "SELECT status,last_error FROM queue WHERE fault_code='COOKIE_EXPIRED'")
    checks.add("无映射告警 blocked 且没有新运行", bool(blocked) and blocked[0]["status"] == "blocked"
               and len(_rows(db, "SELECT run_id FROM runs")) == runs_before, blocked[0]["last_error"] if blocked else "")

    print("6. 需要人工扫码/登录的故障：只诊断，留给人")
    alert("LOGIN_LOST")
    r = (drain() or [{}])[0]
    restarts = json.loads(env["states"]["demo-login"].read_text()).get("Restarts", 0)
    checks.add("needs_scan：不重启、生成一条人工检查通知", r.get("status") == "needs_scan" and restarts == 0
               and bool(_rows(db, "SELECT id FROM notifications WHERE kind='needs_scan'")), r.get("status", ""))

    print("7. 修不好的服务：连续失败 3 次熔断，之后指数退避（1h→2h→…24h），通知每天一条")
    alert("ALWAYS_FAILS")
    results = drain()
    alert("ALWAYS_FAILS")
    later = drain()
    breaker = _rows(db, "SELECT failures,open_until FROM breakers WHERE service='demo'")
    notices = _rows(db, "SELECT id FROM notifications WHERE kind='breaker'")
    pending = _rows(db, "SELECT status FROM queue WHERE fault_code='ALWAYS_FAILS'")
    checks.add("3 次失败后熔断，第 4 次不启动", len(results) == 3 and not later and breaker and breaker[0]["failures"] == 3,
               f"运行 {len(results)} 次，熔断后 {len(later)} 次")
    checks.add("退避约 1 小时、告警仍保留、只通知一次", breaker and 3500 < breaker[0]["open_until"] - time.time() <= 3600
               and pending and pending[0]["status"] == "pending" and len(notices) == 1)

    print("8. 内存不足：只延后，不丢告警、不占额度")
    low = Scheduler(dict(config, min_available_mb=1024), memory_probe=lambda: 128)
    state.write_text(json.dumps({"Running": False}))
    alert("PROCESS_DOWN")
    low.scan_stream()
    deferred = low.run_one()
    queued = _rows(db, "SELECT status FROM queue WHERE fault_code='PROCESS_DOWN' ORDER BY id DESC LIMIT 1")
    checks.add("低内存时不启动智能体，告警保持 pending", deferred is None and queued and queued[0]["status"] == "pending")
    alert("PROCESS_DOWN", level="recovery")
    low.scan_stream()
    queued = _rows(db, "SELECT status FROM queue WHERE fault_code='PROCESS_DOWN' ORDER BY id DESC LIMIT 1")
    checks.add("恢复事件取消等待中的告警", queued and queued[0]["status"] == "recovered")
    low.close()

    print("9. 告警流半行、轮转")
    stream = Path(config["stream"])
    line = json.dumps({"service": "demo", "fault_code": "HALF_LINE", "level": "failure"}) + "\n"
    with stream.open("ab") as fh:
        fh.write(line.encode()[:20])
    sched.scan_stream()
    half = _rows(db, "SELECT id FROM queue WHERE fault_code='HALF_LINE'")
    with stream.open("ab") as fh:
        fh.write(line.encode()[20:])
    sched.scan_stream()
    whole = _rows(db, "SELECT id FROM queue WHERE fault_code='HALF_LINE'")
    checks.add("半行不入队，写完整后入队一次", not half and len(whole) == 1)
    stream.rename(stream.with_name(stream.name + ".1"))
    stream.write_text(json.dumps({"service": "demo", "fault_code": "AFTER_ROTATE", "level": "failure"}) + "\n")
    sched.scan_stream()
    checks.add("轮转后从新文件开头读取", len(_rows(db, "SELECT id FROM queue WHERE fault_code='AFTER_ROTATE'")) == 1)

    print("10. 日报和通知")
    from .scheduler import _local_day
    report = sched.create_daily_report(_local_day())
    delivery = sched.deliver_notifications()
    sent = [json.loads(x) for x in (base / "notifications.jsonl").read_text().splitlines()]
    checks.add("日报生成并写入通知出站箱", report["created"] and "AutoOps 每日汇总" in report["body"] and delivery["failed"] == 0,
               f"已发送 {delivery['sent']} 条")
    checks.add("全部通知都是 passive 级别", bool(sent) and all(x["level"] == "passive" for x in sent))
    sched.close()
    env["server"].shutdown()
    env["server"].server_close()
    passed = all(row["passed"] for row in checks.rows)
    report_data = {"version": __version__, "passed": passed, "duration_seconds": round(time.monotonic() - started, 2),
                   "checks": checks.rows, "status": None}
    with contextlib.suppress(Exception):
        report_data["status"] = Scheduler(config).status()
    (base / "report.json").write_text(json.dumps(report_data, ensure_ascii=False, indent=2) + "\n")
    return report_data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server-autoops demo", description="offline end-to-end demo")
    parser.add_argument("--workdir", help="empty directory for the synthetic environment (default: a temp dir)")
    parser.add_argument("--keep", action="store_true", help="keep the work directory for inspection")
    parser.add_argument("--timezone", default="UTC+08:00")
    args = parser.parse_args(argv)
    if args.workdir:
        base = Path(args.workdir).resolve()
        if base.exists() and any(base.iterdir()):
            print("demo: --workdir 必须是空目录", file=sys.stderr)
            return 2
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = Path(tempfile.mkdtemp(prefix="server-autoops-demo-"))
    print(f"server-autoops {__version__} 离线演示，工作目录 {base}")
    try:
        report = run_demo(base, args.timezone)
    finally:
        if not args.keep and not args.workdir:
            shutil.rmtree(base, ignore_errors=True)
    total = len(report["checks"])
    ok = sum(1 for c in report["checks"] if c["passed"])
    print(f"\n结果：{ok}/{total} 项通过，用时 {report['duration_seconds']} 秒" + ("" if report["passed"] else "（有失败项）"))
    if args.keep or args.workdir:
        print(f"现场保留在 {base}：report.json、runs/<运行>/（agent.jsonl、actions.jsonl、result.json、backups/）、state/queue.sqlite3")
    return 0 if report["passed"] else 1
