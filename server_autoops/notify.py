"""Notification back ends: Bark, an external command, a local JSONL file, or none.

The Bark device URL is a secret (anyone who has it can push to the phone), so it
is never stored in config.json: it is read from ``url_file`` (mode 0600) or from
an environment variable named by ``url_env``. Every notification uses the Bark
``passive`` level unless the operator explicitly maps a kind to another level.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

BARK_LEVELS = ("passive", "active", "timeSensitive")
NOTICE_KINDS = (
    "daily", "needs_scan", "breaker", "rate_limit", "policy", "stream_loss",
    "alert_failure", "alert_recovery", "alert_warning", "memory", "maintenance", "test",
)
DEFAULT_LEVEL = "passive"


class NotifyError(RuntimeError):
    """Notification configuration or delivery error (never contains the secret URL)."""


def validate_notify_config(raw: Any) -> dict[str, Any]:
    """Normalise the ``notify`` section; raise NotifyError with a precise message."""
    if raw is None:
        raw = {"kind": "none"}
    if not isinstance(raw, dict):
        raise NotifyError("notify must be an object")
    cfg = dict(raw)
    kind = cfg.setdefault("kind", "none")
    if kind not in ("none", "bark", "command", "file"):
        raise NotifyError("notify.kind must be one of none, bark, command, file")
    levels = cfg.setdefault("levels", {})
    if not isinstance(levels, dict):
        raise NotifyError("notify.levels must be an object")
    for key, value in levels.items():
        if key not in NOTICE_KINDS:
            raise NotifyError(f"notify.levels has unknown notification kind {key!r}")
        if value not in BARK_LEVELS:
            raise NotifyError(f"notify.levels.{key} must be one of {', '.join(BARK_LEVELS)}")
    default_level = cfg.setdefault("default_level", DEFAULT_LEVEL)
    if default_level not in BARK_LEVELS:
        raise NotifyError(f"notify.default_level must be one of {', '.join(BARK_LEVELS)}")
    group = cfg.setdefault("group", "server-autoops")
    if not isinstance(group, str) or not group or len(group) > 64:
        raise NotifyError("notify.group must be a short non-empty string")
    timeout = cfg.setdefault("timeout_seconds", 12)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise NotifyError("notify.timeout_seconds must be between 1 and 120")
    if kind == "bark":
        if "url" in cfg:
            raise NotifyError("do not put the Bark URL in config.json; use notify.url_file or notify.url_env")
        url_file, url_env = cfg.get("url_file"), cfg.get("url_env")
        if (url_file is None) == (url_env is None):
            raise NotifyError("notify kind bark needs exactly one of url_file or url_env")
        if url_file is not None and (not isinstance(url_file, str) or not os.path.isabs(url_file)):
            raise NotifyError("notify.url_file must be an absolute path")
        if url_env is not None and (not isinstance(url_env, str) or not url_env.replace("_", "").isalnum()):
            raise NotifyError("notify.url_env must be an environment variable name")
    elif kind == "command":
        argv = cfg.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
            raise NotifyError("notify.argv must be a non-empty list of strings")
        if not os.path.isabs(argv[0]):
            raise NotifyError("notify.argv[0] must be an absolute path (no PATH lookup)")
    elif kind == "file":
        path = cfg.get("path")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise NotifyError("notify.path must be an absolute path")
    return cfg


def level_for(cfg: dict[str, Any], kind: str) -> str:
    return cfg.get("levels", {}).get(kind) or cfg.get("default_level", DEFAULT_LEVEL)


def read_bark_url(cfg: dict[str, Any]) -> str:
    if cfg.get("url_env"):
        url = os.environ.get(cfg["url_env"], "").strip()
        if not url:
            raise NotifyError(f"environment variable {cfg['url_env']} is empty")
    else:
        path = Path(cfg["url_file"])
        try:
            st = path.stat()
            url = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise NotifyError(f"cannot read Bark URL file: {type(exc).__name__}") from exc
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise NotifyError("Bark URL file must not be readable by group/others (chmod 600)")
    if not url.startswith(("https://", "http://")) or any(ch.isspace() for ch in url):
        raise NotifyError("Bark URL must be a single http(s) URL")
    return url.rstrip("/")


def post_bark(url: str, title: str, body: str, group: str, level: str, timeout: float = 12) -> bool:
    if level not in BARK_LEVELS:
        raise NotifyError("unsupported Bark level")
    payload = json.dumps({"title": title, "body": body, "group": group, "level": level},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(64 * 1024).decode("utf-8", "ignore") or "{}")
            return 200 <= resp.status < 300 and data.get("code") == 200
    except (urllib.error.URLError, OSError, ValueError):
        # Never echo the exception text: it can contain the device URL.
        return False


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def make_notifier(raw_cfg: Any) -> Callable[[str, str, str], bool]:
    """Return ``send(title, body, kind) -> bool``; kind selects the Bark level."""
    cfg = validate_notify_config(raw_cfg)
    kind_name = cfg["kind"]

    def send(title: str, body: str, kind: str = "test") -> bool:
        level = level_for(cfg, kind)
        if kind_name == "none":
            return True
        if kind_name == "file":
            append_jsonl(Path(cfg["path"]), {"title": title, "body": body, "kind": kind,
                                             "level": level, "group": cfg["group"]})
            return True
        if kind_name == "command":
            data = json.dumps({"title": title, "body": body, "kind": kind, "level": level,
                               "group": cfg["group"]}, ensure_ascii=False).encode("utf-8")
            try:
                result = subprocess.run(cfg["argv"], input=data, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=float(cfg["timeout_seconds"]),
                                        check=False, shell=False)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return result.returncode == 0
        return post_bark(read_bark_url(cfg), title, body, cfg["group"], level, float(cfg["timeout_seconds"]))

    send.enabled = kind_name != "none"  # type: ignore[attr-defined]
    return send
