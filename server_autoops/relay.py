"""Alert relay: append one alert to the stream and optionally push it.

Watchdogs call ``server-autoops alert ...`` (or ``relay.dispatch`` from Python).
The stream is the only input of the scheduler; the push is independent, so a
broken notification channel never loses an alert.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from .notify import append_jsonl, make_notifier
from .timeutil import resolve_timezone

LEVELS = ("failure", "recovery", "warning")


def build_event(service: str, fault_code: str, level: str, title: str, summary: str, details: str = "",
                tz_name: str = "UTC", auto_recovery_handled: bool = False, push_sent: bool = False) -> dict[str, Any]:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    for label, value in (("service", service), ("fault_code", fault_code)):
        if not value or not value.strip() or "\x00" in value:
            raise ValueError(f"{label} must be non-empty")
    now = _dt.datetime.now(_dt.timezone.utc)
    return {
        "timestamp": now.timestamp(),
        "iso_time": now.astimezone(resolve_timezone(tz_name)).isoformat(),
        "service": service.strip(), "fault_code": fault_code.strip(), "level": level,
        "title": title, "summary": summary, "details": details,
        "push_sent": push_sent, "auto_recovery_handled": bool(auto_recovery_handled),
    }


def dispatch(config: dict[str, Any], service: str, fault_code: str, level: str, title: str, summary: str,
             details: str = "", push: bool = True, auto_recovery_handled: bool = False) -> dict[str, Any]:
    push_ok = None
    if push:
        body = "\n".join(part for part in (summary.strip(), details.strip()) if part)
        try:
            push_ok = make_notifier(config.get("notify"))(title, body, f"alert_{level}")
        except Exception as exc:  # noqa: BLE001 - a push failure must not lose the alert
            sys.stderr.write(f"server-autoops alert: push failed: {type(exc).__name__}\n")
            push_ok = False
    event = build_event(service, fault_code, level, title, summary, details, config.get("timezone", "UTC"),
                        auto_recovery_handled, bool(push_ok))
    stream_ok = True
    try:
        append_jsonl(Path(config["stream"]), event)
    except OSError as exc:
        sys.stderr.write(f"server-autoops alert: stream write failed: {type(exc).__name__}\n")
        stream_ok = False
    return {"ok": stream_ok and push_ok is not False, "stream_ok": stream_ok, "push_ok": push_ok, "record": event}


def main(argv: list[str] | None = None) -> int:
    from .scheduler import SchedulerError, load_config
    parser = argparse.ArgumentParser(prog="server-autoops alert", description="append an alert to the stream")
    parser.add_argument("--config", default=os.environ.get("AUTOOPS_CONFIG", "/etc/server-autoops/config.json"))
    parser.add_argument("--service", required=True, help="watchdog / component name, e.g. web")
    parser.add_argument("--fault-code", required=True, help="fault code, e.g. UNHEALTHY; several may be space separated")
    parser.add_argument("--level", choices=LEVELS, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--details", default="")
    parser.add_argument("--no-push", action="store_true", help="only write the stream, do not notify")
    parser.add_argument("--auto-recovery-handled", action="store_true",
                        help="the watchdog already repaired it; the scheduler will not start an agent")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        result = dispatch(config, args.service, args.fault_code, args.level, args.title, args.summary,
                          args.details, push=not args.no_push, auto_recovery_handled=args.auto_recovery_handled)
    except (SchedulerError, OSError, ValueError) as exc:
        print(f"server-autoops alert: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1
