# 配置参考：config.json

默认位置 `/etc/server-autoops/config.json`；所有命令都可以用 `--config <路径>` 或环境变量 `AUTOOPS_CONFIG` 指定别的文件。改完运行 `server-autoops doctor` 检查；常驻服务需要重启才读新配置（`systemctl restart server-autoops` 或 `/etc/init.d/server-autoops restart`）。`policy.json` 不需要重启，scheduler 每次认领前按文件变化重新读取。

完整示例：[`examples/config.example.json`](../examples/config.example.json)。`./install.sh` 生成的配置里，通知是关闭的，模型地址是占位符。

> config.json 里不放任何秘密：模型密钥在 `model.env`，Bark 地址在 `bark.url`，两个文件都必须是 600。

## 路径

| 参数 | 类型 | 默认 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `root` | 绝对路径，**必填** | — | 数据根目录；其他路径的默认值都在它下面 | 想把数据放到别的盘 |
| `stream` | 绝对路径 | `<root>/alert-stream.jsonl` | 告警流：看门狗追加、scheduler 读取。scheduler 不改写、不截断它；轮转交给 logrotate 之类（按 inode 跟踪，轮转安全） | 已有看门狗写到别的位置 |
| `state_db` | 绝对路径 | `<root>/state/queue.sqlite3` | SQLite 状态库：游标、队列、运行记录、冷却、熔断、通知出站箱、日报 | 一般不改 |
| `runs_dir` | 绝对路径 | `<root>/runs` | 每次运行一个目录：event.json、agent.jsonl、actions.jsonl、backups/、result.json | 一般不改 |
| `policy` | 绝对路径，**必填** | — | 白名单文件，见 [POLICY.md](POLICY.md) | — |

示例：`"root": "/var/lib/server-autoops", "policy": "/etc/server-autoops/policy.json"`

## 时间

| 参数 | 类型 | 默认 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `timezone` | 字符串 | `UTC` | 「每天」的边界（每日预算、熔断通知去重、日报）和通知里显示的时间。可以写 IANA 名（`Asia/Shanghai`）或固定偏移（`UTC+08:00`） | 精简系统（OpenWrt）的 Python 没有 tzdata 时，IANA 名会报错，改用 `UTC+08:00` |
| `daily_hour` | 整数 0–23 | `8` | 每天几点（按 `timezone`）生成并发送前一天的日报 | 想换个时间收日报 |

## 调度、预算和熔断

| 参数 | 类型 | 默认 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `poll_seconds` | 数字 > 0 | `0.5` | 读告警流、处理队列的轮询间隔。这不是故障发现速度，那取决于看门狗 | 极省电的设备可以调到 2–5 |
| `cooldown_seconds` | 整数 ≥ 0 | `300` | 同一 `service:code` 两次运行之间至少间隔多久；失败后也按它延后重试 | 服务重启很慢时调大 |
| `service_hourly_limit` | 整数 ≥ 0 | `3` | 同一告警 service 每小时最多启动几次智能体；0 表示不限 | 很少改 |
| `daily_limit` | 整数 ≥ 0 | `20` | 全局每天最多启动几次智能体；0 表示不限 | 按模型费用调整 |
| `breaker_failures` | 整数 > 0 | `3` | 同一告警 service 连续失败多少次熔断 | — |
| `breaker_base_seconds` | 整数 > 0 | `3600` | 第一次熔断的时长；之后每多失败一次翻倍 | — |
| `breaker_max_seconds` | 整数 > 0 | `86400` | 熔断时长上限。永不放弃：到期后自动再试，成功一次清零 | — |
| `min_available_mb` | 整数 ≥ 0 | `1024` | `MemAvailable` 低于它时不启动智能体（告警保留，稍后再试） | 小内存机器按实际调整；智能体容器上限是 `agent.memory` |
| `agent_timeout` | 整数 > 0 | `600` | 单次运行总时限（秒） | 模型很慢时调大 |
| `agent_grace_seconds` | 整数 > 0 | `240` | 总时限里留给 broker 收尾（完成或回滚正在进行的配置事务）的时间；智能体自己最多能用 `agent_timeout - agent_grace_seconds`（至少 10 秒） | 服务重启特别慢时调大 |
| `retention_days` | 整数 ≥ 1 | `90` | 运行记录、已结束的队列项、已发出的通知、日报、隔离日志保留多久。未结束的、仍在熔断的、没发出的通知永不清理 | 磁盘紧张时调小 |
| `notifications_enabled` | 布尔 | `notify.kind != "none"` | 总开关。为 `false` 时通知只记录为 suppressed | 演练时临时关闭 |

## notify：通知

| 参数 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `notify.kind` | `none` / `bark` / `command` / `file` | `none` | 通知后端 |
| `notify.default_level` | `passive` / `active` / `timeSensitive` | `passive` | Bark 通知级别。`passive` 不响铃不亮屏，只进通知中心 |
| `notify.levels` | 对象 | `{}` | 按通知种类单独设级别，键见下表 |
| `notify.group` | 字符串 | `server-autoops` | Bark 分组 / 传给命令的 group 字段 |
| `notify.timeout_seconds` | 1–120 | `12` | 单次发送超时 |
| `notify.url_file` | 绝对路径 | — | `bark`：存 Bark 地址的文件（一行，例如 `https://api.day.app/<设备 key>`），**必须 600** |
| `notify.url_env` | 变量名 | — | `bark`：或者从这个环境变量读地址（和 `url_file` 二选一） |
| `notify.argv` | 字符串数组 | — | `command`：执行的命令（第一个元素必须是绝对路径），通知以 JSON `{"title","body","kind","level","group"}` 从标准输入传入，退出码 0 算成功。可以接 ntfy、Telegram、企业 IM 等 |
| `notify.path` | 绝对路径 | — | `file`：追加写入 JSONL（测试用） |

通知种类（`notify.levels` 的键）：`daily` 日报、`needs_scan` 需要人工、`breaker` 熔断、`rate_limit` 触发限流、`policy` policy 读不出、`stream_loss` 告警流轮转丢了未读内容、`alert_failure` / `alert_recovery` / `alert_warning`（`server-autoops alert` 推送的看门狗告警）、`memory`、`test`。

默认全部 `passive`。想让「需要人工」响铃：`"levels": {"needs_scan": "active"}`。`timeSensitive` 会穿透专注模式，doctor 会提醒你确认。

发送失败会按 30 秒、1 分钟、2 分钟……最多 6 小时退避重试，失败不会产生新的故障事件。配好后：`server-autoops notify-test`。

配置 Bark 的步骤：

```sh
printf '%s\n' 'https://api.day.app/你的设备key' | sudo tee /etc/server-autoops/bark.url >/dev/null
sudo chmod 600 /etc/server-autoops/bark.url
# config.json: "notify": {"kind": "bark", "url_file": "/etc/server-autoops/bark.url"}
sudo systemctl restart server-autoops && sudo server-autoops notify-test
```

## agent：智能体

| 参数 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `agent.kind` | `dsh` / `mock` / `disabled` | `dsh` | `dsh`：真实 LLM 智能体容器；`mock`：按 `plans` 脚本行动（演示、CI、上线前演练）；`disabled`：不启动智能体，只跑一次探针给出结论 |

### kind = dsh

| 参数 | 类型 | 默认 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `agent.image` | 字符串，**必填** | — | 智能体镜像标签，由 `agent-image/Dockerfile` 构建 | 升级 dsh 时换新标签 |
| `agent.docker_bin` | 绝对路径 | `/usr/bin/docker` | 启动智能体容器用的 docker | — |
| `agent.docker_host` | `unix:///...` | `unix:///var/run/docker.sock` | 在哪个 dockerd 上跑智能体。可以和业务用不同的守护进程 | 有专门的隔离 dockerd 时 |
| `agent.network` | `bridge` / `none` / `host` / 网络名 | `bridge` | 智能体容器的网络，只用于访问模型 API。**不要用 `host`** | 想限制出站时建一个自定义网络 |
| `agent.memory` | 如 `512m`、`1g` | `512m` | 容器内存上限 | — |
| `agent.cpus` | 0.1–64 | `0.75` | CPU 上限 | — |
| `agent.pids_limit` | 16–4096 | `96` | 进程数上限 | — |
| `agent.tmpfs_mb` | 16–4096 | `96` | `/tmp` tmpfs 大小（dsh 需要可写、可执行的 /tmp 加载原生模块） | — |
| `agent.user` | `uid:gid`，非 0 | `1000:1000` | 容器内用户；运行目录会 chown 给它 | 镜像里的用户不是 1000 时 |
| `agent.plugin_dir` | 绝对路径 | `<安装目录>/plugin` | `ops` 插件目录，只读挂载进容器 | 一般不改 |
| `agent.prompt_file` | 绝对路径 | 内置中文提示 | 自定义任务提示；告警 JSON 会接在它后面 | 想改语言或补充本机常识 |
| `agent.model.base_url` | URL，**必填** | — | OpenAI 兼容接口地址，如 `https://api.deepseek.com/v1` | — |
| `agent.model.model` | 字符串，**必填** | — | 模型 ID | — |
| `agent.model.env_file` | 绝对路径，**必填** | — | 存密钥的文件（600）；只读取 `api_key_env` 那一个变量，文件不会被执行 | — |
| `agent.model.api_key_env` | 大写变量名 | `AUTOOPS_MODEL_API_KEY` | 密钥变量名；同时也是传进容器的环境变量名 | — |
| `agent.model.api` | 字符串 | `openai-completions` | dsh（pi-ai）的接口类型 | 接 Anthropic/Gemini 原生接口时按 dsh 文档改 |
| `agent.model.provider_id` | 字符串 | `autoops-model` | dsh 里的 provider 名 | 一般不改 |
| `agent.model.display_name` | 字符串 | `AutoOps model` | 显示名 | — |
| `agent.model.context_window` | 整数 | `131072` | 上下文长度 | 按模型实际值 |
| `agent.model.max_tokens` | 整数 | `8192` | 单次输出上限 | — |

### kind = mock

| 参数 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `agent.plans` | 对象 | `{}` | `"<service>:<CODE>": [ops 请求, ...]`。模拟智能体先 `inventory`，再依次发送这些请求，最后 `finish`。没有对应计划时只 `inspect` 每个目标。`{"op":"summary","text":"..."}` 设置诊断摘要；`"$sha256:<服务>/<配置别名>"` 换成最近一次 `read_config` 的哈希 |

## 示例：最常见的修改

```jsonc
{
  "timezone": "Asia/Shanghai",            // 没有 tzdata 的系统写 "UTC+08:00"
  "daily_limit": 10,                       // 控制模型费用
  "min_available_mb": 768,                 // 4 GB 小机器
  "notify": {"kind": "bark", "url_file": "/etc/server-autoops/bark.url",
             "levels": {"needs_scan": "active"}},
  "agent": {"kind": "dsh", "image": "local/server-autoops-agent:0.1.0",
            "model": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                      "env_file": "/etc/server-autoops/model.env"}}
}
```

（真实文件是 JSON，不能写注释。）
