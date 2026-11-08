#!/usr/bin/env python3
"""Real-dsh smoke test without a real model.

Runs the actual runner with ``agent.kind = dsh`` against a scripted, local
OpenAI-compatible endpoint. The fake model asks for ``ops inventory``, then
``ops inspect``, then answers in text. It checks that the model was offered
exactly one tool (``ops``), that the plugin reached the broker, and that the
broker produced the final verdict.

Run it where the Docker daemon can bind-mount --workdir and the agent container
can reach this process at http://<--host-name>:<--port> (e.g. both on one network):

  python3 scripts/dsh_smoke.py --workdir /path/shared --image local/server-autoops-agent:0.1.0 \
      --network sa-net --host-name sa-dsh-runner
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server_autoops.runner import run  # noqa: E402
from server_autoops.scheduler import load_config  # noqa: E402

SEEN: list[dict] = []


def _reply(step: int) -> dict:
    if step == 0:
        return {"tool": {"op": "inventory"}}
    if step == 1:
        return {"tool": {"op": "inspect", "target": "demo-web"}}
    return {"text": "诊断完成：目标容器在运行，无需改动。"}


class FakeLLM(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        tools = sorted(t.get("function", {}).get("name", t.get("name", "")) for t in body.get("tools", []) or [])
        tool_msgs = [m for m in body.get("messages", []) if m.get("role") == "tool"]
        SEEN.append({"tools": tools, "tool_results": [str(m.get("content"))[:400] for m in tool_msgs]})
        reply = _reply(len(tool_msgs))
        if "tool" in reply:
            delta = {"role": "assistant", "content": None, "tool_calls": [{"index": 0, "id": f"call_{len(tool_msgs)}",
                     "type": "function", "function": {"name": "ops",
                     "arguments": json.dumps({"request": json.dumps(reply["tool"])})}}]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": reply["text"]}
            finish = "stop"
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
            for c in chunks:
                c.update(id="fake", object="chat.completion.chunk", created=0, model=body.get("model", "fake"))
                self.wfile.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            msg = dict(delta)
            out = json.dumps({"id": "fake", "object": "chat.completion", "created": 0, "model": body.get("model", "fake"),
                              "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    def log_message(self, *_a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--network", required=True)
    ap.add_argument("--host-name", required=True, help="name under which the agent container reaches this process")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--docker-bin", default=shutil.which("docker") or "/usr/bin/docker")
    args = ap.parse_args()
    base = Path(args.workdir).resolve()
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), FakeLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = base / "web-state.json"
    state.write_text(json.dumps({"Running": True, "Health": "healthy"}))
    (base / "policy.json").write_text(json.dumps({
        "allow_simulated": True,
        "services": {"demo-web": {"driver": "simulated", "state_file": str(state), "mutable": True, "probe": {"kind": "health"}}},
        "faults": {"demo:UNHEALTHY": {"targets": ["demo-web"]}}}))
    env_file = base / "model.env"
    env_file.write_text("AUTOOPS_MODEL_API_KEY=fake-key-for-smoke\n")
    env_file.chmod(0o600)
    (base / "config.json").write_text(json.dumps({
        "root": str(base), "policy": str(base / "policy.json"), "timezone": "UTC+08:00",
        "agent_timeout": 300, "agent_grace_seconds": 60,
        "agent": {"kind": "dsh", "image": args.image, "docker_bin": args.docker_bin, "network": args.network,
                  "bridge_dir": str(base),
                  "model": {"base_url": f"http://{args.host_name}:{args.port}/v1", "model": "fake-model",
                            "env_file": str(env_file)}}}))
    config = load_config(base / "config.json")
    run_id = "dshsmoke"
    run_dir = Path(config["runs_dir"]) / run_id
    run_dir.mkdir(parents=True)
    event = {"service": "demo", "fault_code": "UNHEALTHY", "level": "failure", "summary": "smoke"}
    (run_dir / "event.json").write_text(json.dumps(event))
    result = run(config, event, run_id)
    server.shutdown()
    agent_log = (run_dir / "agent.jsonl").read_text(errors="replace") if (run_dir / "agent.jsonl").exists() else ""
    checks = [
        ("dsh 向模型只提供了一个工具 ops", bool(SEEN) and all(s["tools"] == ["ops"] for s in SEEN), str([s["tools"] for s in SEEN])),
        ("ops inventory 经插件到达 broker 并返回授权目标", any("demo-web" in r for s in SEEN for r in s["tool_results"]), ""),
        ("插件核对工具清单只剩 ops", 'AUTOOPS_TOOLS=["ops"]' in agent_log, ""),
        ("dsh 正常退出", result.get("agent_exit_code") == 0, f"exit={result.get('agent_exit_code')}"),
        ("结论由 broker 探针给出：healthy 且已核实", result.get("status") == "healthy" and result.get("verified") is True,
         f"{result.get('status')} verified={result.get('verified')}"),
        ("运行结束后没有残留智能体容器", subprocess.run([args.docker_bin, "inspect", "server-autoops-agent"],
                                             capture_output=True).returncode != 0, ""),
    ]
    for name, ok, detail in checks:
        print(f"  [{'通过' if ok else '失败'}] {name}" + (f" — {detail}" if detail else ""))
    ok = all(c[1] for c in checks)
    print(f"\n结果：{sum(c[1] for c in checks)}/{len(checks)} 项通过；模型请求 {len(SEEN)} 次")
    if not ok:
        print("--- agent.jsonl 尾部 ---\n" + agent_log[-3000:])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
