# 兼容性

「实测通过」只写本版本（0.1.0）真正测过的组合，测法见 [VALIDATION.md](VALIDATION.md)。其他一律放在「未测试 / 不保证」。

## 运行环境

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| Python | 3.10.12（Ubuntu 22.04 主机）；3.11.2（Debian 12 容器）；3.12.3（Ubuntu 24.04 容器）；3.14.10（Alpine `docker:cli` 容器，单元测试 + demo + 真实 Docker 冒烟） | 3.13（CI 矩阵里有，本地未测）；**3.9 及以下不支持**（安装脚本会拒绝） |
| TOML 配置 | Python 3.11+ 自带 `tomllib` 的路径由单元测试覆盖 | Python 3.10 + `tomli`：代码支持，未实测 |
| 发行版 | Debian 12、Ubuntu 24.04（全新容器：校验 → demo → 安装 → doctor → policy 校验 → alert/status → 单元测试 → 卸载） | 其他发行版；Ubuntu 22.04 只跑过单元测试和 demo（没跑安装脚本） |
| OpenWrt / iStoreOS | — | 本版本未实测。前身（作者的内部版本）在 iStoreOS 24.10.8（内核 6.6，x86_64，Python 3.11.14）上以 procd 服务运行。精简系统需要 `python3`、`python3-sqlite3`；没有 tzdata 时 `timezone` 写 `UTC+08:00` |
| CPU 架构 | arm64（Apple Silicon 上的 OrbStack 原生）；amd64（同一台机器上经 OrbStack 的 x86_64 仿真） | 真实 x86_64 硬件上的本版本；其他架构 |
| 内核 | 容器实测时为 OrbStack 内核 7.0.14；主机 6.8.0（Ubuntu 22.04） | 其他内核。内存闸门读 `/proc/meminfo` 的 `MemAvailable`（3.14+ 内核都有）；mem-guard 的压力指标需要 PSI（4.20+，没有时按 0 处理） |

## Docker

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| broker 操作真实容器（inspect、logs、start、restart、HTTP 探针、改配置 + 重启 + 回滚） | Docker Engine 29.4.0（OrbStack，arm64），`scripts/docker_smoke.py` 9/9 | 其他 Docker 版本；Podman 的 docker 兼容接口 |
| `recreate`（单服务 Compose 重建）、`exec` 探针、Docker 健康检查探针 | 单元测试（模拟 docker 命令） | 本版本未在真实 Docker 上跑过。前身在 Docker 27.3.1 / Compose v2.39.1 上跑过 recreate |
| 多个 Docker 守护进程（`docker.daemons`） | 单元测试 | 真实环境未测 |
| rootless Docker | — | 未测 |

## 智能体

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| `mock` 智能体（脚本） | 全部容器实测、真实 Docker 冒烟 | — |
| `disabled` | 单元测试（CLI 端到端） | — |
| `dsh` 智能体镜像构建（`agent-image/Dockerfile`，`@deepseek-ai/dsh` 0.2.1-rc.5） | arm64 构建成功，`dsh --version` 为 0.2.1-rc.5 | amd64 构建未测 |
| `dsh` 运行时接线（生成的 headless profile、只剩 `ops` 工具、插件 → broker socket、容器清理） | 用本地假 OpenAI 兼容服务实测，6/6（VALIDATION.md 第 4 节） | — |
| `dsh` + 真实模型的修复运行 | — | **本版本未实测**（这一轮没有调用任何真实模型）。前身用 dsh 0.2.1-rc.5 + OpenAI 兼容接口实测过「改配置修复 503→200」和「三次失败熔断」演练 |

## 服务管理

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| `./install.sh --service none`（容器里自动识别） | Debian 12、Ubuntu 24.04 | — |
| systemd unit | — | 写好了，本版本未在真实 systemd 上启动过 |
| OpenWrt procd init | — | 写好了，本版本未实测；前身用同样结构的 procd 脚本运行 |
| mem-guard | 在 Ubuntu 22.04 主机上 `DRY_RUN` 演习 | systemd/procd 服务形式未测 |
| `contrib/openwrt/hw-watchdog.init`、`late-logfile.init` | — | 来自作者在 iStoreOS 上的实际使用，已改成通用参数，本版本未重新测试 |

## 通知

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| Bark 协议（POST JSON、级别字段、`code: 200` 判定） | 单元测试里的本地 HTTP 假服务器 | 真实 Bark 服务器（这一轮没有往任何设备推送） |
| `command`、`file` 后端 | 单元测试、demo | — |
