"""Single command line entry point: ``server-autoops <command> ...``."""
from __future__ import annotations

import sys

from . import __version__

SCHEDULER_COMMANDS = {"run", "status", "enqueue", "daily", "drain", "replay", "maintenance"}
HELP = f"""server-autoops {__version__}

用法: server-autoops <命令> [选项]      （每个命令都支持 -h）

常驻与队列:
  run          常驻：监听告警流、调度智能体、发通知和日报（由 systemd/procd 启动）
  status       队列、熔断、通知出站箱的计数（不输出任何告警内容）
  enqueue      手动把一条告警 JSON 写进队列
  drain        调试：处理当前所有可处理的告警后退出（需先停常驻服务）
  daily        生成并发送某一天的日报
  replay       调试：从头重放当前告警流（生产不要用）
  maintenance on|off|status   维护模式：照常记录告警，但暂停自动修复（默认 6 小时后自动结束）
告警与通知:
  alert        给 watchdog 用：往告警流写一条告警，并按配置推送
  notify-test  发一条测试通知，检查 Bark/命令通知是否配好
检查与演示:
  doctor       自检：依赖、权限、配置、policy、Docker、镜像、模型密钥、通知、服务状态
  policy check <policy.json>   校验 policy 白名单
  demo         离线演示：合成环境里模拟告警，用模拟智能体走完整个流程（不需要 Docker 和模型）
内部:
  runner       由 run 调用，执行一次智能体运行
  mock-agent   由 runner 调用的模拟智能体
  version      打印版本

全局: --config <路径>，默认 $AUTOOPS_CONFIG 或 /etc/server-autoops/config.json
"""


def _hoist_config(argv: list[str]) -> list[str]:
    """Allow ``status --config X`` as well as ``--config X status``."""
    out, config = [], []
    i = 0
    while i < len(argv):
        if argv[i] == "--config" and i + 1 < len(argv):
            config = ["--config", argv[i + 1]]
            i += 2
            continue
        if argv[i].startswith("--config="):
            config = ["--config", argv[i].split("=", 1)[1]]
            i += 1
            continue
        out.append(argv[i])
        i += 1
    return config + out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    command, rest = argv[0], argv[1:]
    if command in ("version", "--version", "-V"):
        print(__version__)
        return 0
    if command in SCHEDULER_COMMANDS:
        from .scheduler import main as scheduler_main
        return scheduler_main(_hoist_config([command] + rest))
    if command == "alert":
        from .relay import main as relay_main
        return relay_main(rest)
    if command == "policy":
        from .policy import main as policy_main
        return policy_main(rest)
    if command == "doctor":
        from .doctor import main as doctor_main
        return doctor_main(rest)
    if command == "demo":
        from .demo import main as demo_main
        return demo_main(rest)
    if command == "notify-test":
        from .doctor import notify_test
        return notify_test(rest)
    if command == "runner":
        from .runner import main as runner_main
        return runner_main(rest)
    if command == "mock-agent":
        from .mock_agent import main as mock_main
        return mock_main(rest)
    print(f"未知命令 {command!r}；运行 server-autoops --help 查看用法", file=sys.stderr)
    return 2
