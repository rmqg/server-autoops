from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # TOML configs are rejected with a clear error
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .policy import validate_policy


MAX_MESSAGE = 128 * 1024
MAX_LOG_BYTES = 32 * 1024
MAX_LOG_FILES = 32
_SECRET = re.compile(
    r"(?i)(authorization|proxy-authorization|password|passwd|token|secret|api[_-]?key|cookie|bark[_-]?url)"
    r"([\"']?\s*[:=]\s*[\"']?)((?:bearer|basic)\s+[^\s,;\"']+|[^\s,;\"']+)"
)
_SECRET_KEY = re.compile(r"(?i)(authorization|proxy-authorization|password|passwd|token|secret|api[_-]?key|cookie|bark[_-]?url)")


class BrokerError(Exception):
    """A denied or failed structured broker request."""


class Broker:
    """Narrow, event-scoped operations for explicitly mapped services."""

    def __init__(self, policy: dict, run_dir: Path, event: dict):
        policy, errors, _warnings = validate_policy(policy)
        if errors:
            raise BrokerError("policy failed validation: " + "; ".join(errors[:3]))
        self.policy = policy
        self.run_dir = Path(run_dir)
        self.event = event
        self._lock = threading.RLock()
        self._actions_path = self.run_dir / "actions.jsonl"
        self._allowed: set[str] = set()
        self._event_fault: dict[str, Any] = {}
        self._resolve_event_targets()
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_safe_directory(self.run_dir)

    def handle(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise BrokerError("request must be an object")
        op = request.get("op")
        if op not in {"inventory", "inspect", "logs", "read_config", "patch_config", "start", "restart", "recreate", "verify", "finish"}:
            raise BrokerError("unsupported operation")
        with self._lock:
            if op == "inventory":
                return self._inventory()
            if op == "finish":
                return self._finish(request)
            target = request.get("target")
            service = self._service(target)
            if op == "inspect":
                return self._inspect(service)
            if op == "logs":
                return self._logs(service)
            if op == "read_config":
                return self._read_config(service, request.get("config"))
            if op == "patch_config":
                return self._patch_config(service, request)
            if op == "start":
                return self._start(service)
            if op == "restart":
                return self._restart(service)
            if op == "recreate":
                return self._recreate(service)
            if op == "verify":
                return self._verify(service)
        raise BrokerError("operation failed")

    def _resolve_event_targets(self) -> None:
        services = self.policy.get("services", {})
        faults = self.policy.get("faults", {})
        service_name = self.event.get("service") or self.event.get("target")
        code = self.event.get("fault_code") or self.event.get("code")
        fault = faults.get(f"{service_name}:{code}", {}) if service_name and code else {}
        if not isinstance(fault, dict):
            fault = {}
        self._event_fault = fault
        targets = fault.get("targets", [])
        if isinstance(targets, list):
            self._allowed = {name for name in targets if isinstance(name, str) and name in services and isinstance(services[name], dict)}

    def _is_protected_target(self, name: str, service: dict) -> bool:
        identities = [name, service.get("container"), (service.get("compose") or {}).get("service") if isinstance(service.get("compose"), dict) else None]
        prohibited = self.policy.get("protected_name_patterns", [])
        return any(any(word in str(identity).lower() for word in prohibited) for identity in identities if identity is not None)

    def _service(self, target: Any) -> dict:
        if not isinstance(target, str) or target not in self._allowed:
            raise BrokerError("target is not authorized for this event")
        service = self.policy.get("services", {}).get(target)
        if not isinstance(service, dict):
            raise BrokerError("target is not mapped")
        if service.get("driver", "docker") == "docker" and service.get("daemon") not in self.policy["docker"]["daemons"]:
            raise BrokerError("unmapped daemon")
        return service

    def _mutable_service(self, target: str, service: dict) -> None:
        if self._is_protected_target(target, service):
            raise BrokerError("target is permanently protected")
        if service.get("mutable") is not True or service.get("login_sensitive") is True:
            raise BrokerError("service is not eligible for mutation")
        if self._event_fault.get("needs_scan") is True:
            raise BrokerError("fault requires scanning; mutation is denied")
        if self._event_fault.get("diagnose_only") is True:
            raise BrokerError("fault is diagnosis-only")

    def _inventory(self) -> dict:
        entries = []
        for name in sorted(self._allowed):
            service = self.policy.get("services", {}).get(name, {})
            configs = service.get("configs", {})
            config_info = {alias: {"editable_keys": spec.get("editable_keys", []), "bounds": spec.get("bounds", {})} for alias, spec in configs.items() if isinstance(spec, dict)}
            entries.append({"target": name, "daemon": service.get("daemon"), "probe": service.get("probe", {}).get("kind"), "configs": config_info})
        return {"targets": entries, "fault": {"needs_scan": bool(self._event_fault.get("needs_scan")), "diagnose_only": bool(self._event_fault.get("diagnose_only"))}}

    def _docker_prefix(self, service: dict) -> list[str]:
        if service.get("driver", "docker") != "docker":
            raise BrokerError("operation needs a docker service")
        endpoint = self.policy["docker"]["daemons"].get(service.get("daemon"))
        if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
            raise BrokerError("unmapped daemon")
        return [self.policy["docker"]["bin"], "-H", endpoint]

    @staticmethod
    def _docker_env() -> dict[str, str]:
        env = os.environ.copy()
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            env.pop(key, None)
        return env

    def _docker(self, service: dict, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
        if service.get("driver") == "simulated":
            return self._simulated(service, args)
        container = service.get("container")
        if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", container):
            raise BrokerError("invalid fixed service mapping")
        cmd = [*self._docker_prefix(service), *args, container]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, shell=False, env=self._docker_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BrokerError(f"Docker operation unavailable: {type(exc).__name__}") from exc

    def _simulated(self, service: dict, args: tuple) -> subprocess.CompletedProcess:
        """Demo/testing driver: a JSON file stands in for the container state. Never used by docker services."""
        if self.policy.get("allow_simulated") is not True:
            raise BrokerError("simulated driver is disabled by policy")
        path = Path(service["state_file"])
        try:
            state = json.loads(self._read_regular(str(path)))
        except BrokerError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        action = args[0] if args else ""
        if action == "inspect":
            out = {"Status": "running" if state.get("Running") else "exited", "Running": bool(state.get("Running")),
                   "ExitCode": 0 if state.get("Running") else int(state.get("ExitCode", 1)),
                   "StartedAt": state.get("StartedAt", ""), "Restarting": False, "Paused": False, "OOMKilled": False}
            if isinstance(state.get("Health"), str):
                out["Health"] = {"Status": state["Health"]}
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        if action == "logs":
            return subprocess.CompletedProcess(args, 0, str(state.get("Logs", ""))[-MAX_LOG_BYTES:], "")
        if action in {"start", "restart"}:
            if state.get("StartFails") is True:
                return subprocess.CompletedProcess(args, 1, "", "simulated start failure")
            state["Running"] = True
            state["StartedAt"] = datetime.now(timezone.utc).isoformat()
            state["Restarts"] = int(state.get("Restarts", 0)) + 1
            self._atomic_replace(path, (json.dumps(state, sort_keys=True) + "\n").encode(), 0o600, allow_missing=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise BrokerError("operation is not supported by the simulated driver")

    def _docker_json(self, service: dict) -> dict:
        result = self._docker(service, "inspect", "--format", "{{json .State}}")
        if result.returncode != 0:
            raise BrokerError("Docker inspect failed")
        try:
            return json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise BrokerError("Docker returned invalid state") from exc

    def _probe(self, service: dict) -> dict:
        probe = service.get("probe") or {"kind": "running"}
        kind = probe.get("kind")
        state = self._docker_json(service)
        running = state.get("Running") is True
        if kind == "running" and isinstance(state.get("Health"), dict):
            health = state["Health"]
            status = health.get("Status", "missing")
            return {"ok": running and status == "healthy", "kind": "health", "running": running, "health": status}
        if kind == "running":
            return {"ok": running, "kind": kind, "running": running}
        if kind == "health":
            health = state.get("Health") or {}
            status = health.get("Status", "missing")
            return {"ok": running and status == "healthy", "kind": kind, "running": running, "health": status}
        if kind == "http":
            url = probe.get("url")
            if not isinstance(url, str):
                raise BrokerError("invalid configured HTTP probe")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise BrokerError("unsafe configured HTTP probe")
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
                with urllib.request.urlopen(req, timeout=5) as response:
                    body = response.read(64 * 1024)
                    status_code = response.status
                ok = 200 <= status_code < 300
                expected = probe.get("expected")
                json_key = probe.get("json_key")
                if ok and json_key:
                    try:
                        actual = json.loads(body.decode("utf-8"))
                        for part in str(json_key).split("."):
                            actual = actual[part]
                        ok = actual == expected
                    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                        ok = False
                return {"ok": running and ok, "kind": kind, "running": running, "http_status": status_code}
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                return {"ok": False, "kind": kind, "running": running, "error": type(exc).__name__}
        if kind == "exec":
            argv = probe.get("argv")
            if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
                raise BrokerError("invalid fixed exec probe")
            container = service.get("container")
            if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", container):
                raise BrokerError("invalid fixed service mapping")
            try:
                result = subprocess.run([*self._docker_prefix(service), "exec", container, *argv], capture_output=True, timeout=20, check=False, shell=False, env=self._docker_env())
                return {"ok": running and result.returncode == 0, "kind": kind, "running": running, "exit_code": result.returncode}
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"ok": False, "kind": kind, "running": running, "error": type(exc).__name__}
        raise BrokerError("unsupported configured probe")

    def _inspect(self, service: dict) -> dict:
        state = self._docker_json(service)
        return {"state": self._safe_state(state), "probe": self._probe(service)}

    @staticmethod
    def _safe_state(state: dict) -> dict:
        out = {}
        for key in ("Status", "Running", "Paused", "Restarting", "OOMKilled", "ExitCode", "StartedAt", "FinishedAt"):
            if key in state:
                out[key] = state[key]
        if isinstance(state.get("Health"), dict):
            out["Health"] = {k: state["Health"].get(k) for k in ("Status", "FailingStreak") if k in state["Health"]}
        return out
    def _wait_for_probe(self, service: dict, timeout: float | None = None) -> dict:
        if timeout is None:
            timeout = float(service.get("probe_timeout_seconds", 45))
        deadline = time.monotonic() + timeout
        last = {"ok": False, "error": "probe did not run"}
        while True:
            try:
                last = self._probe(service)
            except BrokerError as exc:
                last = {"ok": False, "error": str(exc)}
            if last.get("ok") is True:
                return last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last
            time.sleep(min(0.5, remaining))


    def _logs(self, service: dict) -> dict:
        result = self._docker(service, "logs", "--tail", "200")
        if result.returncode != 0:
            raise BrokerError("Docker logs failed")
        raw_logs = (result.stdout + result.stderr).encode("utf-8", errors="replace")
        logs = self._redact(raw_logs[-(MAX_LOG_BYTES // 2):].decode("utf-8", errors="ignore"))
        used = len(logs.encode("utf-8"))
        files = {}
        paths = service.get("log_files", [])
        if not isinstance(paths, list) or len(paths) > MAX_LOG_FILES:
            raise BrokerError("invalid log file mapping")
        for index, path in enumerate(paths):
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise BrokerError("invalid mapped log path")
            remaining = max(0, MAX_LOG_BYTES - used)
            allocation = remaining // (len(paths) - index) if paths else 0
            text = self._read_regular_tail(path, allocation)
            label = re.sub(r"[\x00-\x1f\x7f]", "_", Path(path).name)[:128]
            files[f"{index}:{label}"] = text
            used += len(text.encode("utf-8"))
        return {"logs": logs, "files": files}

    def _read_config(self, service: dict, alias: Any) -> dict:
        config = self._config(service, alias)
        data = self._read_regular(config["path"])
        parsed = self._parse_config(data, config["format"])
        return {"config": alias, "sha256": hashlib.sha256(data).hexdigest(), "values": self._redact_tree(parsed), "editable_keys": config.get("editable_keys", []), "bounds": config.get("bounds", {})}

    def _patch_config(self, service: dict, request: dict) -> dict:
        target = request["target"]
        self._mutable_service(target, service)
        config = self._config(service, request.get("config"))
        path = Path(config["path"])
        original = self._read_regular(path)
        current_hash = hashlib.sha256(original).hexdigest()
        if not isinstance(request.get("expected_sha256"), str) or request["expected_sha256"] != current_hash:
            raise BrokerError("configuration CAS mismatch")
        changes = request.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise BrokerError("changes must be a non-empty object")
        editable = set(config.get("editable_keys", []))
        for key, value in changes.items():
            if key not in editable or not self._is_scalar(value):
                raise BrokerError("only allow-listed scalar keys may be patched")
        parsed = self._parse_config(original, config["format"])
        bounds = config.get("bounds", {})
        if not isinstance(bounds, dict):
            raise BrokerError("invalid configuration bounds")
        for key, value in changes.items():
            if not self._path_exists(parsed, key):
                raise BrokerError("patch key must already exist")
            current = self._path_get(parsed, key)
            if not self._is_scalar(current):
                raise BrokerError("patch key must already be scalar")
            if type(value) is not type(current):
                raise BrokerError("patch value type must match the existing scalar")
            if isinstance(value, float) and not math.isfinite(value):
                raise BrokerError("non-finite numbers are not allowed")
            if key in bounds:
                limits = bounds[key]
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not isinstance(limits, list) or len(limits) != 2 or not all(isinstance(limit, (int, float)) and not isinstance(limit, bool) for limit in limits) or value < limits[0] or value > limits[1]:
                    raise BrokerError("patch value is outside configured bounds")
        replacement = self._replace_config(original, parsed, changes, config["format"])
        backup_dir = self.run_dir / "backups"
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_safe_directory(backup_dir)
        safe_target = re.sub(r"[^A-Za-z0-9_.-]", "_", target)[:80]
        safe_alias = re.sub(r"[^A-Za-z0-9_.-]", "_", str(request["config"]))[:80]
        backup = backup_dir / f"{safe_target}-{safe_alias}-{int(time.time() * 1000)}.bak"
        source_stat = path.lstat()
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise BrokerError("mapped file changed before backup")
        mode = stat.S_IMODE(source_stat.st_mode)
        self._exclusive_write(backup, original, mode, (source_stat.st_uid, source_stat.st_gid))
        self._audit({"op": "patch_config", "target": target, "config": request["config"], "backup": backup.name, "before": current_hash})
        try:
            self._atomic_replace(path, replacement, mode)
            # Parse after installation before the dependent process is restarted.
            self._parse_config(self._read_regular(path), config["format"])
            result = self._recreate_command(service) if service.get("compose") else self._docker(service, "restart")
            if result.returncode != 0 or not self._wait_for_probe(service).get("ok"):
                raise BrokerError("post-patch restart or probe failed")
            self._audit({"op": "patch_config_success", "target": target, "config": request["config"], "verified": True, "sha256": hashlib.sha256(replacement).hexdigest()})
            return {"ok": True, "sha256": hashlib.sha256(replacement).hexdigest(), "backup": backup.name, "verified": True}
        except Exception as exc:
            rollback_error = None
            try:
                self._atomic_replace(path, original, mode)
                restart = self._recreate_command(service) if service.get("compose") else self._docker(service, "restart")
                probe = self._wait_for_probe(service)
                if restart.returncode != 0 or not probe.get("ok"):
                    rollback_error = "restored file but service verification failed"
            except Exception as rollback_exc:
                rollback_error = type(rollback_exc).__name__
            self._audit({"op": "patch_config_rollback", "target": target, "config": request["config"], "rolled_back": rollback_error is None, "error": rollback_error or type(exc).__name__})
            raise BrokerError("patch failed; original configuration restored" if rollback_error is None else f"patch failed and rollback verification failed: {rollback_error}") from exc

    def _start(self, service: dict) -> dict:
        self._mutable_service_for_service(service, "start")
        before = self._probe(service)
        if before["ok"]:
            return {"ok": True, "changed": False, "before": before, "after": before}
        result = self._docker(service, "start")
        after = self._wait_for_probe(service) if result.returncode == 0 else {"ok": False, "kind": "docker-start"}
        outcome = {"ok": result.returncode == 0 and after["ok"], "changed": result.returncode == 0, "before": before, "after": after}
        self._audit({"op": "start", "container": service.get("container"), "before": before, "result": outcome["ok"]})
        return outcome

    def _restart(self, service: dict) -> dict:
        self._mutable_service_for_service(service, "restart")
        before = self._probe(service)
        result = self._docker(service, "restart")
        after = self._wait_for_probe(service) if result.returncode == 0 else {"ok": False, "kind": "docker-restart"}
        outcome = {"ok": result.returncode == 0 and after["ok"], "changed": result.returncode == 0, "before": before, "after": after, "rolled_back": False}
        self._audit({"op": "restart", "container": service.get("container"), "before": before, "result": outcome["ok"], "rolled_back": False})
        return outcome

    def _recreate(self, service: dict) -> dict:
        self._mutable_service_for_service(service, "recreate")
        if not service.get("compose"):
            raise BrokerError("recreate requires a fixed compose mapping")
        before = self._probe(service)
        result = self._recreate_command(service)
        after = self._wait_for_probe(service) if result.returncode == 0 else {"ok": False, "kind": "compose-recreate"}
        outcome = {"ok": result.returncode == 0 and after["ok"], "changed": result.returncode == 0, "before": before, "after": after, "rolled_back": False}
        self._audit({"op": "recreate", "service": service["compose"].get("service"), "before": before, "result": outcome["ok"], "rolled_back": False})
        return outcome

    def _recreate_command(self, service: dict) -> subprocess.CompletedProcess:
        mapping = service.get("compose")
        if not isinstance(mapping, dict):
            raise BrokerError("invalid compose mapping")
        compose_file, project_dir, compose_service = mapping.get("file"), mapping.get("project_dir"), mapping.get("service")
        if not isinstance(compose_file, str) or not Path(compose_file).is_absolute() or not isinstance(project_dir, str) or not Path(project_dir).is_absolute() or not isinstance(compose_service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", compose_service):
            raise BrokerError("invalid fixed compose mapping")
        # Checking the mapping file's inode and its parent chain rejects symlinks and hardlinks.
        self._read_regular(compose_file)
        self._assert_safe_directory(Path(project_dir))
        cmd = [*self._docker_prefix(service), "compose", "-f", compose_file, "--project-directory", project_dir, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", compose_service]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False, shell=False, env=self._docker_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BrokerError(f"Compose operation unavailable: {type(exc).__name__}") from exc

    def _mutable_service_for_service(self, service: dict, operation: str) -> None:
        target = next((name for name in self._allowed if self.policy.get("services", {}).get(name) is service), None)
        if target is None:
            raise BrokerError("unmapped target")
        self._mutable_service(target, service)
        if operation in {"start", "restart", "recreate"} and (self._event_fault.get("needs_scan") is True or self._event_fault.get("diagnose_only") is True):
            raise BrokerError("event does not authorize service mutation")
    def _verify(self, service: dict) -> dict:
        probe = self._probe(service)
        return {"verified": probe["ok"], "probe": probe}

    def _finish(self, request: dict) -> dict:
        results = []
        for target in sorted(self._allowed):
            service = self.policy.get("services", {}).get(target)
            try:
                result = self._probe(service)
            except BrokerError as exc:
                result = {"ok": False, "error": str(exc)}
            results.append({"target": target, "probe": result})
        verified = bool(results) and all(row["probe"].get("ok") is True for row in results)
        # A bare "running" probe (no HTTP/exec/Docker-health check) proves the container runs, not the business.
        container_only = [row["target"] for row in results if row["probe"].get("kind") == "running"]
        business_verified = verified and not container_only
        actions = self._read_audit()
        diagnosis = self._safe_summary(request.get("summary")) or "模型未提供诊断摘要"
        failed = [row["target"] for row in results if row["probe"].get("ok") is not True]
        probe_conclusion = "授权目标探针全部通过" if not failed and results else ("探针未通过目标：" + ", ".join(failed) if failed else "没有可验证的授权目标")
        if verified and container_only:
            probe_conclusion += "（" + ", ".join(container_only) + " 只有容器运行探针：容器在跑、业务未验证）"
        if self._event_fault.get("needs_scan") is True:
            status, verified, conclusion = "needs_scan", False, "需要扫描"
        elif not self._allowed:
            status, verified, conclusion = "blocked", False, "无获准目标，运维受阻"
        elif self._event_fault.get("diagnose_only") is True:
            status, verified, conclusion = "blocked", False, "该故障仅允许诊断"
        elif verified:
            changed = any(action.get("op") in {"start", "restart", "recreate", "patch_config_success"} and action.get("result", action.get("verified", False)) is True for action in actions)
            if business_verified:
                status, conclusion = ("repaired", "授权目标修复后均健康") if changed else ("healthy", "授权目标均健康")
            else:
                status, conclusion = ("repaired", "已处理，容器在跑、业务未验证") if changed else ("healthy", "容器在跑、业务未验证")
        else:
            status, verified, conclusion = "failed", False, "仍有授权目标未通过探针"
        summary = f"诊断：{diagnosis}；探针结论：{probe_conclusion}；状态：{conclusion}"
        result = {"status": status, "summary": summary, "diagnosis": diagnosis, "verification": results, "verified": verified,
                  "business_verified": bool(business_verified and verified), "actions": actions, "rolled_back": any(a.get("op") == "patch_config_rollback" and a.get("rolled_back") for a in actions)}
        out = self.run_dir / "result.json"
        self._atomic_replace(out, (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode(), 0o600, allow_missing=True)
        return result

    def _config(self, service: dict, alias: Any) -> dict:
        configs = service.get("configs", {})
        config = configs.get(alias) if isinstance(alias, str) else None
        if not isinstance(config, dict) or config.get("format") not in {"json", "toml"}:
            raise BrokerError("unknown configuration alias")
        path = config.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise BrokerError("invalid mapped configuration path")
        if not isinstance(config.get("editable_keys", []), list):
            raise BrokerError("invalid editable key mapping")
        return config

    @staticmethod
    def _assert_safe_directory(path: Path) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise BrokerError("unsafe directory path")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            try:
                st = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise BrokerError("unsafe directory path")

    def _read_regular(self, path_value: str) -> bytes:
        path = Path(path_value)
        if not path.is_absolute() or ".." in path.parts:
            raise BrokerError("unsafe mapped path")
        self._assert_safe_directory(path.parent)
        try:
            st = path.lstat()
        except OSError as exc:
            raise BrokerError("mapped file unavailable") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise BrokerError("mapped file is not a safe regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino):
                raise BrokerError("mapped file changed during access")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise BrokerError("mapped file exceeds size limit")
            return data
        finally:
            os.close(fd)
    def _read_regular_tail(self, path_value: str, limit: int) -> str:
        path = Path(path_value)
        if not path.is_absolute() or ".." in path.parts:
            raise BrokerError("unsafe mapped log path")
        self._assert_safe_directory(path.parent)
        try:
            st = path.lstat()
        except OSError as exc:
            raise BrokerError("mapped log unavailable") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise BrokerError("mapped log is not a safe regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino):
                raise BrokerError("mapped log changed during access")
            os.lseek(fd, max(0, current.st_size - limit), os.SEEK_SET)
            data = os.read(fd, limit)
            return self._redact(data.decode("utf-8", errors="ignore"))
        finally:
            os.close(fd)


    @staticmethod
    def _parse_config(data: bytes, fmt: str) -> Any:
        try:
            text = data.decode("utf-8")
            if fmt == "toml" and tomllib is None:
                raise BrokerError("TOML configs need Python 3.11+ (or the tomli package)")
            return json.loads(text) if fmt == "json" else tomllib.loads(text)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise BrokerError("invalid configuration syntax") from exc

    @staticmethod
    def _is_scalar(value: Any) -> bool:
        return value is None or isinstance(value, (str, int, float, bool)) and not isinstance(value, complex)

    @staticmethod
    def _key_path(key: Any) -> list[str]:
        if not isinstance(key, str) or not key or any(not p or p in {".", ".."} for p in key.split(".")):
            raise BrokerError("invalid configuration key")
        return key.split(".")

    @classmethod
    def _path_get(cls, value: Any, key: str) -> Any:
        for part in cls._key_path(key):
            value = value[part]
        return value

    @classmethod
    def _path_exists(cls, value: Any, key: str) -> bool:
        try:
            cls._path_get(value, key)
            return True
        except (KeyError, TypeError, BrokerError):
            return False

    @classmethod
    def _replace_config(cls, data: bytes, parsed: Any, changes: dict, fmt: str) -> bytes:
        if fmt == "json":
            for key, value in changes.items():
                cls._path_set(parsed, key, value)
            return (json.dumps(parsed, ensure_ascii=False, indent=2) + "\n").encode()
        text = data.decode("utf-8")
        lines = text.splitlines(keepends=True)
        for key, value in changes.items():
            parts = cls._key_path(key)
            if len(parts) != 1:
                raise BrokerError("TOML patch keys must be existing top-level scalar keys")
            pattern = re.compile(r"^(\s*" + re.escape(parts[0]) + r"\s*=\s*)([^#\r\n]*)(.*)$")
            matches = [i for i, line in enumerate(lines) if pattern.match(line)]
            if len(matches) != 1:
                raise BrokerError("TOML key must have one unambiguous assignment")
            index = matches[0]
            match = pattern.match(lines[index])
            literal = json.dumps(value, ensure_ascii=False) if isinstance(value, (str, bool)) or value is None else repr(value)
            lines[index] = match.group(1) + literal + match.group(3)
        result = "".join(lines).encode()
        if tomllib is None:
            raise BrokerError("TOML configs need Python 3.11+ (or the tomli package)")
        tomllib.loads(result.decode("utf-8"))
        return result

    @classmethod
    def _path_set(cls, value: dict, key: str, new_value: Any) -> None:
        parts = cls._key_path(key)
        for part in parts[:-1]:
            value = value[part]
        value[parts[-1]] = new_value

    @staticmethod
    def _redact(text: str) -> str:
        return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)

    @classmethod
    def _redact_tree(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: ("[REDACTED]" if _SECRET_KEY.search(str(k)) else cls._redact_tree(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._redact_tree(v) for v in value]
        if isinstance(value, str):
            return cls._redact(value)
        return value

    @staticmethod
    def _safe_summary(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return Broker._redact(value)[:500]

    def _audit(self, action: dict) -> None:
        record = {"at": datetime.now(timezone.utc).isoformat(), **action}
        raw = (json.dumps(self._redact_tree(record), ensure_ascii=False, sort_keys=True) + "\n").encode()
        fd = os.open(self._actions_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise BrokerError("unsafe audit file")
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read_audit(self) -> list:
        try:
            data = self._read_regular(str(self._actions_path))
            return [json.loads(line) for line in data.decode().splitlines() if line.strip()]
        except (BrokerError, UnicodeDecodeError, json.JSONDecodeError):
            return []

    @staticmethod
    def _exclusive_write(path: Path, data: bytes, mode: int, owner: tuple[int, int] | None = None) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, mode)
        try:
            if owner is not None:
                os.fchown(fd, owner[0], owner[1])
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _atomic_replace(cls, path: Path, data: bytes, mode: int, allow_missing: bool = False) -> None:
        path = Path(path)
        cls._assert_safe_directory(path.parent)
        try:
            old = path.lstat()
        except FileNotFoundError:
            if not allow_missing:
                raise BrokerError("replacement target disappeared")
            old = None
        if old is not None and (stat.S_ISLNK(old.st_mode) or not stat.S_ISREG(old.st_mode) or old.st_nlink != 1):
            raise BrokerError("unsafe replacement target")
        fd, temp_name = tempfile.mkstemp(prefix=".autoops-", dir=path.parent)
        temp = Path(temp_name)
        try:
            if old is not None:
                os.fchown(fd, old.st_uid, old.st_gid)
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            if old is not None:
                now = path.lstat()
                if (now.st_dev, now.st_ino, now.st_nlink) != (old.st_dev, old.st_ino, old.st_nlink):
                    raise BrokerError("replacement target changed")
            os.replace(temp, path)
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def serve(broker: Broker, socket_path: str | Path, token: str):
    """Start a JSONL Unix socket server. Returns a stoppable server handle."""
    if not isinstance(token, str) or not token:
        raise ValueError("token must be non-empty")
    path = Path(socket_path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("socket path must be absolute and traversal-free")
    Broker._assert_safe_directory(path.parent)
    try:
        st = path.lstat()
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISSOCK(st.st_mode) or st.st_nlink != 1:
            raise BrokerError("refusing to replace unsafe socket path")
        path.unlink()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            try:
                raw = self.rfile.readline(MAX_MESSAGE + 1)
                if not raw or len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
                    raise BrokerError("invalid or oversized JSONL message")
                message = json.loads(raw.decode("utf-8"))
                if not isinstance(message, dict) or not isinstance(message.get("token"), str) or not __import__("hmac").compare_digest(message["token"], token):
                    raise BrokerError("unauthorized")
                result = broker.handle(message.get("request"))
                response = {"ok": True, "result": result}
            except Exception as exc:
                response = {"ok": False, "error": str(exc)[:500] if isinstance(exc, BrokerError) else "request failed"}
            encoded = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(encoded) > MAX_MESSAGE:
                encoded = b'{"ok":false,"error":"response exceeds message limit"}\n'
            self.wfile.write(encoded)

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = False
        allow_reuse_address = False

        def server_close(self):
            super().server_close()
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    server = Server(str(path), Handler)
    os.chmod(path, 0o666)
    thread = threading.Thread(target=server.serve_forever, name="server-autoops-broker", daemon=True)
    thread.start()
    server.socket_path = path
    server.thread = thread
    def close():
        server.shutdown()
        server.server_close()

    server.stop = close
    return server
