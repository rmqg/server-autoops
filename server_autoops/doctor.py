"""``server-autoops doctor``: read-only self check with a fix suggestion for every problem.

It never starts an agent, never changes a container and never prints secrets.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__

OK, WARN, ERR = "ok", "warn", "error"
MARK = {OK: "✔", WARN: "!", ERR: "✘"}


class Report:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, level: str, topic: str, message: str, fix: str = "") -> None:
        self.items.append({"level": level, "topic": topic, "message": message, "fix": fix})

    @property
    def errors(self) -> int:
        return sum(1 for i in self.items if i["level"] == ERR)


def _mode_check(rep: Report, path: Path, topic: str, secret: bool) -> None:
    try:
        st = path.stat()
    except OSError:
        return
    mode = stat.S_IMODE(st.st_mode)
    if secret and mode & 0o077:
        rep.add(ERR, topic, f"{path} 权限 {oct(mode)}，其他用户能读到密钥", f"chmod 600 {path}")
    elif not secret and mode & 0o022:
        rep.add(ERR, topic, f"{path} 权限 {oct(mode)}，非 root 用户可以改写授权", f"chmod go-w {path}")
    if os.geteuid() == 0 and st.st_uid != 0:
        rep.add(WARN, topic, f"{path} 属主不是 root（uid {st.st_uid}）", f"chown root:root {path}")


def _run(argv: list[str], timeout: float = 8) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check(config_path: str) -> Report:
    rep = Report()
    if sys.version_info < (3, 10):
        rep.add(ERR, "python", f"Python {sys.version.split()[0]} 太旧", "需要 Python 3.10 或更新")
    else:
        rep.add(OK, "python", f"Python {sys.version.split()[0]}")
    try:
        import tomllib  # noqa: F401
        has_toml = True
    except ModuleNotFoundError:
        try:
            import tomli  # noqa: F401
            has_toml = True
        except ModuleNotFoundError:
            has_toml = False
    if os.geteuid() != 0:
        rep.add(WARN, "user", "当前不是 root：读 Docker socket、改受保护配置会失败", "正式运行请用 root（systemd/procd 默认就是）")

    from .scheduler import SchedulerError, load_config
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        rep.add(ERR, "config", f"找不到 {config_path}", "运行 ./install.sh，或用 --config 指定配置文件")
        return rep
    except (SchedulerError, OSError, ValueError) as exc:
        rep.add(ERR, "config", f"配置不合法：{exc}", "对照 docs/CONFIGURATION.md 修改")
        return rep
    rep.add(OK, "config", f"配置可读：{config['config_path']}")
    _mode_check(rep, Path(config["config_path"]), "config", secret=False)

    from .policy import validate_policy
    policy: dict[str, Any] = {}
    try:
        raw = json.loads(Path(config["policy"]).read_bytes())
        policy, errors, warnings = validate_policy(raw)
    except (OSError, ValueError) as exc:
        errors, warnings = [f"读不出 policy：{type(exc).__name__}"], []
    _mode_check(rep, Path(config["policy"]), "policy", secret=False)
    for e in errors:
        rep.add(ERR, "policy", e, f"server-autoops policy check {config['policy']}")
    for w in warnings:
        rep.add(WARN, "policy", w)
    if not errors:
        rep.add(OK, "policy", f"policy 校验通过：{len(policy.get('services', {}))} 个服务，{len(policy.get('faults', {}))} 个故障码")
        if not policy.get("faults"):
            rep.add(WARN, "policy", "policy 里没有任何故障码映射：所有告警都只会记为受阻", "参照 examples/policy.example.json 添加")
        if not has_toml and any(c.get("format") == "toml" for s in policy.get("services", {}).values()
                                for c in s.get("configs", {}).values()):
            rep.add(ERR, "policy", "policy 里有 TOML 配置，但本机 Python 没有 tomllib/tomli", "改用 Python 3.11+，或 pip install tomli")
        if policy.get("allow_simulated"):
            rep.add(WARN, "policy", "allow_simulated=true：模拟驱动只用于演示和测试")

    root = Path(config["root"])
    for label, path in (("数据目录", root), ("状态目录", Path(config["state_db"]).parent), ("运行记录", Path(config["runs_dir"]))):
        if not path.exists():
            rep.add(WARN, "paths", f"{label} {path} 不存在，首次运行时会创建")
        elif not os.access(path, os.W_OK):
            rep.add(ERR, "paths", f"{label} {path} 不可写", f"chown/chmod 让运行用户可写 {path}")
    stream = Path(config["stream"])
    if not stream.exists():
        rep.add(WARN, "stream", f"告警流 {stream} 还不存在", "第一条告警写入时会自动创建；也可以 touch 它")

    # Docker daemons and services
    docker_bin = policy.get("docker", {}).get("bin", "/usr/bin/docker")
    used = {s.get("daemon", "main") for s in policy.get("services", {}).values() if s.get("driver", "docker") == "docker"}
    agent = config["agent"]
    if used or agent.get("kind") == "dsh":
        if not Path(docker_bin).exists() and not shutil.which(docker_bin):
            rep.add(ERR, "docker", f"找不到 docker 程序 {docker_bin}", "安装 Docker，或在 policy.docker.bin 写对路径")
    for daemon in sorted(used):
        endpoint = policy.get("docker", {}).get("daemons", {}).get(daemon, "")
        result = _run([docker_bin, "-H", endpoint, "version", "--format", "{{.Server.Version}}"])
        if result is None or result.returncode != 0:
            rep.add(ERR, "docker", f"连不上 Docker 守护进程 {daemon}（{endpoint}）",
                    "确认 dockerd 在运行、socket 路径正确，并且以 root 运行")
            continue
        rep.add(OK, "docker", f"Docker {daemon}：{result.stdout.strip()}（{endpoint}）")
        for name, svc in sorted(policy.get("services", {}).items()):
            if svc.get("driver", "docker") != "docker" or svc.get("daemon", "main") != daemon:
                continue
            res = _run([docker_bin, "-H", endpoint, "inspect", "--format", "{{.State.Status}}", svc["container"]])
            if res is None or res.returncode != 0:
                rep.add(WARN, "services", f"{name}：容器 {svc['container']} 不存在", "检查 policy 里的 container 名")
            else:
                rep.add(OK, "services", f"{name}：容器 {svc['container']} {res.stdout.strip()}")

    # Agent
    kind = agent.get("kind")
    if kind == "dsh":
        res = _run([agent["docker_bin"], "-H", agent["docker_host"], "image", "inspect", "--format", "{{.Id}}", agent["image"]])
        if res is None or res.returncode != 0:
            rep.add(ERR, "agent", f"智能体镜像 {agent['image']} 不在本机", "按 docs/INSTALL.md 第 4 步构建 agent-image/")
        else:
            rep.add(OK, "agent", f"智能体镜像 {agent['image']} 存在")
        if not (Path(agent["plugin_dir"]) / "index.mjs").is_file():
            rep.add(ERR, "agent", f"{agent['plugin_dir']}/index.mjs 不存在", "重新运行 ./install.sh")
        env_file = Path(agent["model"]["env_file"])
        if not env_file.is_file():
            rep.add(ERR, "agent", f"模型密钥文件 {env_file} 不存在", f"cp examples/model.env.example {env_file} && chmod 600 {env_file}")
        else:
            _mode_check(rep, env_file, "agent", secret=True)
            from .runner import credential
            try:
                key = credential(env_file, agent["model"]["api_key_env"])
                if key.startswith("replace-with"):
                    rep.add(ERR, "agent", f"{env_file} 里还是示例占位符", f"把 {agent['model']['api_key_env']} 改成真实密钥")
                else:
                    rep.add(OK, "agent", f"模型密钥 {agent['model']['api_key_env']} 已配置（不显示内容）")
            except (RuntimeError, OSError, ValueError) as exc:
                rep.add(ERR, "agent", str(exc), f"在 {env_file} 里写 {agent['model']['api_key_env']}=...")
        if "example.com" in agent["model"]["base_url"] or agent["model"]["model"] == "your-model-id":
            rep.add(ERR, "agent", "agent.model 里还是示例地址/模型名", "在 config.json 填写真实的 base_url 和 model")
        if agent.get("network") == "host":
            rep.add(WARN, "agent", "智能体容器用的是 host 网络，能访问本机所有回环端口", "没有特殊需要请用 bridge")
    elif kind == "mock":
        rep.add(WARN, "agent", "agent.kind=mock：只按脚本执行，不会真正分析故障", "正式使用请改成 dsh")
    else:
        rep.add(WARN, "agent", "agent.kind=disabled：只做探针核验，不会修复")

    # Notifications
    notify = config["notify"]
    if notify["kind"] == "none":
        rep.add(WARN, "notify", "没有配置通知：需要人工处理、熔断、日报都不会推送", "见 docs/CONFIGURATION.md 的 notify 一节")
    elif notify["kind"] == "bark":
        from .notify import NotifyError, read_bark_url
        try:
            read_bark_url(notify)
            rep.add(OK, "notify", "Bark 地址已配置（不显示内容），默认级别 " + notify["default_level"])
        except NotifyError as exc:
            rep.add(ERR, "notify", str(exc), "把 Bark 地址写进 url_file 并 chmod 600")
    elif notify["kind"] == "command":
        if not os.access(notify["argv"][0], os.X_OK):
            rep.add(ERR, "notify", f"{notify['argv'][0]} 不可执行", f"chmod +x {notify['argv'][0]}")
        else:
            rep.add(OK, "notify", f"通知命令 {notify['argv'][0]}")
    else:
        rep.add(OK, "notify", f"通知写入文件 {notify['path']}")
    if any(level == "timeSensitive" for level in notify.get("levels", {}).values()) or notify.get("default_level") == "timeSensitive":
        rep.add(WARN, "notify", "有通知被设成 timeSensitive（会打断专注模式）", "默认推荐 passive")

    # Memory
    from .scheduler import available_memory_mb
    free = available_memory_mb()
    if free < int(config["min_available_mb"]):
        rep.add(WARN, "memory", f"可用内存 {free} MiB 低于 min_available_mb={config['min_available_mb']}，现在不会启动智能体")
    else:
        rep.add(OK, "memory", f"可用内存 {free} MiB（门槛 {config['min_available_mb']} MiB）")

    # Service manager
    if Path("/run/systemd/system").exists() and shutil.which("systemctl"):
        res = _run(["systemctl", "is-active", "server-autoops"])
        state = res.stdout.strip() if res else "unknown"
        rep.add(OK if state == "active" else WARN, "service", f"systemd：server-autoops {state}",
                "" if state == "active" else "systemctl enable --now server-autoops")
    elif Path("/etc/init.d/server-autoops").exists() and Path("/sbin/procd").exists():
        res = _run(["/etc/init.d/server-autoops", "status"])
        state = (res.stdout.strip() if res else "") or "unknown"
        rep.add(OK if "running" in state else WARN, "service", f"procd：{state}",
                "" if "running" in state else "/etc/init.d/server-autoops enable && /etc/init.d/server-autoops start")
    else:
        rep.add(WARN, "service", "没有检测到 systemd 或 procd 服务", "前台运行：server-autoops run；或用 ./install.sh --service systemd|procd")

    # Queue state (read-only; never creates the database)
    db = Path(config["state_db"])
    if db.exists():
        try:
            with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)) as conn:
                counts = dict(conn.execute("SELECT status,COUNT(*) FROM queue GROUP BY status").fetchall())
                breakers = conn.execute("SELECT COUNT(*) FROM breakers WHERE open_until>strftime('%s','now')").fetchone()[0]
                outbox = dict(conn.execute("SELECT state,COUNT(*) FROM notifications GROUP BY state").fetchall())
            rep.add(OK, "state", f"队列 {counts or '空'}；熔断中服务 {breakers}；通知 {outbox or '空'}")
            if counts.get("needs_scan"):
                rep.add(WARN, "state", f"有 {counts['needs_scan']} 条告警等人工检查", "server-autoops status；看 runs/<run_id>/result.json")
            if outbox.get("pending", 0) > 5:
                rep.add(WARN, "state", f"{outbox['pending']} 条通知没发出去", "server-autoops notify-test 检查通知通道")
        except sqlite3.Error as exc:
            rep.add(ERR, "state", f"读不了状态库：{type(exc).__name__}", f"检查 {db} 的权限")
    else:
        rep.add(WARN, "state", "状态库还没创建（服务还没运行过）")
    return rep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server-autoops doctor", description="read-only self check")
    parser.add_argument("--config", default=os.environ.get("AUTOOPS_CONFIG", "/etc/server-autoops/config.json"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rep = check(args.config)
    if args.json:
        print(json.dumps({"version": __version__, "errors": rep.errors, "items": rep.items}, ensure_ascii=False, indent=2))
    else:
        print(f"server-autoops {__version__} 自检（{args.config}）")
        for item in rep.items:
            line = f"  {MARK[item['level']]} [{item['topic']}] {item['message']}"
            if item["fix"] and item["level"] != OK:
                line += f"\n      修复：{item['fix']}"
            print(line)
        warns = sum(1 for i in rep.items if i["level"] == WARN)
        print(f"\n{rep.errors} 个错误，{warns} 个提醒。" + ("可以运行。" if rep.errors == 0 else "请先处理错误。"))
    return 1 if rep.errors else 0


def notify_test(argv: list[str] | None = None) -> int:
    from .notify import NotifyError, make_notifier
    from .scheduler import SchedulerError, load_config
    parser = argparse.ArgumentParser(prog="server-autoops notify-test")
    parser.add_argument("--config", default=os.environ.get("AUTOOPS_CONFIG", "/etc/server-autoops/config.json"))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        ok = make_notifier(config["notify"])("server-autoops 测试通知", "这是一条测试通知，不用处理。", "test")
    except (SchedulerError, NotifyError, OSError) as exc:
        print(f"notify-test: {exc}", file=sys.stderr)
        return 2
    print("已发送" if ok else "发送失败（通知后端返回失败）")
    return 0 if ok else 1
