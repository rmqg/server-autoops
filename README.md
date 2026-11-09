# server-autoops

> **难度和用时**
> - **离线演示**：约 5 分钟，不要账号、不要 root、不要 Docker、不联网（下面「快速开始」第 1 步）。
> - **第一次自动修复（入门演练）**：约 15 分钟，需要会用 SSH 登录服务器、复制粘贴命令；不需要模型账号。跟着 [docs/QUICKSTART.md](docs/QUICKSTART.md) 一步一步做。
> - **真实部署**：需要会用 Linux 命令行和 Docker，并且有一个 OpenAI 兼容的模型 API key；白名单要你自己按 [docs/POLICY.md](docs/POLICY.md) 写。

**Linux 服务器的「有白名单的」自动运维。** 看门狗发现故障后写一条告警；server-autoops 把它放进持久队列，按 `policy.json` 白名单查出这个故障允许碰哪些服务，然后启动一个**被关在受限容器里的 LLM 智能体**排查和修复。智能体手里只有一个结构化工具 `ops`，它的每个请求都由宿主上的 root broker 按白名单逐条核对后才执行；修没修好由 broker 自己的探针判断，不听模型的口头汇报。

```
watchdog ──alert──▶ 告警流(jsonl) ──▶ scheduler(SQLite 队列·预算·熔断·通知·日报)
                                          │ 一次只跑一个
                                          ▼
                     runner ──▶ 智能体容器(dsh，只读根、无 shell、只有 ops 工具)
                        │                    │ Unix socket（一次性 token）
                        ▼                    ▼
                     root broker：只执行 policy 白名单里的动作 → 备份 → 应用 → 业务探针 → 失败回滚 → 审计
```

> 本项目从作者自用的 NAS 运维脚本整理而来，为开源做了通用化：路径、Docker 守护进程、通知、时区、模型都改成配置；去掉了所有与具体业务绑定的检查。

## 它是什么

- **告警驱动**：只处理看门狗报上来的故障（`<service>:<FAULT_CODE>`），不做全盘巡检。自带一个通用看门狗示例和告警命令 `server-autoops alert`。
- **白名单授权**：`policy.json` 写明每个故障码能碰哪些服务，每个服务用哪个 Docker 守护进程、哪个容器、用什么探针判断健康、配置文件里**哪几个标量键**能改、取值范围多少。名单外的一律拒绝，有 [JSON Schema](schema/policy.schema.json) 和 `server-autoops policy check` 校验。
- **结果可信**：成功只认 broker 的探针；只有「容器在跑」这种弱探针的，结论降级为「容器在跑、业务未验证」，不算修好。
- **改配置是事务**：CAS（sha256）防并发改动 → 带属主和权限的备份 → 原子替换 → 重启/重建 → 探针 → 失败自动恢复原文件并再验证。
- **不失控**：持久队列（重启不丢告警）、同类冷却、每服务每小时/全局每天的次数上限、连续失败熔断后指数退避（1h→2h→…封顶 24h，永不放弃，修好清零）、内存不足只延后不启动、全局同时只跑一个智能体。
- **维护模式**：升级、切换、手动重启服务之前 `server-autoops maintenance on`，告警照常记录、恢复事件照常取消排队，但不启动智能体，避免把你正在停的服务「修」回去；默认 6 小时后自动结束。
- **通知克制**：Bark（默认全部 `passive`，不响铃不亮屏）、自定义命令、或写文件；日报每天一条，熔断每服务每天最多一条。
- **可选组件**：observe-only 内存观察 `mem-guard`、OpenWrt 硬件看门狗/延迟挂载日志补救、小内存 zram 的 sysctl 建议。

## 它不是什么

- **不是通用的「让 AI 拿 root 随便修」**。智能体没有 shell、SSH、Docker socket、宿主文件系统，也不能联网调用别的工具；它只能提出 `ops` 请求，名单外的动作直接被拒绝，并记为「受阻」交给人。
- **不是监控系统**。它不采集指标、不画图；故障发现靠你已有的看门狗（示例见 `examples/`）。
- **不会做这些事**（结构上就做不到，policy 里也没有对应的动作）：重启宿主、改网络/防火墙、动虚拟机或 cgroup、删除数据、任意执行命令、发聊天消息、自动重新登录需要扫码的账号。
- **不包含模型**。智能体运行时是外部依赖 [DeepSeek Harness（dsh）](https://github.com/deepseek-ai/deepseek-harness)（MIT），模型由你提供任意 OpenAI 兼容接口。没有模型时可以用 `mock`（按脚本）或 `disabled`（只探针）模式。

## 支持情况摘要

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| Python | 3.10.12（Ubuntu 22.04）、3.11.2（Debian 12）、3.12.3（Ubuntu 24.04）、3.14.10（Alpine） | 3.13（只在 CI 矩阵里）；3.9 及以下不支持 |
| 发行版（照文档安装 + demo + doctor + 卸载） | Debian 12、Ubuntu 24.04 全新容器，arm64 和 amd64 各一遍；iStoreOS 24.10.8（OpenWrt，x86_64）上替换了生产服务 | 其他发行版 |
| CPU 架构 | arm64；amd64（OrbStack 仿真 + 真实 x86_64 主机） | 其他架构 |
| Docker（broker 操作真实容器：重启、启动、日志、HTTP 探针、改配置与回滚） | Docker Engine 29.4.0（arm64）9/9；Docker 27.3.1（amd64）11/11 | 其他版本；`recreate` 和 `exec` 探针只有单元测试 |
| 智能体 dsh | `@deepseek-ai/dsh` 0.2.1-rc.5 镜像构建（arm64），接本地假模型跑通；接真实 OpenAI 兼容模型在 iStoreOS 上修复 canary 9/9 | 其他模型；bridge 网络下的真实模型运行 |
| 服务管理 | `--service none`；OpenWrt procd（生产在用） | systemd unit（写好了，未实测） |

详细矩阵和每一项怎么测的见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) 和 [docs/VALIDATION.md](docs/VALIDATION.md)。

## 快速开始

### 1. 先看离线演示（不用 root、不用 Docker、不用模型、不联网）

```sh
tar xzf server-autoops-0.1.0.tar.gz && cd server-autoops-0.1.0
./bin/server-autoops demo
```

演示在临时目录里搭一个合成环境：一个本地 HTTP「网站」、几个模拟容器、一份 policy 和一个按脚本行动的模拟智能体，然后把真实的告警写进告警流，走完 scheduler → runner → broker → 探针的全流程，检查 21 项：配置错误修复、进程退出修复、6 种越权请求被拒、改坏配置自动回滚、无映射告警不启动智能体、需扫码的只诊断、连续失败熔断、低内存延后、告警流半行和轮转、日报和通知级别。加 `--keep` 保留现场。

### 2. 安装（root）

```sh
sudo ./install.sh            # 先检查环境，再安装，然后问你三个问题（智能体、Bark 通知、时区）
sudo server-autoops doctor   # 自检，每个问题都给修复命令
```

- 向导第 1 个问题选「入门演练」，不需要模型就能看到第一次自动修复，完整步骤见 [docs/QUICKSTART.md](docs/QUICKSTART.md)。
- 没有终端（脚本、自动化工具）时不提问，用答案文件：`sudo ./install.sh --config install.env`，格式见 [examples/install.env.example](examples/install.env.example)。
- 以后想换模型、改 Bark 地址：`sudo server-autoops setup`，可以反复运行。

装到 `/opt/server-autoops`，配置在 `/etc/server-autoops`，数据在 `/var/lib/server-autoops`。没选入门演练时白名单是空的，什么都不会被自动处理。按 [docs/INSTALL.md](docs/INSTALL.md) 继续：写 policy、构建智能体镜像、填模型密钥、配通知、接看门狗。

### 3. 接一个看门狗

```sh
# 每分钟检查一次 app-web 容器和它的健康接口；连续 2 次失败写一条 app:WEB_DOWN 告警
* * * * * /opt/server-autoops/examples/watchdogs/container_http_watchdog.py \
    --service app --container app-web --health-url http://127.0.0.1:8080/healthz --json-key ok \
    --down-code WEB_DOWN --unhealthy-code WEB_DOWN --state-file /var/lib/server-autoops/wd-web.json
```

或者在你自己的脚本里调用：

```sh
server-autoops alert --service app --fault-code WEB_DOWN --level failure --title "web 异常" --summary "HTTP 503"
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | 新手从零开始：全新服务器 → 第一次自动修复，每一步写了应该看到什么 |
| [docs/INSTALL.md](docs/INSTALL.md) | 一步一步安装、升级、卸载 |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `config.json` 每个参数：含义、类型、默认值、示例、什么时候改 |
| [docs/POLICY.md](docs/POLICY.md) | 怎么写白名单、每个字段、常见写法、校验 |
| [docs/SECURITY.md](docs/SECURITY.md) | 安全边界：智能体能做什么、broker 以什么身份做什么、名单外怎么办、熔断规则、风险 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 组件、数据流、状态机、文件布局 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 按症状排查，附命令 |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | 支持矩阵 |
| [docs/VALIDATION.md](docs/VALIDATION.md) | 本版本在干净容器里的实测记录 |

## 验证下载的 release

```sh
sha256sum -c SHA256SUMS
gh attestation verify server-autoops-0.1.0.tar.gz --repo rmqg/server-autoops
```

第二条验证这个文件确实由本仓库的 GitHub Actions 从对应 tag 构建（构建来源证明）。本地也能复现：`scripts/build-release.sh` 在同一个 commit 上得到同样的字节。

## 许可证

MIT，见 [LICENSE](LICENSE)。第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。漏洞报告方式见 [SECURITY.md](SECURITY.md)。
