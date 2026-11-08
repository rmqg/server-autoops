# 安全边界

这份文档回答五个问题：智能体能做什么、broker 以什么身份执行什么、白名单怎么决定一切、名单外的动作怎么办、什么时候停手。最后列出读写的文件、需要的权限和剩余风险。漏洞报告方式见仓库根目录的 [SECURITY.md](../SECURITY.md)。

## 1. 信任关系一览

| 组件 | 以谁的身份运行 | 信任 | 能接触到什么 |
|---|---|---|---|
| 看门狗、`server-autoops alert` | 你决定（通常 root 的 cron/服务） | 半信任：只能「报告」故障 | 只追加告警流 |
| scheduler（`server-autoops run`） | root | 可信 | 告警流、SQLite 状态库、启动 runner、发通知 |
| runner | root（scheduler 的子进程） | 可信 | 本次运行目录、broker、启动智能体容器 |
| **broker** | root（runner 进程里的线程） | 可信，**唯一能动宿主的部件** | policy 里写明的 Docker 守护进程、容器、配置文件、日志文件、探针 |
| 智能体（dsh 容器里的 LLM） | 容器内 uid 1000，无任何 capability | **不可信** | 只有 broker 的 Unix socket（带一次性 token） |
| 告警正文、日志、配置内容 | — | **不可信数据**（可能含提示注入） | 只作为证据交给智能体，先脱敏 |
| `policy.json`、`config.json` | root 所有，其他用户不可写 | 可信，**授权的唯一来源** | — |

原则：**权限只来自 root 管理的 policy，不来自模型，也不来自告警。** 模型被提示注入、告警被伪造，最坏结果都只是「在名单内的服务上做名单内的动作」，并且每一步都有探针和审计。

## 2. 智能体能做什么

智能体只有一个工具 `ops`，它的请求是一个 JSON 对象，经 Unix socket 发给 broker。可用的操作只有 10 种：

| op | 作用 | 是否改动系统 |
|---|---|---|
| `inventory` | 列出**本次告警**授权的目标、探针类型、可改配置键和范围 | 否 |
| `inspect` | 容器状态（只返回 Status/Running/ExitCode/StartedAt/Health 等少数字段）+ 一次探针 | 否 |
| `logs` | 容器最后 200 行日志 + policy 列出的日志文件尾部，合计 32 KiB，**先脱敏** | 否 |
| `read_config` | 读 policy 列出的配置文件，返回 sha256 和脱敏后的内容 | 否 |
| `verify` | 跑一次探针 | 否 |
| `start` | 容器没在跑时启动它 | 是 |
| `restart` | 重启容器 | 是 |
| `recreate` | 用 policy 固定的 Compose 文件重建**单个**服务（`--no-deps --no-build --pull never`） | 是 |
| `patch_config` | 改配置里白名单内的标量键（见第 4 节的事务） | 是 |
| `finish` | 结束，提交诊断摘要；**结论由 broker 的探针决定** | 否 |

智能体**不能**：执行 shell 或任意命令、读写任意文件、指定任意路径/容器/守护进程、联网调用工具、启动子智能体、修改告警流、自己发通知。具体做法：

- **dsh 只保留一个工具**：runner 每次为运行新建一个 headless profile，禁用 bash、pwsh、fs、fs-search、web、skill、subagent、workflow、plugin-manager 等全部插件（列表见 `server_autoops/runner.py` 的 `DISABLED_DSH_PLUGINS`）；`ops` 插件里还有 `tools.guard` 拒绝任何非 `ops` 调用，每次调用前核对工具清单必须恰好是 `["ops"]`，否则拒绝运维。
- **容器隔离**：`--read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --memory 512m --cpus 0.75 --pids-limit 96`，`/tmp` 是 96 MiB 的 tmpfs；默认 `bridge` 网络（只为访问模型 API）。只挂载本次运行的空 home、空 workspace、只读的插件目录和只读的 broker socket 目录。**不挂载** Docker socket、宿主目录、SSH 钥匙、其他 dsh 的 home。
- **模型密钥**只通过环境变量传进容器，不写进命令行；runner 只读 `model.env` 里指定的那一个变量，不执行这个文件。密钥文件必须是 600。
- **每次运行都是全新的**：不加载记忆插件，不继承会话；运行目录在 `runs/<run_id>/` 留作审计。

## 3. broker 以什么身份执行什么

broker 以 root 身份运行在 runner 进程里（不在智能体容器里，所以容器被 OOM 杀掉也不会留下半改的配置）。它只做这些事，而且每个参数都来自 policy，不来自请求：

- **Docker 命令**：`<policy.docker.bin> -H <policy.docker.daemons[服务.daemon]> <inspect|logs|start|restart> <服务.container>`，以及 `compose -f <固定文件> --project-directory <固定目录> up -d --no-deps --no-build --pull never --force-recreate <固定服务>`。全部用参数数组调用（`shell=False`），并清除 `DOCKER_HOST`、`DOCKER_CONTEXT` 等环境变量。守护进程只允许本地 `unix:///` socket，不允许 TCP。
- **探针**：`running`（容器在跑；有 Docker 健康检查时自动用它）、`health`（Docker 健康检查为 healthy）、`http`（GET 固定 URL，2xx，可选 JSON 字段等于期望值；URL 不允许带账号密码）、`exec`（在容器里执行 policy 固定的 argv）。
- **读写文件**：只读写 policy 列出的绝对路径；拒绝符号链接、硬链接（`st_nlink != 1`）、路径中含 `..`、父目录链中有符号链接；读之前和打开之后核对 inode 一致；单文件上限 2 MiB。
- **审计**：每个改动写入 `runs/<run_id>/actions.jsonl`（`O_APPEND`、`fsync`），配置备份写进 `runs/<run_id>/backups/`（`O_EXCL`，保持原属主和权限）。

请求本身的限制：每条消息最大 128 KiB，必须带本次运行随机生成的 64 位十六进制 token（常数时间比较），socket 只在本次运行期间存在。

## 4. policy 白名单怎么决定一切

完整写法见 [POLICY.md](POLICY.md)。和安全直接相关的规则：

1. **目标由告警决定**：告警 `<service>:<CODE>` 在 `faults` 里查到的 `targets` 就是本次运行全部能碰的服务。其他服务即使在 policy 里，本次也碰不到（`target is not authorized for this event`）。
2. **能不能改**：服务必须 `mutable: true`，且不是 `login_sensitive`，且名字/容器名/Compose 服务名不含 `protected_name_patterns` 里的任何片段（默认 `autoops`、`broker`、`listener`，防止它管理自己），且这次的故障没有标 `needs_scan` 或 `diagnose_only`。否则只能读和探针。
3. **配置怎么改**：只改 `editable_keys` 里列出的键；键必须已经存在、原值是标量（字符串、数字、布尔、null）、新值类型必须和原值一致、数字必须在 `bounds` 范围内；必须带上 `read_config` 拿到的 sha256（CAS），文件被别人改过就拒绝。TOML 只支持顶层键。
4. **事务**：备份（保持属主和权限）→ 原子替换并 fsync 目录 → 重新解析 → 重启或单服务重建 → 等探针通过（`probe_timeout_seconds`，默认 45 秒）。任何一步失败：恢复原文件、再重启、再探针；回滚后仍不健康会明确报告「回滚验证失败」。
5. **policy 本身要过校验**：未知字段、未定义的目标、TCP 守护进程、相对路径、带账号的 URL、`bounds` 与 `editable_keys` 不一致等都是错误。policy 有错误时 **scheduler 不启动任何智能体，告警全部保留排队**，并发一条（每天最多一条）通知；broker 也拒绝加载有错误的 policy。

## 5. 名单外的动作：必须先问人

server-autoops 自己从不「先做了再说」：

- 告警在 policy 里没有映射：认领前直接记为 `blocked`（`no policy mapping; agent not started`），**不启动智能体、不占预算**，写进日报。
- 映射的故障标了 `needs_scan`（例如需要扫码重新登录）：智能体只能诊断；结果是 `needs_scan`，生成一条「需要人工检查」通知，告警停在 `needs_scan`，不会自动重试。
- 标了 `diagnose_only`：只诊断，结果 `blocked`。
- 智能体请求了名单外的东西：broker 拒绝这一条请求（运行目录里有记录），智能体只能换别的名单内动作或 `finish`。
- 运行被打断，而审计里已经有改动记录、或结果文件不完整：一律转为 `needs_scan`，**不盲目重跑**。

要扩大权限只有一条路：管理员修改 `policy.json`，跑 `server-autoops policy check`。告警正文、日志、模型输出都无权临时扩权。如果你还有交互式的 AI 助手能 root 登录这台机器，建议在它的说明里写明：后台 policy 的授权只属于后台智能体，交互会话重启服务、改配置仍要先问人。

## 6. 什么时候停手：预算、熔断、资源闸门

| 规则 | 默认 | 配置项 |
|---|---|---|
| 同一 `service:code` 两次运行之间的冷却 | 300 秒 | `cooldown_seconds` |
| 每个告警来源（`service`）每小时最多运行 | 3 次 | `service_hourly_limit` |
| 全局每天（按 `timezone`）最多运行 | 20 次 | `daily_limit` |
| 连续失败多少次熔断 | 3 次 | `breaker_failures` |
| 熔断时长 | 第一次 1 小时，之后每次翻倍，封顶 24 小时；**永不放弃**，成功一次计数清零 | `breaker_base_seconds`、`breaker_max_seconds` |
| 熔断通知 | 每个服务每天最多一条 | — |
| 可用内存低于多少不启动智能体（只延后，不丢告警） | 1024 MiB | `min_available_mb` |
| 同一时间运行的智能体 | 1 个（文件锁） | — |
| 维护模式 | 关 | `server-autoops maintenance on/off`：开着时只记录告警，不启动智能体 |
| 单次运行总时限 / 其中留给 broker 收尾的时间 | 600 秒 / 240 秒 | `agent_timeout`、`agent_grace_seconds` |

超时或服务停止时，runner 先删除智能体容器、等 broker 正在进行的配置事务完成或回滚（最多 `agent_grace_seconds`），再结束进程组。systemd unit 用 `KillMode=mixed` 让 SIGTERM 只发给 scheduler，避免同时打断 broker 正在执行的 `docker compose`。

## 7. 读写清单和所需权限

| 路径（默认） | 读/写 | 谁 | 说明 |
|---|---|---|---|
| `/etc/server-autoops/config.json`、`policy.json` | 读 | scheduler、runner | root 所有，`chmod go-w`；doctor 会检查 |
| `/etc/server-autoops/model.env` | 读 | runner | 600；只取一个变量 |
| `/etc/server-autoops/bark.url` | 读 | scheduler、alert | 600；Bark 地址等同于推送权限，不放进 config.json |
| `/var/lib/server-autoops/alert-stream.jsonl` | 追加 / 读 | 看门狗 / scheduler | scheduler 不改写、不截断它 |
| `/var/lib/server-autoops/state/queue.sqlite3` | 读写 | scheduler | 700 目录、600 文件 |
| `/var/lib/server-autoops/runs/<id>/` | 写 | runner、broker | 事件、智能体输出、审计、备份、结果；默认保留 90 天 |
| policy 列出的配置文件 | 读、按白名单写 | broker | 保持原属主和权限 |
| policy 列出的日志文件 | 读尾部 | broker | 脱敏后交给智能体 |
| Docker socket（policy 列出的） | 访问 | broker、runner | 需要 root（或 docker 组，不推荐） |

需要 root 的原因：访问 Docker socket、按原属主写回配置。智能体本身从不以 root 运行。

## 8. 剩余风险（如实说明）

- **名单内的动作仍可能有害**：例如把一个本来就在恢复中的服务再重启一次。所以只把「重启/改这几个数也无妨」的服务标成 `mutable`，数据库等关键服务建议 `diagnose_only`。
- **弱探针会让人误以为修好了**：只有 `running` 探针的目标即使通过也只记「容器在跑、业务未验证」，日报单列；请尽量给每个服务配 `http`、`exec` 或 Docker 健康检查。
- **模型服务不可用会被算作失败**：探针没过就记失败并计入熔断（之后指数退避重试）。
- **`--network bridge` 仍能访问外网**：智能体没有能发网络请求的工具，但如果你担心，可以给它一个只能访问模型 API 的自定义 Docker 网络（`agent.network`）。不要用 `host`，那样它和宿主共享回环地址。
- **脱敏是尽力而为**：按常见键名（password、token、secret、api_key、cookie、authorization、bark_url 等）和常见格式（Bearer、私钥块、URL 里的账号、`sk-`、`ghp_` 等）替换。非常规格式的秘密可能漏掉；不要把秘密放进会交给智能体读的日志和配置里。
- **模拟驱动**（`driver: simulated`）只用于演示和测试，policy 必须显式 `allow_simulated: true` 才能用，doctor 会提醒。
- **智能体镜像来自 npm**：`agent-image/Dockerfile` 安装固定版本的 `@deepseek-ai/dsh`，它的依赖来自 npm。构建时请在可信网络上进行，之后用固定标签运行。
- **看门狗可以伪造告警**：能写告警流的人可以让智能体对名单内服务做名单内动作（受预算限制）。告警流默认 600，只给 root 写。
