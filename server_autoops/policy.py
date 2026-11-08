"""policy.json validation (format version 1).

The policy is the *only* source of authority for the agent: which services exist,
how the broker reaches them, which probes decide success, which configuration
scalars may be edited, and which fault codes authorise which targets. Anything
not described here is denied. ``validate_policy`` returns ``(policy, errors,
warnings)``; callers must refuse to act when ``errors`` is non-empty.
``schema/policy.schema.json`` mirrors these rules for editors.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

POLICY_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
CODE_RE = re.compile(r"^[^\s:]{1,160}:[^\s]{1,240}$")
KEY_PATH_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
DEFAULT_DAEMONS = {"main": "unix:///var/run/docker.sock"}
DEFAULT_PROTECTED = ["autoops", "broker", "listener"]
PROBE_KINDS = ("running", "health", "http", "exec")
DRIVERS = ("docker", "simulated")
TOP_KEYS = {"$schema", "version", "description", "docker", "protected_name_patterns",
            "allow_simulated", "code_aliases", "services", "faults"}
SERVICE_KEYS = {"description", "driver", "daemon", "container", "mutable", "login_sensitive",
                "probe", "probe_timeout_seconds", "log_files", "configs", "compose", "state_file"}
FAULT_KEYS = {"description", "targets", "needs_scan", "diagnose_only"}


class PolicyError(ValueError):
    """Raised by ``load_policy`` when the policy has errors."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""))
        self.errors = errors


def _abs_clean(value: Any) -> bool:
    return isinstance(value, str) and os.path.isabs(value) and ".." not in Path(value).parts and "\x00" not in value


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_policy(raw: Any) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["policy must be a JSON object"], []
    policy = json.loads(json.dumps(raw))  # deep copy, JSON types only
    for key in sorted(set(policy) - TOP_KEYS):
        errors.append(f"unknown top-level key {key!r}")
    version = policy.setdefault("version", POLICY_VERSION)
    if version != POLICY_VERSION:
        errors.append(f"version must be {POLICY_VERSION}")

    docker = policy.setdefault("docker", {})
    if not isinstance(docker, dict) or set(docker) - {"bin", "daemons"}:
        errors.append("docker must be an object with optional keys bin, daemons")
        docker = {}
        policy["docker"] = docker
    docker.setdefault("bin", "/usr/bin/docker")
    if not _abs_clean(docker["bin"]):
        errors.append("docker.bin must be an absolute path")
    daemons = docker.setdefault("daemons", dict(DEFAULT_DAEMONS))
    if not isinstance(daemons, dict) or not daemons:
        errors.append("docker.daemons must be a non-empty object")
        daemons = {}
    for name, endpoint in daemons.items():
        if not NAME_RE.match(name):
            errors.append(f"docker.daemons key {name!r} is not a valid name")
        if not isinstance(endpoint, str) or not endpoint.startswith("unix:///") or ".." in endpoint:
            errors.append(f"docker.daemons.{name} must be a local unix:///absolute/path socket (TCP is not allowed)")

    protected = policy.setdefault("protected_name_patterns", list(DEFAULT_PROTECTED))
    if not isinstance(protected, list) or not all(isinstance(p, str) and p and p == p.lower() for p in protected):
        errors.append("protected_name_patterns must be a list of lower-case substrings")
        protected = []
    allow_sim = policy.setdefault("allow_simulated", False)
    if not isinstance(allow_sim, bool):
        errors.append("allow_simulated must be boolean")
    if allow_sim:
        warnings.append("allow_simulated is true: simulated services are for demo/testing only")

    services = policy.setdefault("services", {})
    if not isinstance(services, dict):
        errors.append("services must be an object")
        services = {}
        policy["services"] = services
    for name, svc in services.items():
        where = f"services.{name}"
        if not NAME_RE.match(name):
            errors.append(f"{where}: service name must match [A-Za-z0-9_.-]{{1,128}}")
        if not isinstance(svc, dict):
            errors.append(f"{where} must be an object")
            continue
        for key in sorted(set(svc) - SERVICE_KEYS):
            errors.append(f"{where}: unknown key {key!r}")
        driver = svc.setdefault("driver", "docker")
        if driver not in DRIVERS:
            errors.append(f"{where}.driver must be one of {', '.join(DRIVERS)}")
        for flag in ("mutable", "login_sensitive"):
            svc.setdefault(flag, False)
            if not isinstance(svc[flag], bool):
                errors.append(f"{where}.{flag} must be boolean")
        if driver == "docker":
            if svc.get("daemon", "main") not in daemons:
                errors.append(f"{where}.daemon must name a key of docker.daemons")
            svc.setdefault("daemon", "main")
            if not isinstance(svc.get("container"), str) or not NAME_RE.match(svc["container"]):
                errors.append(f"{where}.container must be a container name [A-Za-z0-9_.-]")
            if "state_file" in svc:
                errors.append(f"{where}.state_file is only valid for driver simulated")
        elif driver == "simulated":
            if not allow_sim:
                errors.append(f"{where}: driver simulated requires allow_simulated: true")
            if not _abs_clean(svc.get("state_file")):
                errors.append(f"{where}.state_file must be an absolute path")
            svc.setdefault("container", name)
        probe = svc.setdefault("probe", {"kind": "running"})
        if not isinstance(probe, dict) or probe.get("kind") not in PROBE_KINDS:
            errors.append(f"{where}.probe.kind must be one of {', '.join(PROBE_KINDS)}")
        else:
            kind = probe["kind"]
            allowed = {"kind"} | {"http": {"url", "json_key", "expected"}, "exec": {"argv"}}.get(kind, set())
            for key in sorted(set(probe) - allowed):
                errors.append(f"{where}.probe: key {key!r} is not valid for kind {kind}")
            if kind == "http":
                url = probe.get("url")
                from urllib.parse import urlsplit
                parts = urlsplit(url) if isinstance(url, str) else None
                if not parts or parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
                    errors.append(f"{where}.probe.url must be an http(s) URL without credentials")
                if "json_key" in probe and (not isinstance(probe["json_key"], str) or not KEY_PATH_RE.match(probe["json_key"])):
                    errors.append(f"{where}.probe.json_key must be a dotted key path")
                if "json_key" in probe and "expected" not in probe:
                    errors.append(f"{where}.probe: json_key needs expected")
            if kind == "exec":
                argv = probe.get("argv")
                if driver != "docker":
                    errors.append(f"{where}.probe: exec probes need driver docker")
                if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
                    errors.append(f"{where}.probe.argv must be a non-empty list of strings")
            if kind == "running" and svc.get("mutable"):
                warnings.append(f"{where}: only a 'running' probe; successful runs are reported as 'container running, business unverified'")
        timeout = svc.setdefault("probe_timeout_seconds", 45)
        if not _number(timeout) or not 1 <= timeout <= 600:
            errors.append(f"{where}.probe_timeout_seconds must be between 1 and 600")
        logs = svc.setdefault("log_files", [])
        if not isinstance(logs, list) or len(logs) > 32 or not all(_abs_clean(p) for p in logs):
            errors.append(f"{where}.log_files must be a list (max 32) of absolute paths")
        configs = svc.setdefault("configs", {})
        if not isinstance(configs, dict):
            errors.append(f"{where}.configs must be an object")
            configs = {}
        for alias, spec in configs.items():
            cw = f"{where}.configs.{alias}"
            if not NAME_RE.match(alias):
                errors.append(f"{cw}: alias must match [A-Za-z0-9_.-]")
            if not isinstance(spec, dict):
                errors.append(f"{cw} must be an object")
                continue
            for key in sorted(set(spec) - {"path", "format", "editable_keys", "bounds", "description"}):
                errors.append(f"{cw}: unknown key {key!r}")
            if not _abs_clean(spec.get("path")):
                errors.append(f"{cw}.path must be an absolute path without '..'")
            if spec.get("format") not in ("json", "toml"):
                errors.append(f"{cw}.format must be json or toml")
            keys = spec.setdefault("editable_keys", [])
            if not isinstance(keys, list) or not all(isinstance(k, str) and KEY_PATH_RE.match(k) for k in keys):
                errors.append(f"{cw}.editable_keys must be a list of dotted key paths")
                keys = []
            if spec.get("format") == "toml" and any("." in k for k in keys):
                errors.append(f"{cw}: TOML editable_keys must be top-level keys")
            if not keys:
                warnings.append(f"{cw}: no editable_keys; the agent can read but never patch it")
            bounds = spec.setdefault("bounds", {})
            if not isinstance(bounds, dict):
                errors.append(f"{cw}.bounds must be an object")
                bounds = {}
            for key, limits in bounds.items():
                if key not in keys:
                    errors.append(f"{cw}.bounds.{key} is not in editable_keys")
                if not (isinstance(limits, list) and len(limits) == 2 and all(_number(x) for x in limits) and limits[0] <= limits[1]):
                    errors.append(f"{cw}.bounds.{key} must be [min, max] numbers with min <= max")
        compose = svc.get("compose")
        if compose is not None:
            if driver != "docker":
                errors.append(f"{where}.compose needs driver docker")
            if not isinstance(compose, dict) or set(compose) - {"file", "service", "project_dir"}:
                errors.append(f"{where}.compose must have only file, service, project_dir")
            elif not (_abs_clean(compose.get("file")) and _abs_clean(compose.get("project_dir"))
                      and isinstance(compose.get("service"), str) and NAME_RE.match(compose["service"])):
                errors.append(f"{where}.compose needs absolute file/project_dir and a service name")
        identities = [name, svc.get("container"), (compose or {}).get("service") if isinstance(compose, dict) else None]
        hits = [p for p in protected if any(p in str(i).lower() for i in identities if i)]
        if hits and svc.get("mutable"):
            warnings.append(f"{where}: name matches protected pattern(s) {hits}; the broker will refuse every mutation")
        if svc.get("login_sensitive") and svc.get("mutable"):
            warnings.append(f"{where}: login_sensitive services are never mutated even if mutable is true")

    faults = policy.setdefault("faults", {})
    if not isinstance(faults, dict):
        errors.append("faults must be an object")
        faults = {}
        policy["faults"] = faults
    referenced: set[str] = set()
    for key, fault in faults.items():
        fw = f"faults.{key}"
        if not CODE_RE.match(key):
            errors.append(f"{fw}: key must be '<service>:<FAULT_CODE>' without spaces")
        if not isinstance(fault, dict):
            errors.append(f"{fw} must be an object")
            continue
        for k in sorted(set(fault) - FAULT_KEYS):
            errors.append(f"{fw}: unknown key {k!r}")
        targets = fault.get("targets")
        if not isinstance(targets, list) or not targets:
            errors.append(f"{fw}.targets must be a non-empty list")
            targets = []
        for target in targets:
            if target not in services:
                errors.append(f"{fw}.targets: {target!r} is not a defined service")
            else:
                referenced.add(target)
        for flag in ("needs_scan", "diagnose_only"):
            fault.setdefault(flag, False)
            if not isinstance(fault[flag], bool):
                errors.append(f"{fw}.{flag} must be boolean")
        if fault.get("needs_scan") and fault.get("diagnose_only"):
            warnings.append(f"{fw}: needs_scan already implies no mutation; diagnose_only is redundant")
    for name in services:
        if name not in referenced:
            warnings.append(f"services.{name} is not a target of any fault; the agent can never reach it")

    aliases = policy.setdefault("code_aliases", {})
    if not isinstance(aliases, dict):
        errors.append("code_aliases must be an object")
    else:
        for key, value in aliases.items():
            if not CODE_RE.match(key):
                errors.append(f"code_aliases.{key}: key must be '<service>:<CODE>'")
            if (not isinstance(value, list) or not value or len(value) > 16
                    or not all(isinstance(v, str) and v and not any(ch.isspace() for ch in v) for v in value)):
                errors.append(f"code_aliases.{key} must be a list of 1-16 codes without spaces")
    return policy, errors, warnings


def load_policy(path: str | os.PathLike[str]) -> tuple[dict[str, Any], list[str]]:
    """Read and validate; raise PolicyError on any error. Returns (policy, warnings)."""
    try:
        raw = json.loads(Path(path).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError([f"cannot read policy: {type(exc).__name__}: {exc}"]) from exc
    policy, errors, warnings = validate_policy(raw)
    if errors:
        raise PolicyError(errors)
    return policy, warnings


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="server-autoops policy", description="validate a policy.json")
    parser.add_argument("action", choices=["check"])
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(Path(args.path).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raw, errors, warnings = None, [f"cannot read policy: {type(exc).__name__}: {exc}"], []
    else:
        _, errors, warnings = validate_policy(raw)
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings}, ensure_ascii=False, indent=2))
    else:
        for e in errors:
            print(f"错误  {e}")
        for w in warnings:
            print(f"提醒  {w}")
        print("policy 校验通过" if not errors else f"policy 校验失败：{len(errors)} 个错误")
    return 0 if not errors else 1
