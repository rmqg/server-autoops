"""``server-autoops setup``: environment check and the interactive install wizard.

install.sh calls ``setup --check-only`` before copying anything, and ``setup``
after it has written the default configuration. The wizard can be re-run at any
time (``sudo server-autoops setup``) to switch the agent, the model key or the
notification address. Secrets are written to 600 files and never printed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

QUICKSTART_SERVICE = "quickstart-web"
QUICKSTART_CONTAINER = "quickstart-web"
QUICKSTART_PORT = 18080
QUICKSTART_CODE = "WEB_DOWN"
ANSWER_KEYS = {"AUTOOPS_AGENT", "AUTOOPS_MODEL_BASE_URL", "AUTOOPS_MODEL", "AUTOOPS_MODEL_API_KEY",
               "AUTOOPS_BARK_URL", "AUTOOPS_TIMEZONE", "AUTOOPS_QUICKSTART_POLICY"}


# ---------------------------------------------------------------- environment check

def _os_release() -> dict:
    info: dict[str, str] = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    info[key] = value.strip().strip('"')
            break
        except OSError:
            continue
    return info


def package_manager() -> str:
    for name in ("apt-get", "opkg", "dnf", "yum", "apk", "pacman", "zypper"):
        if shutil.which(name):
            return name
    return ""


def install_hint(apt: str, opkg: str = "", dnf: str = "", apk: str = "") -> str:
    """Return the install command for this machine's package manager."""
    pm = package_manager()
    table = {"apt-get": f"apt-get update && apt-get install -y {apt}" if apt else "",
             "opkg": f"opkg update && opkg install {opkg}" if opkg else "",
             "dnf": f"dnf install -y {dnf}" if dnf else "", "yum": f"yum install -y {dnf}" if dnf else "",
             "apk": f"apk add {apk}" if apk else ""}
    command = table.get(pm, "")
    if command:
        return command
    return f"用系统的包管理器安装（Debian/Ubuntu 的包名：{apt}）" if apt else "按你的发行版文档安装"


def _mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return -1


def _run(argv: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, type(exc).__name__


def environment(state_dir: str = "/var/lib/server-autoops") -> list[dict]:
    """Each item: name, status (ok|warn|fail), detail, fix. fail = cannot install."""
    items: list[dict] = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        items.append({"name": name, "status": status, "detail": detail, "fix": fix})

    osr = _os_release()
    add("系统", "ok", osr.get("PRETTY_NAME") or platform.system())
    add("内核", "ok", platform.release())
    add("CPU 架构", "ok", platform.machine())
    if sys.version_info >= (3, 10):
        add("Python", "ok", platform.python_version())
    else:
        add("Python", "fail", f"{platform.python_version()}，需要 3.10 或更新", install_hint("python3", "python3", "python3", "python3"))
    try:
        import sqlite3  # noqa: F401
        add("Python sqlite3 模块", "ok", "有")
    except ImportError:
        add("Python sqlite3 模块", "fail", "没有（队列要用）", install_hint("python3", "python3-sqlite3", "python3", "python3"))
    try:
        import zoneinfo
        zoneinfo.ZoneInfo("Asia/Shanghai")
        add("时区数据 tzdata", "ok", "有")
    except Exception:
        add("时区数据 tzdata", "warn", "没有：时区会改用固定偏移（如 UTC+08:00），不影响使用", install_hint("tzdata", "zoneinfo-asia", "tzdata", "tzdata"))
    docker = shutil.which("docker")
    if not docker:
        add("Docker", "warn", "没有安装：离线演示不需要；自动修复容器、跑智能体需要",
            install_hint("docker.io", "docker dockerd", "docker", "docker") + "，装好后 systemctl enable --now docker")
    else:
        code, text = _run([docker, "version", "--format", "{{.Server.Version}}"])
        if code == 0:
            add("Docker", "ok", f"服务端 {text.splitlines()[0] if text else '?'}（{docker}）")
        elif os.geteuid() != 0:
            add("Docker", "warn", "连不上 Docker 守护进程（当前不是 root）", "用 sudo 运行安装")
        else:
            add("Docker", "warn", "装了 docker 命令，但守护进程没在运行", "systemctl enable --now docker（OpenWrt：/etc/init.d/dockerd start）")
    mem = _mem_available_mb()
    if mem < 0:
        add("可用内存", "warn", "读不到 /proc/meminfo")
    else:
        add("可用内存", "ok" if mem >= 1024 else "warn", f"{mem} MiB",
            "" if mem >= 1024 else "低于 1024 MiB 时不会启动智能体；可以在 config.json 调低 min_available_mb")
    probe = Path(state_dir)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free_mb = shutil.disk_usage(probe).free // (1024 * 1024)
        add("磁盘空间", "ok" if free_mb >= 2048 else "warn", f"{probe} 所在分区剩余 {free_mb} MiB",
            "" if free_mb >= 2048 else "智能体镜像约几百 MB，运行记录会慢慢增长；先清理一些空间")
    except OSError:
        pass
    if Path("/run/systemd/system").exists() and shutil.which("systemctl"):
        add("服务管理器", "ok", "systemd")
    elif Path("/sbin/procd").exists():
        add("服务管理器", "ok", "OpenWrt procd")
    else:
        add("服务管理器", "warn", "没有 systemd 或 procd：不会注册开机自启", "装完后用 server-autoops run 前台运行，或自己配置常驻")
    if os.geteuid() != 0:
        add("运行用户", "warn", "不是 root：只能装到自己有写权限的目录", "正式安装请用 sudo ./install.sh")
    else:
        add("运行用户", "ok", "root")
    return items


def print_environment(items: list[dict]) -> int:
    marks = {"ok": "  ok ", "warn": "提醒 ", "fail": "缺少 "}
    print("==> 检查环境")
    for item in items:
        print(f"  {marks[item['status']]} {item['name']}：{item['detail']}")
        if item["fix"] and item["status"] != "ok":
            print(f"        怎么办：{item['fix']}")
    failed = [i for i in items if i["status"] == "fail"]
    if failed:
        print("\n有必需的东西缺失，先按上面「怎么办」装好，再重新运行 ./install.sh")
        return 1
    return 0


# ---------------------------------------------------------------- answers

def read_answers(path: str) -> dict:
    """KEY=VALUE lines; # comments; values may be quoted. The file is never executed."""
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"错误：找不到答案文件 {path}")
    if p.stat().st_mode & 0o077 and _has_secret(p):
        print(f"提醒：{path} 里有密钥，但别人也能读。建议 chmod 600 {path}", file=sys.stderr)
    answers: dict[str, str] = {}
    for number, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in ANSWER_KEYS:
            raise SystemExit(f"错误：{path} 第 {number} 行看不懂：{key or raw!r}。可用的键：{', '.join(sorted(ANSWER_KEYS))}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        answers[key] = value
    return answers


def _has_secret(p: Path) -> bool:
    try:
        return bool(re.search(r"^\s*(export\s+)?(AUTOOPS_MODEL_API_KEY|AUTOOPS_BARK_URL)\s*=\s*\S", p.read_text(encoding="utf-8"), re.M))
    except OSError:
        return False


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}：").strip()
    except EOFError:
        value = ""
    return value or default


def _choose(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    print(prompt)
    for index, (key, label) in enumerate(options, 1):
        print(f"  {index}) {label}{'  ← 默认' if key == default else ''}")
    while True:
        value = _ask("输入编号，直接回车用默认")
        if not value:
            return default
        if value.isdigit() and 1 <= int(value) <= len(options):
            return options[int(value) - 1][0]
        print("  请输入上面列出的编号。")


def _yes(prompt: str, default: bool) -> bool:
    value = _ask(prompt + ("（Y/n）" if default else "（y/N）")).lower()
    return default if not value else value in ("y", "yes", "是", "好")


# ---------------------------------------------------------------- writing files

def _atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_env_value(path: Path, name: str, value: str) -> None:
    lines = []
    if path.is_file():
        lines = [line for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip().removeprefix("export ").partition("=")[0].strip() != name]
    lines.append(f"{name}={json.dumps(value)}" if re.search(r"[\s#'\"\\]", value) else f"{name}={value}")
    _atomic_write(path, "\n".join(lines) + "\n", 0o600)


def quickstart_policy(docker_bin: str) -> dict:
    return {
        "version": 1,
        "description": "入门演练：只管理 quickstart-web 这一个容器，故障时只允许启动/重启它。正式使用时换成你自己的白名单",
        "docker": {"bin": docker_bin, "daemons": {"main": "unix:///var/run/docker.sock"}},
        "services": {QUICKSTART_SERVICE: {
            "description": "入门演练用的 nginx 网站",
            "container": QUICKSTART_CONTAINER,
            "mutable": True,
            "probe": {"kind": "http", "url": f"http://127.0.0.1:{QUICKSTART_PORT}/"},
            "probe_timeout_seconds": 30}},
        "faults": {f"{QUICKSTART_SERVICE}:{QUICKSTART_CODE}": {"targets": [QUICKSTART_SERVICE]}},
    }


def _policy_is_empty(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not data.get("services") and not data.get("faults")


# ---------------------------------------------------------------- wizard

def wizard(config_path: Path, answers: dict | None, interactive: bool) -> int:
    from .scheduler import SchedulerError, load_config

    answers = dict(answers or {})
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"错误：找不到 {config_path}。先运行 sudo ./install.sh 生成默认配置。", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"错误：{config_path} 不是合法的 JSON（{exc}）。修好或删掉它后重新运行 ./install.sh。", file=sys.stderr)
        return 1
    etc = config_path.parent
    agent = config.setdefault("agent", {"kind": "dsh"})
    policy_path = Path(config.get("policy") or etc / "policy.json")
    model_env = Path(agent.get("model", {}).get("env_file") or etc / "model.env")
    key_name = agent.get("model", {}).get("api_key_env", "AUTOOPS_MODEL_API_KEY")

    print("\n==> 配置向导（随时可以重新运行：sudo server-autoops setup）")
    current = agent.get("kind", "dsh")
    if interactive and "AUTOOPS_AGENT" not in answers:
        answers["AUTOOPS_AGENT"] = _choose(
            "\n第 1 步：智能体（出故障时由谁来修）",
            [("quickstart", "入门演练：不需要模型。装一个示例白名单，只管理演练容器 quickstart-web，故障时自动把它启动起来"),
             ("dsh", "接真实模型：需要一个 OpenAI 兼容接口的地址和 API key（例如 DeepSeek）"),
             ("disabled", "暂时不修：只记录告警、核对服务是否健康"),
             ("keep", f"保持现在的设置（{current}）")],
            "quickstart" if _policy_is_empty(policy_path) and current == "dsh"
            and "example.com" in agent.get("model", {}).get("base_url", "example.com") else "keep")
    choice = answers.get("AUTOOPS_AGENT", "keep") or "keep"
    if choice not in ("quickstart", "dsh", "disabled", "keep", "mock"):
        print(f"错误：AUTOOPS_AGENT 只能是 quickstart、dsh、disabled、keep，收到 {choice!r}", file=sys.stderr)
        return 1

    changed: list[str] = []
    if choice == "quickstart":
        docker_bin = shutil.which("docker") or "/usr/bin/docker"
        install_policy = answers.get("AUTOOPS_QUICKSTART_POLICY", "yes").lower() not in ("no", "0", "false")
        if install_policy and not _policy_is_empty(policy_path):
            print(f"  {policy_path} 里已经有白名单，入门演练不会覆盖它。")
            install_policy = False
        if install_policy:
            _atomic_write(policy_path, json.dumps(quickstart_policy(docker_bin), ensure_ascii=False, indent=2) + "\n", 0o644)
            changed.append(f"白名单 {policy_path}：只授权容器 {QUICKSTART_CONTAINER}")
        agent.clear()
        agent.update({"kind": "mock", "plans": {f"{QUICKSTART_SERVICE}:{QUICKSTART_CODE}": [
            {"op": "summary", "text": "入门演练：容器停了，按白名单把它启动起来"},
            {"op": "start", "target": QUICKSTART_SERVICE}]}})
        changed.append("智能体：入门演练（agent.kind=mock，不需要模型）")
    elif choice == "dsh":
        model = dict(agent.get("model") or {})
        defaults = {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}
        if "example.com" not in model.get("base_url", "example.com"):
            defaults["base_url"] = model["base_url"]
        if model.get("model") not in (None, "", "your-model-id"):
            defaults["model"] = model["model"]
        base_url = answers.get("AUTOOPS_MODEL_BASE_URL") or (_ask("\n第 1 步之一：模型接口地址（OpenAI 兼容，以 /v1 结尾）", defaults["base_url"]) if interactive else defaults["base_url"])
        model_id = answers.get("AUTOOPS_MODEL") or (_ask("第 1 步之二：模型 ID", defaults["model"]) if interactive else defaults["model"])
        if not re.match(r"^https?://", base_url):
            print(f"错误：模型地址要以 http:// 或 https:// 开头，收到 {base_url!r}", file=sys.stderr)
            return 1
        key = answers.get("AUTOOPS_MODEL_API_KEY", "")
        if not key and interactive:
            try:
                key = getpass.getpass("第 1 步之三：API key（输入时不显示，直接回车表示以后再填）：").strip()
            except EOFError:
                key = ""
        prefix = Path(__file__).resolve().parent.parent
        base = {"kind": "dsh", "image": "local/server-autoops-agent:0.1.0", "docker_bin": shutil.which("docker") or "/usr/bin/docker",
                "docker_host": "unix:///var/run/docker.sock", "network": "bridge", "memory": "512m", "cpus": 0.75,
                "pids_limit": 96, "tmpfs_mb": 96, "user": "1000:1000", "plugin_dir": str(prefix / "plugin")}
        kept = {k: v for k, v in agent.items() if k not in ("kind", "plans", "model")} if current == "dsh" else {}
        model.update({"provider_id": model.get("provider_id", "autoops-model"), "display_name": model.get("display_name", "AutoOps model"),
                      "api": model.get("api", "openai-completions"), "base_url": base_url, "model": model_id,
                      "context_window": model.get("context_window", 131072), "max_tokens": model.get("max_tokens", 8192),
                      "api_key_env": key_name, "env_file": str(model_env)})
        agent.clear()
        agent.update({**base, **kept, "kind": "dsh", "model": model})
        if key:
            _write_env_value(model_env, key_name, key)
            changed.append(f"模型密钥写进 {model_env}（权限 600，不显示内容）")
        elif not model_env.is_file():
            _write_env_value(model_env, key_name, "replace-with-your-key")
        changed.append(f"智能体：真实模型 {model_id}（{base_url}）")
        if not key:
            changed.append(f"还没填 API key：以后再运行 sudo server-autoops setup，或编辑 {model_env}")
    elif choice == "disabled":
        agent.clear()
        agent.update({"kind": "disabled"})
        changed.append("智能体：暂时不修（agent.kind=disabled）")

    bark = answers.get("AUTOOPS_BARK_URL")
    if bark is None and interactive:
        print("\n第 2 步：手机通知（可选）。用 iPhone 的 Bark App 收通知：打开 App，复制首页上那个 https://api.day.app/... 地址。")
        bark = _ask("Bark 地址（直接回车跳过，以后可以再运行 sudo server-autoops setup）")
    if bark:
        if not re.match(r"^https?://\S+$", bark):
            print("错误：Bark 地址要以 https:// 开头、中间不能有空格", file=sys.stderr)
            return 1
        notify = config.setdefault("notify", {})
        url_file = Path(notify.get("url_file") or etc / "bark.url")
        _atomic_write(url_file, bark.strip() + "\n", 0o600)
        group = notify.get("group", "server-autoops")
        config["notify"] = {**{k: v for k, v in notify.items() if k not in ("kind", "path", "argv")},
                            "kind": "bark", "url_file": str(url_file), "group": group,
                            "default_level": notify.get("default_level", "passive")}
        changed.append(f"通知：Bark，地址写进 {url_file}（权限 600，不显示内容）")

    timezone = answers.get("AUTOOPS_TIMEZONE")
    if timezone is None and interactive:
        timezone = _ask("\n第 3 步：日报和日志用的时区", config.get("timezone", "Asia/Shanghai"))
    if timezone and timezone != config.get("timezone"):
        config["timezone"] = timezone
        changed.append(f"时区：{timezone}")

    # Validate before writing: a config that does not load is never left behind.
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "config.json"
        probe.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        try:
            load_config(str(probe))
        except (SchedulerError, ValueError, OSError) as exc:
            print(f"错误：这些设置组合起来不合法：{exc}\n  没有写入任何改动。对照 docs/CONFIGURATION.md 检查后重新运行。", file=sys.stderr)
            return 1
    mode = config_path.stat().st_mode & 0o777 if config_path.exists() else 0o644
    _atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode)
    print("\n==> 向导写入了：")
    for line in changed or ["没有改动"]:
        print(f"  - {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server-autoops setup", description="环境检查和安装向导")
    parser.add_argument("--config", default=os.environ.get("AUTOOPS_CONFIG", "/etc/server-autoops/config.json"),
                        help="要修改的 config.json（默认 /etc/server-autoops/config.json）")
    parser.add_argument("--answers", help="答案文件（KEY=VALUE，见 examples/install.env.example）；给了就不再提问")
    parser.add_argument("--non-interactive", action="store_true", help="不提问：只用 --answers 里写了的项，其余保持不变")
    parser.add_argument("--check-only", action="store_true", help="只检查环境，不改任何东西")
    parser.add_argument("--state", default="/var/lib/server-autoops", help="数据目录（只用来检查磁盘空间）")
    args = parser.parse_args(argv)
    if args.check_only:
        return print_environment(environment(args.state))
    answers = read_answers(args.answers) if args.answers else {}
    interactive = not args.non_interactive and not args.answers and sys.stdin.isatty()
    if not interactive and not answers:
        print("没有终端也没有答案文件：不提问，配置保持不变。用 --answers <文件> 自动填写（见 examples/install.env.example）。")
        return 0
    return wizard(Path(args.config), answers, interactive)
