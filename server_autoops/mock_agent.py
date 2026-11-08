#!/usr/bin/env python3
"""Scripted stand-in for the LLM agent.

It speaks the exact JSONL protocol of the dsh ``ops`` plugin over the broker's
Unix socket, so every request still passes the broker's authorisation checks.
Use it for the offline demo, for CI, and to dry-run a new policy before giving
it to a real model.

A plan is a list of ``ops`` requests. The string ``$sha256:<target>/<config>``
is replaced by the hash returned by the latest ``read_config`` of that config,
which is what ``patch_config`` needs for its compare-and-swap check. Without a
plan the mock agent only runs ``inventory`` and ``inspect`` on every target.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any


def call(sock_path: str, token: str, request: dict, timeout: float = 240) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(sock_path)
        conn.sendall((json.dumps({"token": token, "request": request}, ensure_ascii=False) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 262144:
                raise RuntimeError("broker response too large")
    reply = json.loads(data.decode("utf-8"))
    if not isinstance(reply, dict):
        raise RuntimeError("invalid broker reply")
    return reply


def _substitute(value: Any, hashes: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("$sha256:"):
        return hashes.get(value[len("$sha256:"):], "unknown")
    if isinstance(value, dict):
        return {k: _substitute(v, hashes) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, hashes) for v in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server-autoops mock-agent")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--event-file", required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("AUTOOPS_TOKEN", "")
    plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    hashes: dict[str, str] = {}
    log = lambda record: print(json.dumps(record, ensure_ascii=False), flush=True)  # noqa: E731
    inventory = call(args.socket, token, {"op": "inventory"})
    log({"step": "inventory", "reply": inventory})
    if plan is None:
        targets = [t["target"] for t in inventory.get("result", {}).get("targets", [])]
        plan = [{"op": "inspect", "target": t} for t in targets]
    summary = "模拟智能体：按脚本执行完毕"
    for index, step in enumerate(plan):
        if not isinstance(step, dict):
            continue
        if step.get("op") == "summary":
            summary = str(step.get("text", summary))
            continue
        request = _substitute(step, hashes)
        try:
            reply = call(args.socket, token, request)
        except (OSError, RuntimeError, ValueError) as exc:
            reply = {"ok": False, "error": f"transport: {type(exc).__name__}"}
        if reply.get("ok") and request.get("op") == "read_config":
            hashes[f"{request.get('target')}/{request.get('config')}"] = reply["result"].get("sha256", "")
        log({"step": index, "request": request, "reply": reply})
    final = call(args.socket, token, {"op": "finish", "summary": summary})
    log({"step": "finish", "reply": final})
    return 0


if __name__ == "__main__":
    sys.exit(main())
