#!/usr/bin/env python3
"""Durable, single-worker scheduler for the autoops runner.

The stream, queue, execution ledger, budgets, breaker state and notification
outbox all live in SQLite so an interrupted process can resume without losing
an alert. Importing this module has no side effects; use ``main`` for the CLI.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as _dt
import fcntl
import json
import os
import signal
import sqlite3
import math
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from .timeutil import resolve_timezone, zone_label
from .policy import validate_policy
from .notify import NotifyError, make_notifier, validate_notify_config

LOCAL_TZ = resolve_timezone("UTC")


def set_timezone(name: str) -> None:
    """Select the zone used for budget days, breaker notices and daily reports."""
    global LOCAL_TZ
    LOCAL_TZ = resolve_timezone(name)
MAX_LINE_BYTES = 1024 * 1024
READ_BYTES_PER_SOURCE = 1024 * 1024
OUTPUT_TAIL_BYTES = 1024 * 1024
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_SECONDS = 3600          # first open; doubles on every further failure
BREAKER_MAX_COOLDOWN_SECONDS = 24 * 3600
MAX_SPLIT_CODES = 16
RETENTION_INTERVAL_SECONDS = 6 * 3600
UNMAPPED_REASON = "no policy mapping; agent not started"
OUTCOME_LABELS = {
    "repaired": "已修复", "healthy": "本来健康",
    "repaired_unverified": "已处理(容器在跑,业务未验证)", "healthy_unverified": "容器在跑(业务未验证)",
    "failed": "失败", "needs_scan": "需人工", "blocked": "受阻", "interrupted": "中断", "running": "运行中",
}
NOTIFICATION_LEASE_SECONDS = 120
NOTIFICATION_RETRY_MAX_SECONDS = 6 * 3600


class SchedulerError(RuntimeError):
    """Expected scheduler/configuration error suitable for a concise CLI error."""


class LockBusy(SchedulerError):
    """Another scheduler worker currently owns the single-worker lock."""


def _now() -> float:
    return time.time()


def _utc_iso(value: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(_now() if value is None else value, _dt.timezone.utc).isoformat()


def _local_day(value: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(_now() if value is None else value, LOCAL_TZ).date().isoformat()


def _local_clock(value: float) -> str:
    return _dt.datetime.fromtimestamp(value, LOCAL_TZ).strftime("%Y-%m-%d %H:%M") + f"（{zone_label(LOCAL_TZ)}）"


def _next_local_midnight(value: float | None = None) -> float:
    now = _dt.datetime.fromtimestamp(_now() if value is None else value, LOCAL_TZ)
    tomorrow = now.date() + _dt.timedelta(days=1)
    return _dt.datetime.combine(tomorrow, _dt.time.min, LOCAL_TZ).timestamp()


def _safe_text(value: Any, limit: int = 240) -> str:
    text = _redact_string(str(value if value is not None else ""))
    text = "".join(ch if ch >= " " and ch not in "\x7f\u2028\u2029" else " " for ch in text)
    return " ".join(text.split())[:limit]

def _safe_body(value: str, limit: int = 3000) -> str:
    text = _redact_string(str(value))
    text = "".join(ch for ch in text if ch in "\n\t" or (ch >= " " and ch not in "\x7f\u2028\u2029"))
    return text[:limit]


_SECRET_ASSIGNMENT = re.compile(r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization|credential|private[_-]?key)\b\s*[:=]\s*)([^\s,;]+)")
_BEARER_SECRET = re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/-]+=*")
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_KNOWN_TOKEN = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,})\b")
_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE | re.DOTALL)


def _redact_string(value: str) -> str:
    value = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", value)
    value = _BEARER_SECRET.sub(r"\1 [REDACTED]", value)
    value = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
    value = _URL_USERINFO.sub(r"\1[REDACTED]@", value)
    return _KNOWN_TOKEN.sub("[REDACTED]", value)


def _redact_payload(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "[REDACTED: nesting limit]"
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, list):
        return [_redact_payload(item, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        clean = {}
        for key, item in list(value.items())[:200]:
            key_text = str(key)
            if re.search(r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|authorization|credential|private[_-]?key)", key_text):
                clean[key_text] = "[REDACTED]"
            else:
                clean[key_text] = _redact_payload(item, depth + 1)
        return clean
    return value


def _json_object(data: str | bytes, label: str) -> dict[str, Any]:
    try:
        obj = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchedulerError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise SchedulerError(f"{label} must be a JSON object")
    return obj


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve(strict=True)
    try:
        config = _json_object(config_path.read_bytes(), "config")
    except OSError as exc:
        raise SchedulerError(f"cannot read config: {exc}") from exc
    config["config_path"] = str(config_path)
    return normalize_config(config)


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and fill defaults; shared by load_config, doctor and the test-suite."""
    config = dict(config)
    root = config.get("root")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise SchedulerError("config field 'root' must be an absolute path")
    config.setdefault("stream", os.path.join(root, "alert-stream.jsonl"))
    config.setdefault("state_db", os.path.join(root, "state", "queue.sqlite3"))
    config.setdefault("runs_dir", os.path.join(root, "runs"))
    for key in ("stream", "state_db", "policy", "runs_dir"):
        value = config.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            raise SchedulerError(f"config field {key!r} must be an absolute path")
        config[key] = str(Path(value).resolve())
    config["root"] = str(Path(root).resolve())
    config.setdefault("timezone", "UTC")
    try:
        set_timezone(config["timezone"])
    except ValueError as exc:
        raise SchedulerError(f"config field 'timezone': {exc}") from exc
    try:
        config["notify"] = validate_notify_config(config.get("notify"))
    except NotifyError as exc:
        raise SchedulerError(str(exc)) from exc
    from .runner import validate_agent_config
    try:
        config["agent"] = validate_agent_config(config.get("agent"), config["root"])
    except ValueError as exc:
        raise SchedulerError(f"config field 'agent': {exc}") from exc
    config.setdefault("cooldown_seconds", 300)
    config.setdefault("service_hourly_limit", 3)
    config.setdefault("daily_limit", 20)
    config.setdefault("agent_timeout", 600)
    config.setdefault("agent_grace_seconds", 240)
    config.setdefault("breaker_failures", BREAKER_FAILURES)
    config.setdefault("breaker_base_seconds", BREAKER_COOLDOWN_SECONDS)
    config.setdefault("breaker_max_seconds", BREAKER_MAX_COOLDOWN_SECONDS)
    config.setdefault("min_available_mb", 1024)
    config.setdefault("poll_seconds", 0.5)
    config.setdefault("daily_hour", 8)
    config.setdefault("notifications_enabled", config["notify"]["kind"] != "none")
    if not isinstance(config["notifications_enabled"], bool):
        raise SchedulerError("config field 'notifications_enabled' must be boolean")
    integer_fields = {"cooldown_seconds", "service_hourly_limit", "daily_limit", "agent_timeout", "min_available_mb",
                      "agent_grace_seconds", "breaker_failures", "breaker_base_seconds", "breaker_max_seconds"}
    for key in ("cooldown_seconds", "service_hourly_limit", "daily_limit", "agent_timeout", "min_available_mb", "poll_seconds",
                "agent_grace_seconds", "breaker_failures", "breaker_base_seconds", "breaker_max_seconds"):
        if isinstance(config[key], bool):
            raise SchedulerError(f"config field {key!r} must be numeric")
        try:
            number = float(config[key])
        except (TypeError, ValueError) as exc:
            raise SchedulerError(f"config field {key!r} must be numeric") from exc
        if not math.isfinite(number) or number < 0 or (key in integer_fields and not number.is_integer()):
            raise SchedulerError(f"config field {key!r} is out of range")
        if key in ("agent_timeout", "poll_seconds", "breaker_failures", "breaker_base_seconds", "breaker_max_seconds") and number <= 0:
            raise SchedulerError(f"config field {key!r} must be greater than zero")
        config[key] = int(number) if key in integer_fields else number
    try:
        config["daily_hour"] = int(config["daily_hour"])
    except (TypeError, ValueError) as exc:
        raise SchedulerError("config field 'daily_hour' must be an integer") from exc
    if not 0 <= config["daily_hour"] <= 23:
        raise SchedulerError("config field 'daily_hour' must be between 0 and 23")
    config.setdefault("retention_days", 90)
    if isinstance(config["retention_days"], bool) or not isinstance(config["retention_days"], int) or config["retention_days"] < 1:
        raise SchedulerError("config field 'retention_days' must be a positive integer")
    return config


def breaker_backoff_seconds(failures: int, threshold: int = BREAKER_FAILURES,
                            base: float = BREAKER_COOLDOWN_SECONDS, cap: float = BREAKER_MAX_COOLDOWN_SECONDS) -> float:
    """base at the opening failure, then x2, x4 ... capped; never gives up (defaults 1h -> 24h)."""
    exponent = max(0, int(failures) - int(threshold))
    return float(min(cap, base * (2 ** min(exponent, 16))))


def available_memory_mb() -> int:
    """Return conservative available RAM in MiB; unknown means zero (fail closed)."""
    try:
        with open("/proc/meminfo", "rt", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return max(0, int(line.split()[1]) // 1024)
    except (OSError, ValueError, IndexError):
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return max(0, int(pages * page_size // (1024 * 1024)))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return 0



class FileLock:
    """Advisory cross-process exclusive lock, released on process exit."""
    def __init__(self, path: str):
        self.path = path
        self._file = None

    def acquire(self) -> "FileLock":
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            self._file = None
            raise LockBusy("scheduler worker is already running") from exc
        return self

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()


class Scheduler:
    """Own the durable queue and execute at most one runner process at a time."""
    def __init__(self, config: dict[str, Any], *, clock: Callable[[], float] = _now,
                 memory_probe: Callable[[], int] = available_memory_mb,
                 runner_executor: Callable[..., dict[str, Any]] | None = None,
                 notifier: Callable[[str, str, str], Any] | None = None):
        self.config = dict(config)
        self.clock = clock
        self.memory_probe = memory_probe
        self.runner_executor = runner_executor or self._execute_runner
        self.config.setdefault("timezone", "UTC")
        set_timezone(self.config["timezone"])
        self.notifier = notifier or make_notifier(self.config.get("notify"))
        self.db_path = Path(self.config["state_db"])
        self.root = Path(self.config["root"])
        self.stream_path = Path(self.config["stream"])
        self.run_root = Path(self.config.get("runs_dir") or self.root / "runs")
        self.error_log = self.db_path.with_suffix(self.db_path.suffix + ".errors.jsonl")
        self.lock_path = str(self.db_path.with_suffix(self.db_path.suffix + ".lock"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self._open_sources: dict[str, Any] = {}
        self._open_source_names: dict[str, str] = {}
        self._stop_signal: int | None = None
        self._policy_cache: tuple[tuple[int, int, int], dict[str, Any]] | None = None
        self._initialize_db()

    def _load_policy(self) -> dict[str, Any] | None:
        """Read the root-managed policy (cached by inode/mtime/size); None when unreadable."""
        path = Path(self.config["policy"])
        try:
            st = path.stat()
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            if self._policy_cache is not None and self._policy_cache[0] == key:
                return self._policy_cache[1]
            raw = _json_object(path.read_bytes(), "policy")
        except (OSError, SchedulerError):
            return None
        data, errors, _warnings = validate_policy(raw)
        if errors:
            # Fail closed: an invalid policy authorises nothing and starts no agent.
            return None
        self._policy_cache = (key, data)
        return data

    @staticmethod
    def _fault_spec(policy: dict[str, Any], service: str, code: str) -> dict[str, Any] | None:
        """Mirror broker target resolution: a fault is mapped only if it names at least one known service."""
        faults, services = policy.get("faults"), policy.get("services")
        if not isinstance(faults, dict) or not isinstance(services, dict):
            return None
        fault = faults.get(f"{service}:{code}")
        if not isinstance(fault, dict) or not isinstance(fault.get("targets"), list):
            return None
        if not any(isinstance(name, str) and isinstance(services.get(name), dict) for name in fault["targets"]):
            return None
        return fault

    def _expand_codes(self, event: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Split watchdog-combined codes ("a\\nb", "a b ") and apply policy aliases; None = keep as is."""
        if event.get("split_from") is not None:
            return None
        service = event.get("service", event.get("service_name"))
        code = event.get("fault_code", event.get("code", event.get("fault")))
        if not isinstance(service, str) or not isinstance(code, str):
            return None
        service, raw = service.strip(), code.strip()
        parts = list(dict.fromkeys(raw.split()))
        policy = self._load_policy() or {}
        aliases = policy.get("code_aliases") if isinstance(policy.get("code_aliases"), dict) else {}
        expanded: list[str] = []
        for part in parts:
            alias = aliases.get(f"{service}:{part}")
            if (isinstance(alias, list) and alias
                    and all(isinstance(item, str) and item.strip() and not any(ch.isspace() for ch in item.strip()) for item in alias)):
                expanded.extend(item.strip() for item in alias)
            else:
                expanded.append(part)
        expanded = list(dict.fromkeys(expanded))
        if not expanded or expanded == [raw]:
            return None
        if len(expanded) > MAX_SPLIT_CODES:
            raise SchedulerError("too many combined fault codes")
        symptoms: list[str] = []
        if event.get("level") == "failure" and policy:
            # A scan-required login loss explains the other QQ checks; queue only the scan fault.
            scan = [item for item in expanded if (self._fault_spec(policy, service, item) or {}).get("needs_scan") is True]
            if scan:
                symptoms = [item for item in expanded if item not in scan]
                expanded = scan
        out = []
        for item in expanded:
            part = {key: value for key, value in event.items() if key not in ("fault_code", "code", "fault")}
            part["fault_code"] = item
            part["split_from"] = raw
            if symptoms:
                part["split_symptoms"] = symptoms
            out.append(part)
        return out


    def close(self) -> None:
        for stream in self._open_sources.values():
            with contextlib.suppress(OSError):
                stream.close()
        self._open_sources.clear()
        self._open_source_names.clear()

    @contextlib.contextmanager
    def _termination_handlers(self) -> Iterable[None]:
        previous: dict[int, Any] = {}

        def request_stop(signum: int, _frame: Any) -> None:
            self._stop_signal = signum

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
            except ValueError:
                previous.pop(signum, None)
        self._stop_signal = None
        try:
            yield
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    fault_code TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','resolved','needs_scan','blocked','recovered')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    due_at REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    merged_count INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS queue_ready ON queue(status,due_at,id);
                CREATE INDEX IF NOT EXISTS queue_fault ON queue(service,fault_code,status);
                CREATE TABLE IF NOT EXISTS cursors (
                    source_key TEXT PRIMARY KEY,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    offset INTEGER NOT NULL,
                    pending BLOB NOT NULL DEFAULT X'',
                    discarding INTEGER NOT NULL DEFAULT 0,
                    eof_size INTEGER NOT NULL DEFAULT -1,
                    eof_checks INTEGER NOT NULL DEFAULT 0,
                    checkpoint BLOB NOT NULL DEFAULT X'',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quarantines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    path TEXT NOT NULL,
                    raw_b64 TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    logged INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    queue_id INTEGER NOT NULL,
                    service TEXT NOT NULL,
                    fault_code TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    outcome TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    exit_code INTEGER,
                    summary TEXT,
                    output_path TEXT NOT NULL,
                    FOREIGN KEY(queue_id) REFERENCES queue(id)
                );
                CREATE INDEX IF NOT EXISTS runs_service_started ON runs(service,started_at);
                CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at);
                CREATE TABLE IF NOT EXISTS cooldowns (
                    service TEXT NOT NULL,
                    fault_code TEXT NOT NULL,
                    until_at REAL NOT NULL,
                    PRIMARY KEY(service,fault_code)
                );
                CREATE TABLE IF NOT EXISTS breakers (
                    service TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL DEFAULT 0,
                    open_until REAL NOT NULL DEFAULT 0,
                    opened_at REAL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','suppressed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    not_before REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    sent_at REAL
                );
                CREATE INDEX IF NOT EXISTS notifications_due ON notifications(state,not_before,id);
                CREATE TABLE IF NOT EXISTS daily_reports (
                    day TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    notification_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            cursor_columns = {row[1] for row in conn.execute("PRAGMA table_info(cursors)")}
            if "eof_size" not in cursor_columns:
                conn.execute("ALTER TABLE cursors ADD COLUMN eof_size INTEGER NOT NULL DEFAULT -1")
            if "eof_checks" not in cursor_columns:
                conn.execute("ALTER TABLE cursors ADD COLUMN eof_checks INTEGER NOT NULL DEFAULT 0")
            if "checkpoint" not in cursor_columns:
                conn.execute("ALTER TABLE cursors ADD COLUMN checkpoint BLOB NOT NULL DEFAULT X''")
            os.chmod(self.db_path, 0o600)
        finally:
            conn.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterable[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _quarantine(self, conn: sqlite3.Connection, path: str, raw: bytes, reason: str) -> None:
        safe_raw = _redact_string(raw[:MAX_LINE_BYTES].decode("utf-8", "replace")).encode("utf-8")
        conn.execute(
            "INSERT INTO quarantines(created_at,path,raw_b64,reason) VALUES(?,?,?,?)",
            (self.clock(), path, base64.b64encode(safe_raw).decode("ascii"), _safe_text(reason, 300)),
        )

    def _flush_quarantines(self) -> None:
        with self._transaction() as conn:
            rows = conn.execute("SELECT id,created_at,path,raw_b64,reason FROM quarantines WHERE logged=0 ORDER BY id LIMIT 1000").fetchall()
            if not rows:
                return
            self.error_log.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.error_log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                for row in rows:
                    record = {"id": row["id"], "at": _utc_iso(row["created_at"]), "path": row["path"],
                              "raw_b64": row["raw_b64"], "reason": row["reason"]}
                    payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("short quarantine log write")
                        view = view[written:]
                    conn.execute("UPDATE quarantines SET logged=1 WHERE id=?", (row["id"],))
            finally:
                os.close(fd)

    @staticmethod
    def _source_key(device: int, inode: int) -> str:
        return f"{device}:{inode}"

    def _find_tracked_files(self, rows: list[sqlite3.Row]) -> dict[str, Path]:
        wanted = {(int(row["device"]), int(row["inode"])): row["source_key"] for row in rows}
        found: dict[str, Path] = {}
        try:
            with os.scandir(self.stream_path.parent) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=True)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    key = wanted.get((st.st_dev, st.st_ino))
                    if key is not None:
                        found[key] = Path(entry.path)
        except OSError:
            pass
        try:
            st = self.stream_path.stat()
            key = wanted.get((st.st_dev, st.st_ino))
            if key is not None:
                found[key] = self.stream_path
        except OSError:
            pass
        return found

    def _parse_line(self, conn: sqlite3.Connection, path: str, line: bytes) -> str:
        raw = line[:-1] if line.endswith(b"\r") else line
        if not raw.strip():
            return "ignored"
        try:
            text = raw.decode("utf-8", errors="strict")
            event = _json_object(text, "alert line")
            result = self.enqueue_event(event, conn=conn)
            action = result.get("action", "ignored")
            if action == "split":
                actions = {part.get("action") for part in result.get("parts", [])}
                return next((name for name in ("queued", "merged", "recovered") if name in actions), "ignored")
            return action
        except (UnicodeDecodeError, SchedulerError, ValueError) as exc:
            self._quarantine(conn, path, raw, str(exc))
            return "quarantined"

    def scan_stream(self, *, replay: bool = False) -> dict[str, int]:
        """Ingest bounded JSONL input while committing queue changes and offsets atomically."""
        counts = {"lines": 0, "events": 0, "merged": 0, "recoveries": 0, "quarantined": 0}
        try:
            stream_probe = self.stream_path.open("rb", buffering=0)
            current_stat = os.fstat(stream_probe.fileno())
            if not stat.S_ISREG(current_stat.st_mode):
                stream_probe.close()
                raise SchedulerError("stream must be a regular file")
            probe_key = self._source_key(current_stat.st_dev, current_stat.st_ino)
            existing_probe = self._open_sources.get(probe_key)
            if existing_probe is not None:
                stream_probe.close()
            else:
                self._open_sources[probe_key] = stream_probe
            self._open_source_names[probe_key] = str(self.stream_path)
        except FileNotFoundError:
            # A temporarily absent stream is not a reason to lose the durable queue.
            return counts
        except OSError as exc:
            raise SchedulerError(f"cannot open stream: {type(exc).__name__}") from exc
        with self._transaction() as conn:
            initialized = conn.execute("SELECT value FROM metadata WHERE key='stream_initialized'").fetchone()
            first_stream_seen = initialized is None
            if replay:
                key = self._source_key(current_stat.st_dev, current_stat.st_ino)
                conn.execute("""INSERT INTO cursors(source_key,device,inode,path,offset,pending,discarding,created_at,updated_at)
                    VALUES(?,?,?,?,0,X'',0,?,?) ON CONFLICT(source_key) DO UPDATE SET path=excluded.path,offset=0,pending=X'',discarding=0,checkpoint=X'',eof_size=-1,eof_checks=0,updated_at=excluded.updated_at""",
                    (key, current_stat.st_dev, current_stat.st_ino, str(self.stream_path), self.clock(), self.clock()))
            key_current = self._source_key(current_stat.st_dev, current_stat.st_ino)
            tracked = conn.execute("SELECT * FROM cursors ORDER BY CASE WHEN source_key=? THEN 0 ELSE 1 END,updated_at,created_at,source_key LIMIT 4",
                                   (key_current,)).fetchall()
            by_key = {row["source_key"]: row for row in tracked}
            if key_current not in by_key:
                initial_offset = current_stat.st_size if first_stream_seen and not replay else 0
                initial_checkpoint = b""
                if initial_offset:
                    probe = self._open_sources[key_current]
                    probe.seek(max(0, initial_offset - 64))
                    initial_checkpoint = probe.read(min(64, initial_offset))
                conn.execute("INSERT INTO cursors(source_key,device,inode,path,offset,pending,discarding,checkpoint,created_at,updated_at) VALUES(?,?,?,?,?,X'',0,?,?,?)",
                             (key_current, current_stat.st_dev, current_stat.st_ino, str(self.stream_path), initial_offset,
                              initial_checkpoint, self.clock(), self.clock()))
                tracked = conn.execute("SELECT * FROM cursors ORDER BY CASE WHEN source_key=? THEN 0 ELSE 1 END,updated_at,created_at,source_key LIMIT 4", 
                                       (key_current,)).fetchall()
                by_key = {row["source_key"]: row for row in tracked}
            conn.execute("INSERT INTO metadata(key,value) VALUES('stream_initialized','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            locations = self._find_tracked_files(tracked)
            read_budget = READ_BYTES_PER_SOURCE
            # Keep descriptors for every tracked inode: a rotated pathname may disappear
            # before the next poll, but the already-open descriptor can still drain its tail.
            for key, row in by_key.items():
                if read_budget <= 0:
                    break
                source = locations.get(key)
                stream = self._open_sources.get(key)
                source_name = str(source) if source is not None else self._open_source_names.get(key, row["path"])
                if stream is None:
                    if source is None:
                        pending = bytes(row["pending"] or b"")
                        self._quarantine(conn, row["path"], pending, "tracked inode disappeared before its unread tail could be recovered")
                        self._enqueue_notification(conn, f"stream-loss:{key}", "stream_loss", "AutoOps 日志轮转数据缺口",
                                                   f"stream inode {key} was removed before its unread tail could be drained; inspect the local quarantine log.")
                        counts["quarantined"] += 1
                        conn.execute("DELETE FROM cursors WHERE source_key=?", (key,))
                        continue
                    try:
                        stream = open(source, "rb", buffering=0)
                    except FileNotFoundError:
                        self._quarantine(conn, str(source), bytes(row["pending"] or b""),
                                         "tracked inode disappeared before it could be opened")
                        self._enqueue_notification(conn, f"stream-loss:{key}", "stream_loss", "AutoOps 日志轮转数据缺口",
                                                   f"stream inode {key} disappeared before it could be read; inspect the local quarantine log.")
                        counts["quarantined"] += 1
                        conn.execute("DELETE FROM cursors WHERE source_key=?", (key,))
                        continue
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (int(row["device"]), int(row["inode"])):
                        stream.close()
                        continue
                    self._open_sources[key] = stream
                    self._open_source_names[key] = str(source)
                else:
                    self._open_source_names[key] = source_name
                try:
                    st = os.fstat(stream.fileno())
                    if (st.st_dev, st.st_ino) != (int(row["device"]), int(row["inode"])):
                        stream.close()
                        self._open_sources.pop(key, None)
                        self._open_source_names.pop(key, None)
                        continue
                    offset = int(row["offset"])
                    pending = bytes(row["pending"] or b"")
                    checkpoint = bytes(row["checkpoint"] or b"")
                    discarding = bool(row["discarding"])
                    if st.st_size < offset:
                        if pending:
                            self._quarantine(conn, source_name, pending, "stream truncated with an incomplete line")
                            counts["quarantined"] += 1
                        offset, pending, checkpoint, discarding = 0, b"", b"", False
                    elif offset and checkpoint:
                        stream.seek(max(0, offset - len(checkpoint)))
                        actual = stream.read(len(checkpoint))
                        if actual != checkpoint:
                            if pending:
                                self._quarantine(conn, source_name, pending, "stream contents changed before the cursor; treated as truncation")
                                counts["quarantined"] += 1
                            offset, pending, checkpoint, discarding = 0, b"", b"", False
                    stream.seek(offset)
                    chunk = stream.read(min(READ_BYTES_PER_SOURCE, read_budget))
                    read_budget -= len(chunk)
                    next_offset = offset + len(chunk)
                    if chunk:
                        stream.seek(max(0, next_offset - 64))
                        checkpoint = stream.read(min(64, next_offset))
                    if not chunk:
                        eof_size, eof_checks = -1, 0
                        if key != key_current and st.st_nlink == 0:
                            eof_size = st.st_size
                            eof_checks = int(row["eof_checks"]) + 1 if int(row["eof_size"]) == st.st_size else 1
                            if eof_checks >= 2:
                                if pending:
                                    self._quarantine(conn, source_name, pending, "unlinked rotated stream ended with an incomplete line")
                                    counts["quarantined"] += 1
                                conn.execute("DELETE FROM cursors WHERE source_key=?", (key,))
                                self._open_sources.pop(key, None)
                                self._open_source_names.pop(key, None)
                                stream.close()
                                continue
                        conn.execute("UPDATE cursors SET path=?,offset=?,pending=?,discarding=?,checkpoint=?,eof_size=?,eof_checks=?,updated_at=? WHERE source_key=?",
                                     (source_name, next_offset, pending, int(discarding), checkpoint, eof_size, eof_checks, self.clock(), key))
                        continue
                    position = 0
                    while True:
                        newline = chunk.find(b"\n", position)
                        if newline < 0:
                            tail = chunk[position:]
                            break
                        raw_line = chunk[position:newline]
                        position = newline + 1
                        counts["lines"] += 1
                        if discarding:
                            discarding = False
                            pending = b""
                            continue
                        line = pending + raw_line
                        pending = b""
                        if len(line) > MAX_LINE_BYTES:
                            self._quarantine(conn, source_name, line, "line exceeds 1 MiB limit")
                            counts["quarantined"] += 1
                            continue
                        action = self._parse_line(conn, source_name, line)
                        if action == "queued":
                            counts["events"] += 1
                        elif action == "merged":
                            counts["merged"] += 1
                        elif action == "recovered":
                            counts["recoveries"] += 1
                        elif action == "quarantined":
                            counts["quarantined"] += 1
                    if discarding:
                        pending = b""
                    else:
                        pending += tail
                        if len(pending) > MAX_LINE_BYTES:
                            self._quarantine(conn, source_name, pending, "line exceeds 1 MiB limit")
                            counts["quarantined"] += 1
                            pending = b""
                            discarding = True
                    conn.execute("UPDATE cursors SET path=?,offset=?,pending=?,discarding=?,checkpoint=?,eof_size=-1,eof_checks=0,updated_at=? WHERE source_key=?",
                                 (source_name, next_offset, pending, int(discarding), checkpoint, self.clock(), key))
                except OSError as exc:
                    raise SchedulerError(f"cannot read tracked stream inode {key}: {exc}") from exc
            conn.execute("INSERT INTO metadata(key,value) VALUES('last_stream_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (str(self.clock()),))
        self._flush_quarantines()
        return counts

    def _normalize_event(self, event: dict[str, Any]) -> tuple[str, str, bool, bool]:
        if not isinstance(event, dict):
            raise SchedulerError("event must be a JSON object")
        service_value = event.get("service", event.get("service_name"))
        code_value = event.get("fault_code", event.get("code", event.get("fault")))
        if not isinstance(service_value, str) or not service_value.strip():
            raise SchedulerError("event requires a non-empty service")
        if not isinstance(code_value, str) or not code_value.strip():
            raise SchedulerError("event requires a non-empty fault_code or code")
        service, code = service_value.strip(), code_value.strip()
        if len(service) > 160 or len(code) > 240 or "\x00" in service or "\x00" in code:
            raise SchedulerError("service and fault identifiers exceed safe bounds")
        state = _safe_text(event.get("level", event.get("status", event.get("state", event.get("type", "")))), 80).upper()
        explicit = event.get("recovered") is True or event.get("service_recovered") is True or state in {
            "RECOVERED", "RECOVERY", "RESOLVED", "HEALTHY", "SERVICE_RECOVERED"
        }
        suffix = code.upper().endswith("_RECOVERED")
        recovery = explicit or suffix
        service_level = event.get("service_recovered") is True or _safe_text(event.get("scope", ""), 40).lower() == "service"
        base = code[:-10] if suffix else code
        # Current watchdog recovery records are explicitly service-scoped *_RECOVERED.
        if suffix:
            service_level = True
        if state == "SERVICE_RECOVERED":
            service_level = True
        normalized_code = base if suffix else code
        return service, normalized_code, recovery, service_level

    def enqueue_event(self, event: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Persist failures, apply scoped recovery, and merge repeated active faults."""
        if not isinstance(event, dict):
            raise SchedulerError("event must be a JSON object")
        level = event.get("level")
        if level not in ("failure", "recovery"):
            return {"action": "ignored", "reason": "only failure and recovery levels are actionable"}
        if level == "failure" and event.get("auto_recovery_handled") is True:
            return {"action": "ignored", "reason": "automatic recovery was already handled"}
        if conn is None:
            with self._transaction() as tx:
                return self.enqueue_event(event, conn=tx)
        expanded = self._expand_codes(event)
        if expanded is not None:
            return {"action": "split", "parts": [self.enqueue_event(part, conn=conn) for part in expanded]}
        safe_event = _redact_payload(event)
        service, fault_code, recovery, service_level = self._normalize_event(safe_event)
        if level == "recovery":
            recovery = True
        try:
            encoded = json.dumps(safe_event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SchedulerError(f"event is not JSON serializable: {type(exc).__name__}") from exc
        if len(encoded.encode("utf-8")) > MAX_LINE_BYTES:
            raise SchedulerError("event exceeds 1 MiB limit")
        now = self.clock()
        if recovery:
            if service_level:
                cur = conn.execute("UPDATE queue SET status='recovered',updated_at=?,last_error=NULL WHERE service=? AND status='pending'", (now, service))
            else:
                cur = conn.execute("UPDATE queue SET status='recovered',updated_at=?,last_error=NULL WHERE service=? AND fault_code=? AND status='pending'", (now, service, fault_code))
            return {"action": "recovered", "cancelled": cur.rowcount, "service": service, "fault_code": fault_code}
        row = conn.execute("SELECT id,status,event_json,merged_count FROM queue WHERE service=? AND fault_code=? AND status IN ('pending','running') ORDER BY id LIMIT 1",
                           (service, fault_code)).fetchone()
        if row is not None:
            merged = int(row["merged_count"]) + 1
            if row["status"] == "pending":
                try:
                    prior = json.loads(row["event_json"])
                except json.JSONDecodeError:
                    prior = {"service": service, "fault_code": fault_code}
                if not isinstance(prior, dict):
                    prior = {"service": service, "fault_code": fault_code}
                observations = prior.get("merge_observations")
                if not isinstance(observations, list):
                    observations = []
                    prior["merge_observations"] = observations
                if len(observations) < 20:
                    summary = {key: safe_event[key] for key in ("timestamp", "iso_time", "level", "title", "summary") if key in safe_event}
                    observations.append({"merged_at": _utc_iso(now), "event": summary})
                merged_json = json.dumps(prior, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                if len(merged_json.encode("utf-8")) > MAX_LINE_BYTES:
                    prior.pop("merge_observations", None)
                    merged_json = json.dumps(prior, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    if len(merged_json.encode("utf-8")) > MAX_LINE_BYTES:
                        merged_json = row["event_json"]
                conn.execute("UPDATE queue SET event_json=?,merged_count=?,updated_at=? WHERE id=?",
                             (merged_json, merged, now, row["id"]))
            else:
                conn.execute("UPDATE queue SET merged_count=?,updated_at=? WHERE id=?", (merged, now, row["id"]))
            return {"action": "merged", "queue_id": row["id"], "merged_count": merged}
        cur = conn.execute("INSERT INTO queue(service,fault_code,event_json,status,created_at,updated_at) VALUES(?,?,?,'pending',?,?)",
                           (service, fault_code, encoded, now, now))
        return {"action": "queued", "queue_id": cur.lastrowid}

    def enqueue_json(self, data: str | bytes) -> dict[str, Any]:
        return self.enqueue_event(_json_object(data, "event"))

    def _replay_stream(self) -> None:
        st = self.stream_path.stat()
        if not stat.S_ISREG(st.st_mode):
            raise SchedulerError("stream must be a regular file")
        key = self._source_key(st.st_dev, st.st_ino)
        with self._transaction() as conn:
            conn.execute("""INSERT INTO cursors(source_key,device,inode,path,offset,pending,discarding,created_at,updated_at)
                VALUES(?,?,?,?,0,X'',0,?,?) ON CONFLICT(source_key) DO UPDATE SET path=excluded.path,offset=0,pending=X'',discarding=0,checkpoint=X'',eof_size=-1,eof_checks=0,updated_at=excluded.updated_at""", 
                (key, st.st_dev, st.st_ino, str(self.stream_path), self.clock(), self.clock()))
            conn.execute("INSERT INTO metadata(key,value) VALUES('stream_initialized','1') ON CONFLICT(key) DO UPDATE SET value='1'")

    def _broker_mutation_logged(self, run_id: str) -> tuple[bool, bool]:
        """Return (mutation_seen, audit_uncertain) from the trusted local broker journal."""
        path = self.run_root / run_id / "actions.jsonl"
        try:
            with open(path, "rb") as stream:
                data = stream.read(MAX_LINE_BYTES + 1)
        except FileNotFoundError:
            return False, False
        except OSError:
            return False, True
        if len(data) > MAX_LINE_BYTES:
            return False, True
        mutating_ops = {"patch_config", "patch_config_success", "patch_config_rollback", "start", "restart", "recreate"}
        try:
            for line in data.splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    return False, True
                if entry.get("op") in mutating_ops:
                    return True, False
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, True
        return False, False

    def _resolve_running_on_restart(self) -> int:
        """Reconcile broker results; never rerun a verified repair with unknown exit status."""
        with contextlib.closing(self._connect()) as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT run_id,queue_id,service,fault_code,output_path FROM runs WHERE finished_at IS NULL ORDER BY started_at")]
        recovered = 0
        for row in rows:
            run_id = row["run_id"]
            run_dir = self.run_root / run_id
            result_path = run_dir / "result.json"
            result = None
            try:
                with result_path.open("rb") as stream:
                    data = stream.read(MAX_LINE_BYTES + 1)
                if len(data) <= MAX_LINE_BYTES:
                    result = _json_object(data, "runner result")
            except (OSError, SchedulerError):
                pass
            mutation_seen, audit_uncertain = self._broker_mutation_logged(run_id)
            self._remove_agent_container(run_id)
            claim = {"queue_id": row["queue_id"], "run_id": run_id, "service": row["service"],
                     "fault_code": row["fault_code"]}
            if result is not None:
                if result.get("status") in ("repaired", "healthy") and result.get("verified") is True:
                    detail = _safe_text(result.get("summary", ""), 400)
                    result = {"status": "needs_scan", "verified": False,
                              "summary": "verified runner result exists but scheduler did not observe its exit code; inspect before retrying. " + detail}
                self._finish_claim(claim, {"exit_code": None, "result": result, "timed_out": False})
            elif result_path.exists() or mutation_seen or audit_uncertain:
                reason = "broker action was logged" if mutation_seen else "broker audit could not be trusted" if audit_uncertain else "runner result file is incomplete"
                self._finish_claim(claim, {"exit_code": None,
                    "result": {"status": "needs_scan", "verified": False,
                               "summary": f"{reason} after restart; inspect before retrying."}})
            else:
                now = self.clock()
                with self._transaction() as conn:
                    conn.execute("UPDATE runs SET finished_at=?,outcome='interrupted',verified=0,summary='scheduler interrupted; no broker result or mutation was recorded' WHERE run_id=? AND finished_at IS NULL",
                                 (now, run_id))
                    conn.execute("UPDATE queue SET status='pending',updated_at=?,due_at=?,run_id=NULL,last_error='interrupted; safely requeued' WHERE id=? AND status='running'",
                                 (now, now, row["queue_id"]))
            recovered += 1
        return recovered

    @staticmethod
    def _is_success(result: dict[str, Any], exit_code: int | None) -> bool:
        return exit_code == 0 and result.get("status") in ("repaired", "healthy") and result.get("verified") is True

    def _remove_agent_container(self, run_id: str) -> None:
        """Remove only the agent container labelled with this run id (container back ends only)."""
        from .runner import remove_agent_container
        remove_agent_container(self.config.get("agent") or {}, run_id)

    def _budget_wait(self, conn: sqlite3.Connection, service: str, now: float) -> float:
        waits: list[float] = []
        service_limit = int(self.config["service_hourly_limit"])
        if service_limit:
            rows = conn.execute("SELECT started_at FROM runs WHERE service=? AND started_at>? AND (outcome IS NULL OR outcome!='interrupted') ORDER BY started_at",
                                (service, now - 3600)).fetchall()
            if len(rows) >= service_limit:
                until = float(rows[0]["started_at"]) + 3600
                waits.append(until)
                hour = _dt.datetime.fromtimestamp(now, LOCAL_TZ).strftime("%Y-%m-%d-%H")
                self._enqueue_notification(conn, f"rate-limit:service:{service}:{hour}", "rate_limit",
                    "AutoOps 服务触发小时限流", f"service={_safe_text(service)}; 当前小时额度已用尽，将于 {_utc_iso(until)} 后重试；告警仍保留。")
        daily_limit = int(self.config["daily_limit"])
        if daily_limit:
            day = _local_day(now)
            start = _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time.min, LOCAL_TZ).timestamp()
            count = conn.execute("SELECT COUNT(*) FROM runs WHERE started_at>=? AND (outcome IS NULL OR outcome!='interrupted')", (start,)).fetchone()[0]
            if count >= daily_limit:
                waits.append(_next_local_midnight(now))
                self._enqueue_notification(conn, f"rate-limit:daily:{day}", "rate_limit",
                    "AutoOps 触发每日限流", f"{zone_label(LOCAL_TZ)} {day} 的每日额度已用尽；告警仍保留至次日恢复调度。")
        return max(waits, default=0.0)


    def _block_unmapped(self, policy: dict[str, Any], now: float) -> int:
        """Faults without a policy mapping can never be repaired: record them, never start an Agent."""
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute("SELECT id,service,fault_code FROM queue WHERE status='pending' ORDER BY id LIMIT 1000").fetchall()
        unmapped = [int(row["id"]) for row in rows if self._fault_spec(policy, row["service"], row["fault_code"]) is None]
        if not unmapped:
            return 0
        with self._transaction() as conn:
            for queue_id in unmapped:
                conn.execute("UPDATE queue SET status='blocked',updated_at=?,last_error=? WHERE id=? AND status='pending'",
                             (now, UNMAPPED_REASON, queue_id))
        return len(unmapped)

    def _claim_next(self) -> dict[str, Any] | None:
        now = self.clock()
        policy = self._load_policy()
        if policy is None:
            # Fail closed: keep every alert pending rather than guessing which ones are mapped.
            with self._transaction() as conn:
                self._enqueue_notification(conn, f"policy-unreadable:{_local_day(now)}", "policy",
                    "AutoOps 授权策略无法读取", "policy.json 无法读取或未通过校验（运行 server-autoops policy check 查看原因）；告警全部保留在队列，暂不启动 Agent。")
            return None
        self._block_unmapped(policy, now)
        # Memory is a hard admission gate, not a reason to drop a durable alert.
        try:
            free_mb = int(self.memory_probe())
        except Exception:
            free_mb = 0
        if free_mb < int(self.config["min_available_mb"]):
            with self._transaction() as conn:
                delay = max(5.0, float(self.config["poll_seconds"]) * 4)
                conn.execute("UPDATE queue SET due_at=MAX(due_at,?),updated_at=? WHERE status='pending'",
                             (now + delay, now))
            return None
        with self._transaction() as conn:
            row = None
            after_id = 0
            while row is None:
                candidates = conn.execute("SELECT id,service,fault_code,due_at FROM queue WHERE status='pending' AND id>? ORDER BY id LIMIT 100",
                                          (after_id,)).fetchall()
                if not candidates:
                    break
                for candidate in candidates:
                    after_id = int(candidate["id"])
                    due = float(candidate["due_at"])
                    cooldown = conn.execute("SELECT until_at FROM cooldowns WHERE service=? AND fault_code=?",
                                            (candidate["service"], candidate["fault_code"])).fetchone()
                    if cooldown:
                        due = max(due, float(cooldown["until_at"]))
                    breaker = conn.execute("SELECT open_until FROM breakers WHERE service=?", (candidate["service"],)).fetchone()
                    if breaker and float(breaker["open_until"]) > now:
                        due = max(due, float(breaker["open_until"]))
                    budget_until = self._budget_wait(conn, candidate["service"], now)
                    if budget_until:
                        due = max(due, budget_until)
                    if due > now:
                        conn.execute("UPDATE queue SET due_at=MAX(due_at,?),updated_at=? WHERE id=?",
                                     (due, now, candidate["id"]))
                        continue
                    row = conn.execute("SELECT * FROM queue WHERE id=?", (candidate["id"],)).fetchone()
                    break
            if row is None:
                return None
            run_id = uuid.uuid4().hex
            run_dir = self.run_root / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            os.chmod(run_dir, 0o700)
            event_path = run_dir / "event.json"
            payload = json.dumps(json.loads(row["event_json"]), ensure_ascii=False, indent=2) + "\n"
            fd = os.open(event_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            output_path = run_dir / "output.log"
            os.close(os.open(output_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            cur = conn.execute("UPDATE queue SET status='running',attempts=attempts+1,updated_at=?,run_id=?,last_error=NULL WHERE id=? AND status='pending'",
                               (now, run_id, row["id"]))
            if cur.rowcount != 1:
                return None
            conn.execute("INSERT INTO runs(run_id,queue_id,service,fault_code,started_at,output_path) VALUES(?,?,?,?,?,?)",
                         (run_id, row["id"], row["service"], row["fault_code"], now, str(output_path)))
            cooldown_until = now + float(self.config["cooldown_seconds"])
            conn.execute("INSERT INTO cooldowns(service,fault_code,until_at) VALUES(?,?,?) ON CONFLICT(service,fault_code) DO UPDATE SET until_at=excluded.until_at",
                         (row["service"], row["fault_code"], cooldown_until))
            return {"queue_id": int(row["id"]), "run_id": run_id, "service": row["service"],
                    "fault_code": row["fault_code"], "event_path": str(event_path), "output_path": str(output_path),
                    "attempt": int(row["attempts"]) + 1}

    def _read_final_json(self, output_path: str) -> dict[str, Any] | None:
        try:
            with open(output_path, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - OUTPUT_TAIL_BYTES))
                data = stream.read(OUTPUT_TAIL_BYTES)
        except OSError:
            return None
        for line in reversed(data.splitlines()):
            try:
                parsed = json.loads(line.decode("utf-8", "strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("status"), str):
                return parsed
        return None

    def _terminate_runner(self, proc: subprocess.Popen[Any], run_id: str) -> int | None:
        """Ask broker work to finish for up to agent_grace_seconds before killing its process group."""
        self._remove_agent_container(run_id)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + float(self.config.get("agent_grace_seconds", 240))
        while time.monotonic() < deadline:
            exit_code = proc.poll()
            try:
                os.killpg(proc.pid, 0)
                group_exists = True
            except ProcessLookupError:
                group_exists = False
            except PermissionError:
                group_exists = True
            if not group_exists:
                if exit_code is None:
                    try:
                        return proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        return proc.poll()
                return exit_code
            time.sleep(0.2)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return proc.poll()

    def _execute_runner(self, claim: dict[str, Any]) -> dict[str, Any]:
        package_parent = str(Path(__file__).resolve().parent.parent)
        entry = str(Path(package_parent) / "bin" / "server-autoops.py")
        command = [sys.executable, "-I", entry, "runner", "--config", self.config["config_path"],
                   "--event-file", claim["event_path"], "--run-id", claim["run_id"]]
        try:
            with open(claim["output_path"], "ab", buffering=0) as output:
                proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                        close_fds=True, start_new_session=True, cwd=package_parent)
                deadline = time.monotonic() + float(self.config["agent_timeout"])
                while proc.poll() is None:
                    if self._stop_signal is not None:
                        self._terminate_runner(proc, claim["run_id"])
                        return {"exit_code": None, "result": None, "timed_out": False,
                                "interrupted": True, "deadline_exceeded": False, "error": "scheduler shutdown requested"}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        exit_code = self._terminate_runner(proc, claim["run_id"])
                        if exit_code is None:
                            return {"exit_code": None, "result": None, "timed_out": True,
                                    "interrupted": False, "deadline_exceeded": True, "error": "runner timed out after cancellation grace"}
                        return {"exit_code": exit_code, "result": self._read_final_json(claim["output_path"]),
                                "timed_out": False, "interrupted": False, "deadline_exceeded": True, "error": None}
                    time.sleep(min(0.2, remaining))
                return {"exit_code": proc.returncode, "result": self._read_final_json(claim["output_path"]),
                        "timed_out": False, "interrupted": False, "error": None}
        except OSError as exc:
            return {"exit_code": None, "result": None, "timed_out": False, "interrupted": False,
                    "error": f"runner launch failed: {type(exc).__name__}"}

    def _reconcile_stopped_claim(self, claim: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
        """Inspect trusted result/audit state before deciding whether interrupted work may retry."""
        interrupted = execution.get("interrupted") is True
        deadline_exceeded = execution.get("deadline_exceeded") is True or execution.get("timed_out") is True
        if not interrupted and not deadline_exceeded:
            return execution
        if deadline_exceeded and not interrupted and execution.get("exit_code") is not None and isinstance(execution.get("result"), dict):
            return execution
        run_dir = Path(claim["event_path"]).parent
        result_path = run_dir / "result.json"
        result = None
        result_uncertain = False
        try:
            with result_path.open("rb") as stream:
                data = stream.read(MAX_LINE_BYTES + 1)
            result_uncertain = len(data) > MAX_LINE_BYTES
            if not result_uncertain:
                result = _json_object(data, "runner result")
        except FileNotFoundError:
            pass
        except (OSError, SchedulerError):
            result_uncertain = True
        mutation_seen, audit_uncertain = self._broker_mutation_logged(claim["run_id"])
        if result is not None:
            if interrupted or (result.get("status") in ("repaired", "healthy") and result.get("verified") is True):
                detail = _safe_text(result.get("summary", ""), 300)
                result = {"status": "needs_scan", "verified": False,
                          "summary": "runner stopped without an observed zero exit; inspect before retrying. " + detail}
                return {"exit_code": None, "result": result, "timed_out": False,
                        "interrupted": False, "deadline_exceeded": deadline_exceeded}
            return {"exit_code": None, "result": result, "timed_out": False,
                    "interrupted": False, "deadline_exceeded": deadline_exceeded}
        if result_path.exists() or result_uncertain or mutation_seen or audit_uncertain:
            reason = "broker mutation was recorded" if mutation_seen else "broker audit is incomplete" if audit_uncertain else "runner result is incomplete"
            return {"exit_code": None,
                    "result": {"status": "needs_scan", "verified": False,
                               "summary": f"{reason} after runner interruption; inspect before retrying."},
                    "timed_out": False, "interrupted": False, "deadline_exceeded": deadline_exceeded}
        return execution
    def _enqueue_notification(self, conn: sqlite3.Connection, key: str, kind: str, title: str, body: str) -> None:
        state = "pending" if self.config.get("notifications_enabled", True) else "suppressed"
        note = None if state == "pending" else "suppressed: notifications disabled by config"
        conn.execute("INSERT OR IGNORE INTO notifications(dedupe_key,kind,title,body,state,last_error,created_at) VALUES(?,?,?,?,?,?,?)",
                     (key, kind, _safe_text(title, 120), _safe_body(body), state, note, self.clock()))

    def _finish_claim(self, claim: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        result = execution.get("result") if isinstance(execution, dict) else None
        if not isinstance(result, dict):
            result = {}
        interrupted = execution.get("interrupted") is True
        exit_code = execution.get("exit_code")
        success = not interrupted and self._is_success(result, exit_code)
        status = result.get("status") if isinstance(result.get("status"), str) else "failed"
        summary = _safe_text(result.get("summary", execution.get("error", "runner returned no final JSON result")), 1000)
        if interrupted:
            status = "interrupted"
            summary = "scheduler shutdown interrupted runner; task safely requeued"
        elif execution.get("timed_out"):
            status = "failed"
            summary = "runner timed out after safe cancellation grace"
        elif not success and status in ("repaired", "healthy") and result.get("verified") is True:
            status = "needs_scan"
            summary = "runner reported a verified result but did not exit successfully; inspect before retrying"
        with self._transaction() as conn:
            run = conn.execute("SELECT queue_id FROM runs WHERE run_id=?", (claim["run_id"],)).fetchone()
            if run is None:
                raise SchedulerError("claimed run disappeared from the execution ledger")
            outcome = ("repaired" if status == "repaired" else "healthy") if success else ("interrupted" if interrupted else "failed" if status in ("repaired", "healthy") else status)
            business_unverified = success and result.get("business_verified") is False
            if business_unverified:
                # Only a container-running probe passed: resolved, but never reported as a verified repair.
                outcome += "_unverified"
            conn.execute("UPDATE runs SET finished_at=?,outcome=?,verified=?,exit_code=?,summary=? WHERE run_id=?",
                         (now, outcome, int(success), exit_code, summary, claim["run_id"]))
            if interrupted:
                conn.execute("UPDATE queue SET status='pending',updated_at=?,due_at=?,run_id=NULL,last_error=? WHERE id=?",
                             (now, now, summary, claim["queue_id"]))
            elif success:
                conn.execute("UPDATE queue SET status='resolved',updated_at=?,last_error=NULL WHERE id=?",
                             (now, claim["queue_id"]))
                conn.execute("UPDATE breakers SET failures=0,open_until=0,opened_at=NULL,last_error=NULL WHERE service=?",
                             (claim["service"],))
            elif status == "needs_scan":
                conn.execute("UPDATE queue SET status='needs_scan',updated_at=?,last_error=? WHERE id=?",
                             (now, summary, claim["queue_id"]))
                self._enqueue_notification(conn, f"needs-scan:{claim['queue_id']}", "needs_scan",
                    "AutoOps 需要人工检查", f"service={_safe_text(claim['service'])}; fault={_safe_text(claim['fault_code'])}; run={claim['run_id']}; see the local run result.")
            elif status == "blocked":
                conn.execute("UPDATE queue SET status='blocked',updated_at=?,last_error=? WHERE id=?",
                             (now, summary, claim["queue_id"]))
            else:
                conn.execute("UPDATE queue SET status='pending',updated_at=?,due_at=?,last_error=? WHERE id=?",
                             (now, now + float(self.config["cooldown_seconds"]), summary, claim["queue_id"]))
                breaker = conn.execute("SELECT failures FROM breakers WHERE service=?", (claim["service"],)).fetchone()
                failures = (int(breaker["failures"]) if breaker else 0) + 1
                open_until = 0.0
                opened_at = None
                if failures >= int(self.config.get("breaker_failures", BREAKER_FAILURES)):
                    backoff = breaker_backoff_seconds(failures, int(self.config.get("breaker_failures", BREAKER_FAILURES)),
                                                      float(self.config.get("breaker_base_seconds", BREAKER_COOLDOWN_SECONDS)),
                                                      float(self.config.get("breaker_max_seconds", BREAKER_MAX_COOLDOWN_SECONDS)))
                    open_until = now + backoff
                    opened_at = now
                    # Exponential backoff keeps retrying forever; at most one breaker notice per service per local day.
                    self._enqueue_notification(conn, f"breaker:{claim['service']}:{_local_day(now)}", "breaker",
                        "AutoOps 服务熔断", f"service={_safe_text(claim['service'])}; 连续失败 {failures} 次，退避 {backoff / 3600:.4g} 小时，"
                        f"{_local_clock(open_until)} 后自动再试；告警仍保留，修好后计数清零。本服务当天不再重复通知。")
                conn.execute("INSERT INTO breakers(service,failures,open_until,opened_at,last_error) VALUES(?,?,?,?,?) "
                             "ON CONFLICT(service) DO UPDATE SET failures=excluded.failures,open_until=excluded.open_until,opened_at=excluded.opened_at,last_error=excluded.last_error",
                             (claim["service"], failures, open_until, opened_at, summary))
        return {"queue_id": claim["queue_id"], "run_id": claim["run_id"], "status": status,
                "success": success, "summary": summary, "outcome": outcome,
                "business_verified": not business_unverified if success else False}

    def run_one(self) -> dict[str, Any] | None:
        if self._stop_signal is not None:
            return None
        claim = self._claim_next()
        if claim is None:
            return None
        if self._stop_signal is not None:
            execution = {"exit_code": None, "result": None, "timed_out": False,
                         "interrupted": True, "error": "scheduler shutdown requested before runner start"}
        else:
            try:
                execution = self.runner_executor(claim)
            except Exception as exc:
                execution = {"exit_code": None, "result": None, "timed_out": False,
                             "error": f"runner executor error: {type(exc).__name__}"}
        execution = self._reconcile_stopped_claim(claim, execution)
        return self._finish_claim(claim, execution)

    def drain(self, max_runs: int | None = None) -> list[dict[str, Any]]:
        results = []
        while self._stop_signal is None and (max_runs is None or len(results) < max_runs):
            result = self.run_one()
            if result is None:
                break
            results.append(result)
        return results

    def deliver_notifications(self, *, limit: int = 20) -> dict[str, int]:
        if not self.config.get("notifications_enabled", True):
            with self._transaction() as conn:
                cur = conn.execute("UPDATE notifications SET state='suppressed',lease_until=0,last_error='suppressed: notifications disabled by config' WHERE state IN ('pending','sending')")
                return {"sent": 0, "failed": 0, "suppressed": cur.rowcount}
        sent = failed = 0
        for _ in range(limit):
            now = self.clock()
            with self._transaction() as conn:
                row = conn.execute("SELECT * FROM notifications WHERE (state='pending' AND not_before<=?) OR (state='sending' AND lease_until<=?) ORDER BY id LIMIT 1",
                                   (now, now)).fetchone()
                if row is None:
                    break
                conn.execute("UPDATE notifications SET state='sending',attempts=attempts+1,lease_until=? WHERE id=?",
                             (now + NOTIFICATION_LEASE_SECONDS, row["id"]))
                notice = dict(row)
            try:
                delivered = self.notifier(notice["title"], notice["body"], notice["kind"])
                if delivered is False:
                    raise SchedulerError("notification back end reported failure")
            except Exception as exc:
                failed += 1
                attempts = int(notice["attempts"]) + 1
                delay = min(NOTIFICATION_RETRY_MAX_SECONDS, 30 * (2 ** min(attempts - 1, 10)))
                with self._transaction() as conn:
                    conn.execute("UPDATE notifications SET state='pending',not_before=?,lease_until=0,last_error=? WHERE id=?",
                                 (self.clock() + delay, type(exc).__name__, notice["id"]))
            else:
                sent += 1
                with self._transaction() as conn:
                    conn.execute("UPDATE notifications SET state='sent',sent_at=?,lease_until=0,last_error=NULL WHERE id=?",
                                 (self.clock(), notice["id"]))
        return {"sent": sent, "failed": failed}

    def create_daily_report(self, day: str | None = None) -> dict[str, Any]:
        """Persist a local-day report and its once-only heartbeat notification."""
        if day is None:
            day = (_dt.datetime.fromtimestamp(self.clock(), LOCAL_TZ).date() - _dt.timedelta(days=1)).isoformat()
        try:
            report_date = _dt.date.fromisoformat(day)
        except ValueError as exc:
            raise SchedulerError("daily report date must be YYYY-MM-DD") from exc
        if report_date.isoformat() != day:
            raise SchedulerError("daily report date must be YYYY-MM-DD")
        start = _dt.datetime.combine(report_date, _dt.time.min, LOCAL_TZ).timestamp()
        end_ts = _dt.datetime.combine(report_date + _dt.timedelta(days=1), _dt.time.min, LOCAL_TZ).timestamp()
        with self._transaction() as conn:
            existing = conn.execute("SELECT body,created_at FROM daily_reports WHERE day=?", (day,)).fetchone()
            if existing:
                return {"day": day, "body": existing["body"], "created": False}
            run_stats = conn.execute("SELECT COUNT(*) total, SUM(CASE WHEN outcome IN ('repaired','healthy') THEN 1 ELSE 0 END) successful, "
                                     "SUM(CASE WHEN outcome IN ('repaired_unverified','healthy_unverified') THEN 1 ELSE 0 END) unverified, "
                                     "SUM(CASE WHEN outcome IS NULL OR outcome NOT IN ('repaired','healthy','repaired_unverified','healthy_unverified') THEN 1 ELSE 0 END) failed "
                                     "FROM runs WHERE started_at>=? AND started_at<?", (start, end_ts)).fetchone()
            pending = conn.execute("SELECT COUNT(*) FROM queue WHERE status='pending' AND created_at<?", (end_ts,)).fetchone()[0]
            # Terminal states are counted for the reported day only, so the numbers do not grow forever.
            day_stats = conn.execute("SELECT SUM(CASE WHEN status='needs_scan' THEN 1 ELSE 0 END) needs_scan, "
                                     "SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked, "
                                     "SUM(CASE WHEN status='blocked' AND last_error=? THEN 1 ELSE 0 END) unmapped "
                                     "FROM queue WHERE updated_at>=? AND updated_at<?", (UNMAPPED_REASON, start, end_ts)).fetchone()
            q_errors = conn.execute("SELECT COUNT(*) FROM quarantines WHERE created_at>=? AND created_at<?", (start, end_ts)).fetchone()[0]
            total = int(run_stats["total"] or 0)
            success = int(run_stats["successful"] or 0)
            unverified = int(run_stats["unverified"] or 0)
            failed = int(run_stats["failed"] or 0)
            pending = int(pending or 0)
            needs_scan = int(day_stats["needs_scan"] or 0)
            blocked = int(day_stats["blocked"] or 0)
            unmapped = int(day_stats["unmapped"] or 0)
            lines = [f"AutoOps 每日汇总（{zone_label(LOCAL_TZ)}） {day}",
                     f"运行：{total} 次；业务已验证成功：{success}；容器在跑、业务未验证：{unverified}；未成功：{failed}",
                     f"待处理：{pending}；当日需人工检查：{needs_scan}；当日受阻：{blocked}（其中无映射、未启动 Agent：{unmapped}）",
                     f"隔离错误日志行：{int(q_errors)}"]
            if total == 0 and pending == 0 and needs_scan == 0 and blocked == 0 and q_errors == 0:
                lines.append("当日无运维变更；这是每日健康心跳。")
            run_rows = conn.execute("SELECT run_id,service,fault_code,started_at,outcome,summary FROM runs "
                                    "WHERE started_at>=? AND started_at<? ORDER BY started_at,run_id LIMIT 20", (start, end_ts)).fetchall()
            if run_rows:
                lines.append("当日运行详情（最多20条）：")
            for row in run_rows[:20]:
                rolled_back = "未知"
                result_summary = row["summary"] or ""
                try:
                    result_path = self.run_root / row["run_id"] / "result.json"
                    with result_path.open("rb") as stream:
                        data = stream.read(MAX_LINE_BYTES + 1)
                    if len(data) <= MAX_LINE_BYTES:
                        result = _json_object(data, "runner result")
                        if isinstance(result.get("rolled_back"), bool):
                            rolled_back = "是" if result["rolled_back"] else "否"
                        if not result_summary:
                            result_summary = result.get("summary", "")
                except (OSError, SchedulerError):
                    pass
                when = _dt.datetime.fromtimestamp(row["started_at"], LOCAL_TZ).strftime("%H:%M")
                outcome = OUTCOME_LABELS.get(row["outcome"] or "running", _safe_text(row["outcome"], 20))
                service = _safe_text(row["service"], 20)
                fault = _safe_text(row["fault_code"], 22)
                reason = _safe_text(result_summary, 36)
                lines.append(_safe_text(f"{when} {service}/{fault} {outcome} 回滚={rolled_back} 根因:{reason}", 100))
            if total > 20:
                lines.append(f"另有 {total - 20} 次运行未逐条展开。")
            body = "\n".join(lines)
            if len(body) > 3000:
                body = body[:2940] + "\n…本地报告内容已按3000字符上限截断。"
            notice_body = body
            if len(notice_body) > 2600:
                report_ref = f"{self.db_path} (daily_reports.day={day})"
                notice_body = body[:2300] + f"\n…通知摘要已截断；完整报告保存在 {report_ref}。"
            key = f"daily:{day}"
            conn.execute("INSERT INTO daily_reports(day,body,created_at,notification_key) VALUES(?,?,?,?)",
                         (day, body, self.clock(), key))
            self._enqueue_notification(conn, key, "daily", "AutoOps 每日汇总", notice_body)
        return {"day": day, "body": body, "created": True}

    def maybe_daily_report(self) -> dict[str, Any] | None:
        now = _dt.datetime.fromtimestamp(self.clock(), LOCAL_TZ)
        if now.hour < int(self.config["daily_hour"]):
            return None
        day = (now.date() - _dt.timedelta(days=1)).isoformat()
        with contextlib.closing(self._connect()) as conn:
            exists = conn.execute("SELECT 1 FROM daily_reports WHERE day=?", (day,)).fetchone()
        if exists:
            return None
        return self.create_daily_report(day)

    def maybe_prune(self) -> dict[str, int] | None:
        """Run retention at most every 6 hours; a retention error never stops the worker."""
        now = self.clock()
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='last_retention'").fetchone()
        try:
            last = float(row["value"]) if row else 0.0
        except (TypeError, ValueError):
            last = 0.0
        if 0 <= now - last < RETENTION_INTERVAL_SECONDS:
            return None
        try:
            return self.prune_expired(now)
        except (OSError, sqlite3.Error) as exc:
            print(f"autoops retention: {_safe_text(exc, 300)}", file=sys.stderr, flush=True)
            return None

    def prune_expired(self, now: float | None = None) -> dict[str, int]:
        """Delete ledger rows, run directories and error-log lines older than retention_days.

        Never touches pending/running work, unfinished runs, breaker backoff state or
        undelivered notifications."""
        now = self.clock() if now is None else now
        cutoff = now - int(self.config.get("retention_days", 90)) * 86400
        counts: dict[str, int] = {}
        with self._transaction() as conn:
            statements = {
                "runs": ("DELETE FROM runs WHERE finished_at IS NOT NULL AND finished_at<?", (cutoff,)),
                "queue": ("DELETE FROM queue WHERE status IN ('resolved','recovered','blocked','needs_scan') AND updated_at<? "
                          "AND id NOT IN (SELECT queue_id FROM runs)", (cutoff,)),
                "notifications": ("DELETE FROM notifications WHERE state IN ('sent','suppressed') AND created_at<?", (cutoff,)),
                "daily_reports": ("DELETE FROM daily_reports WHERE created_at<?", (cutoff,)),
                "quarantines": ("DELETE FROM quarantines WHERE logged=1 AND created_at<?", (cutoff,)),
                "cooldowns": ("DELETE FROM cooldowns WHERE until_at<?", (cutoff,)),
                "breakers": ("DELETE FROM breakers WHERE failures=0 AND open_until<?", (cutoff,)),
            }
            for name, (sql, params) in statements.items():
                counts[name] = conn.execute(sql, params).rowcount
            protected = {row[0] for row in conn.execute("SELECT run_id FROM runs")}
            conn.execute("INSERT INTO metadata(key,value) VALUES('last_retention',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (str(now),))
        counts["run_dirs"] = self._prune_run_dirs(cutoff, protected)
        counts["error_log_lines"] = self._prune_error_log(cutoff)
        return counts

    def _prune_run_dirs(self, cutoff: float, protected: set[str]) -> int:
        removed = 0
        try:
            entries = list(os.scandir(self.run_root))
        except OSError:
            return 0
        for entry in entries:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry.name) or entry.name in protected:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                if not stat.S_ISDIR(st.st_mode):
                    continue
                newest = st.st_mtime
                with os.scandir(entry.path) as children:
                    for child in children:
                        newest = max(newest, child.stat(follow_symlinks=False).st_mtime)
                if newest >= cutoff:
                    continue
                # Python's rmtree uses fd-based traversal on Linux and does not follow symlinks.
                shutil.rmtree(entry.path)
                removed += 1
            except OSError:
                continue
        return removed

    def _prune_error_log(self, cutoff: float) -> int:
        try:
            data = self.error_log.read_bytes()
        except FileNotFoundError:
            return 0
        kept, dropped = [], 0
        for line in data.splitlines(keepends=True):
            try:
                at = _dt.datetime.fromisoformat(json.loads(line)["at"]).timestamp()
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                kept.append(line)
                continue
            if at < cutoff:
                dropped += 1
            else:
                kept.append(line)
        if dropped:
            fd, temp_name = tempfile.mkstemp(prefix=".errors-", dir=self.error_log.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(b"".join(kept))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.error_log)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp_name)
        return dropped

    def status(self) -> dict[str, Any]:
        """Return operational counts only; never serialize config or event payloads."""
        with contextlib.closing(self._connect()) as conn:
            counts = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) n FROM queue GROUP BY status")}
            breakers = [{"service": row["service"], "failures": row["failures"],
                         "open_until": _utc_iso(row["open_until"]) if row["open_until"] else None}
                        for row in conn.execute("SELECT service,failures,open_until FROM breakers WHERE failures>0 OR open_until>0 ORDER BY service")]
            outbox = {row["state"]: row["n"] for row in conn.execute("SELECT state,COUNT(*) n FROM notifications GROUP BY state")}
            cursor_count = conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0]
            quarantine_count = conn.execute("SELECT COUNT(*) FROM quarantines").fetchone()[0]
            return {"queue": {key: int(counts.get(key, 0)) for key in ("pending", "running", "resolved", "needs_scan", "blocked", "recovered")}, 
                    "notifications": {key: int(outbox.get(key, 0)) for key in ("pending", "sending", "sent", "suppressed")},
                    "breakers": breakers, "tracked_stream_sources": int(cursor_count),
                    "quarantined_lines": int(quarantine_count), "available_memory_mb": int(self.memory_probe())}

    def run_loop(self, *, once: bool = False, drain: bool = False, replay: bool = False) -> int:
        with FileLock(self.lock_path), self._termination_handlers():
            if replay:
                self._replay_stream()
            self._resolve_running_on_restart()
            while True:
                if self._stop_signal is not None:
                    return 128 + self._stop_signal
                self.scan_stream()
                self.maybe_daily_report()
                if self._stop_signal is None:
                    if drain:
                        self.drain()
                    else:
                        self.run_one()
                if self._stop_signal is None:
                    self.deliver_notifications()
                if self._stop_signal is None:
                    self.maybe_prune()
                if self._stop_signal is not None:
                    return 128 + self._stop_signal
                if once:
                    return 0
                time.sleep(float(self.config["poll_seconds"]))

    def run_drain(self, *, replay: bool = False, max_runs: int | None = None) -> list[dict[str, Any]]:
        with FileLock(self.lock_path), self._termination_handlers():
            if replay:
                self._replay_stream()
            self._resolve_running_on_restart()
            self.scan_stream()
            self.maybe_daily_report()
            result = self.drain(max_runs=max_runs)
            if self._stop_signal is None:
                self.deliver_notifications()
            return result


def _read_event_arg(event: str | None, file_path: str | None) -> dict[str, Any]:
    if event is not None and file_path is not None:
        raise SchedulerError("use only one of --event and --event-file")
    if event is not None:
        if len(event.encode("utf-8")) > MAX_LINE_BYTES:
            raise SchedulerError("event exceeds 1 MiB limit")
        return _json_object(event, "event")
    if file_path:
        try:
            with open(file_path, "rb") as stream:
                data = stream.read(MAX_LINE_BYTES + 1)
            if len(data) > MAX_LINE_BYTES:
                raise SchedulerError("event file exceeds 1 MiB limit")
            return _json_object(data, "event file")
        except OSError as exc:
            raise SchedulerError(f"cannot read event file: {type(exc).__name__}") from exc
    data = sys.stdin.buffer.read(MAX_LINE_BYTES + 1)
    if len(data) > MAX_LINE_BYTES:
        raise SchedulerError("stdin event exceeds 1 MiB limit")
    return _json_object(data, "stdin event")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server-autoops", description="Persistent AutoOps alert scheduler")
    parser.add_argument("--config", default=os.environ.get("AUTOOPS_CONFIG", "/etc/server-autoops/config.json"),
                        help="config.json (default: $AUTOOPS_CONFIG or /etc/server-autoops/config.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="poll the stream and run the single worker")
    run.add_argument("--once", action="store_true", help="perform one poll/worker pass and exit")
    run.add_argument("--drain", action="store_true", help="run all currently eligible queued events")
    run.add_argument("--replay", action="store_true", help="explicitly rewind the current stream inode")
    status = sub.add_parser("status", help="show redacted queue and worker state")
    enqueue = sub.add_parser("enqueue", help="persist one JSON event (stdin by default)")
    enqueue.add_argument("--event", help="event JSON string")
    enqueue.add_argument("--event-file", "--file", dest="event_file", help="read event JSON from this file")
    daily = sub.add_parser("daily", help="create and deliver one local-day summary")
    daily.add_argument("--date", help="report date YYYY-MM-DD (default: yesterday in the configured timezone)")
    replay = sub.add_parser("replay", help="rewind the current stream inode and optionally drain")
    replay.add_argument("--drain", action="store_true", help="also process eligible events")
    replay.add_argument("--max-runs", type=int, default=None)
    drain = sub.add_parser("drain", help="read the stream and process currently eligible events")
    drain.add_argument("--max-runs", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    scheduler = None
    try:
        config = load_config(args.config)
        scheduler = Scheduler(config)
        if args.command == "status":
            print(json.dumps(scheduler.status(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "enqueue":
            event = _read_event_arg(args.event, args.event_file)
            result = scheduler.enqueue_event(event)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "daily":
            result = scheduler.create_daily_report(args.date)
            delivery = scheduler.deliver_notifications()
            print(json.dumps({**result, "delivery": delivery}, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "run":
            return scheduler.run_loop(once=args.once, drain=args.drain, replay=args.replay)
        if args.command == "drain":
            results = scheduler.run_drain(max_runs=args.max_runs)
            print(json.dumps({"runs": results, "status": scheduler.status()}, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "replay":
            results = scheduler.run_drain(replay=True, max_runs=args.max_runs) if args.drain else scheduler.run_loop(once=True, replay=True)
            print(json.dumps({"runs": results if isinstance(results, list) else [], "status": scheduler.status()}, ensure_ascii=False, sort_keys=True))
            return 0
    except (SchedulerError, OSError, sqlite3.Error) as exc:
        print(f"autoops scheduler: {_safe_text(exc, 500)}", file=sys.stderr)
        return 2
    finally:
        if scheduler is not None:
            scheduler.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
