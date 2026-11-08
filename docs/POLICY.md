# 写 policy 白名单

`policy.json`（默认 `/etc/server-autoops/policy.json`）是智能体权限的**唯一来源**。没写的就是不允许。改完先校验：

```sh
server-autoops policy check /etc/server-autoops/policy.json
```

有「错误」时 scheduler 不会启动任何智能体（告警保留排队）；「提醒」不阻止运行，但通常说明某个服务实际上碰不到或探针太弱。编辑器可以用 [`schema/policy.schema.json`](../schema/policy.schema.json) 做补全和检查（在文件里写 `"$schema": "/opt/server-autoops/schema/policy.schema.json"`）；程序内的校验比 schema 更严格，以程序为准。

完整示例：[`examples/policy.example.json`](../examples/policy.example.json)；最小示例：[`examples/policy.minimal.json`](../examples/policy.minimal.json)。

## 结构

```json
{
  "version": 1,
  "docker": {"bin": "/usr/bin/docker", "daemons": {"main": "unix:///var/run/docker.sock"}},
  "protected_name_patterns": ["autoops", "broker", "listener"],
  "code_aliases": {"app:ALL_DOWN": ["WEB_DOWN", "WORKER_DOWN"]},
  "services": { "<服务名>": { ... } },
  "faults":   { "<告警 service>:<故障码>": { "targets": ["<服务名>", ...] } }
}
```

注意两种「服务」：

- **告警的 `service`**：看门狗写告警时用的名字（例如 `app`），只是故障码的命名空间，用来组成 `faults` 的键 `app:WEB_DOWN`，也是限流按小时计数的单位。
- **`services` 里的服务**：broker 能操作的对象（一个容器）。`faults[...].targets` 把前者映射到后者。

## 顶层字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `version` | 整数 | `1` | 格式版本，目前只能是 1 |
| `description` | 字符串 | — | 备注 |
| `docker.bin` | 绝对路径 | `/usr/bin/docker` | broker 调用的 docker 程序，不走 PATH |
| `docker.daemons` | 对象 | `{"main": "unix:///var/run/docker.sock"}` | 守护进程别名 → 本地 socket。有第二套 dockerd（例如 rootless 或嵌套）时加一个别名。**只允许 `unix:///`** |
| `protected_name_patterns` | 小写字符串数组 | `["autoops","broker","listener"]` | 服务名、容器名、Compose 服务名包含其中任一片段的，永远不允许改动（只读）。用来防止它重启自己、你的反向代理、虚拟机等。设成 `[]` 表示不保护 |
| `allow_simulated` | 布尔 | `false` | 允许 `driver: simulated`（只用于演示/测试） |
| `code_aliases` | 对象 | `{}` | 一个故障码展开成多个，见下文 |
| `services` | 对象 | `{}` | 见下文 |
| `faults` | 对象 | `{}` | 见下文 |

## services.<名字>

名字只能用 `A-Z a-z 0-9 _ . -`，最长 128。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `description` | 字符串 | — | 备注 |
| `driver` | `docker` / `simulated` | `docker` | `simulated` 用一个 JSON 状态文件代替容器，只给 demo 和测试用 |
| `daemon` | 字符串 | `main` | `docker.daemons` 里的别名 |
| `container` | 字符串 | 必填（docker） | 容器名 |
| `mutable` | 布尔 | `false` | 是否允许 start/restart/recreate/patch_config。`false` 时只能看 |
| `login_sensitive` | 布尔 | `false` | 需要人工登录状态的服务（扫码、二次验证）。为 `true` 时**永远不改动**，避免重启把登录弄丢 |
| `probe` | 对象 | `{"kind":"running"}` | 判断健康的探针，见下表 |
| `probe_timeout_seconds` | 数字 1–600 | `45` | 改动后等探针通过的最长时间 |
| `log_files` | 绝对路径数组（≤32） | `[]` | `logs` 时额外读取这些文件的尾部（脱敏） |
| `configs` | 对象 | `{}` | 可读、可按白名单修改的配置文件，见下文 |
| `compose` | 对象 | — | `{"file","service","project_dir"}`，允许 `recreate` 和「改配置后单服务重建」。单文件 bind mount 的配置改完必须 recreate 才生效时要写它 |
| `state_file` | 绝对路径 | — | 只用于 `simulated` |

### 探针 `probe`

| kind | 其他字段 | 通过条件 | 结论 |
|---|---|---|---|
| `running` | — | 容器在运行；容器自带 Docker 健康检查时改用健康状态 | 没有健康检查时只算「容器在跑、业务未验证」 |
| `health` | — | 容器在运行且 Docker 健康状态为 `healthy` | 业务已验证 |
| `http` | `url`；可选 `json_key` + `expected` | 容器在运行、GET 返回 2xx；给了 `json_key`（点分路径）时还要求该字段等于 `expected` | 业务已验证 |
| `exec` | `argv`（字符串数组） | 容器在运行，且 `docker exec <容器> <argv...>` 退出码为 0 | 业务已验证 |

探针由 broker 在宿主上执行（`http` 从宿主访问 URL），智能体影响不了它。**尽量不要只用 `running`**：它证明不了业务恢复。

### 配置 `configs.<别名>`

| 字段 | 类型 | 说明 |
|---|---|---|
| `path` | 绝对路径 | 配置文件。必须是普通文件（不能是符号链接或硬链接），≤2 MiB |
| `format` | `json` / `toml` | TOML 需要 Python 3.11+（或安装 `tomli`），且只能改顶层键 |
| `editable_keys` | 数组 | 允许修改的键，JSON 可以用点分路径（`features.retry`）。为空时只能读 |
| `bounds` | 对象 | `{"键": [最小, 最大]}`，只能给 `editable_keys` 里的数值键 |
| `description` | 字符串 | 备注 |

智能体看到的配置是脱敏后的（键名像 password、token、secret、api_key、cookie、authorization 的值替换成 `[REDACTED]`）。只改原本存在的标量，类型必须一致；改完写回时 JSON 会重新排版（2 空格缩进），TOML 只替换那一行的值、保留注释。

## faults.<service>:<CODE>

键是「告警 service + 冒号 + 故障码」，都不能有空格。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `targets` | 服务名数组 | 必填 | 这个故障允许碰的服务 |
| `needs_scan` | 布尔 | `false` | 需要人工（扫码、登录）。只诊断，结果是 `needs_scan`，会通知人 |
| `diagnose_only` | 布尔 | `false` | 只诊断，结果是 `blocked` |
| `description` | 字符串 | — | 备注 |

没有出现在 `faults` 里的告警，一律在认领前记为 `blocked`，不启动智能体。

### 恢复事件

告警 `level: "recovery"`，或故障码以 `_RECOVERED` 结尾，会把同一告警 service 下还在排队的故障取消（`_RECOVERED` 后缀取消整个 service，其余只取消同一个码）。已经在跑的不会被打断。

### 组合码和别名

有的看门狗一次报多个码（`"WEB_DOWN WORKER_DOWN"`，空格或换行分隔）。scheduler 会拆成一码一条事件。`code_aliases` 可以把一个总码展开：

```json
"code_aliases": {"app:ALL_DOWN": ["WEB_DOWN", "WORKER_DOWN"]}
```

同一批里有 `needs_scan` 的码时，只排这一条，其他码作为症状记在它的事件里——登录掉了的时候，先别忙着重启其他组件。

## 常见写法

**只读诊断一个关键服务（数据库）**

```json
"db": {"container": "app-db", "mutable": false, "probe": {"kind": "health"}}
"app:DB_DOWN": {"targets": ["db"], "diagnose_only": true}
```

**允许重启，用业务接口判断**

```json
"web": {"container": "app-web", "mutable": true,
        "probe": {"kind": "http", "url": "http://127.0.0.1:8080/healthz", "json_key": "ok", "expected": true}}
```

**允许改几个数值，改完单服务重建**

```json
"worker": {"container": "app-worker", "mutable": true,
           "probe": {"kind": "exec", "argv": ["/usr/local/bin/worker", "status", "--check"]},
           "configs": {"main": {"path": "/srv/app/worker/config.json", "format": "json",
                                "editable_keys": ["interval", "batch_size"], "bounds": {"interval": [1, 300], "batch_size": [1, 64]}}},
           "compose": {"file": "/srv/app/compose.yaml", "service": "worker", "project_dir": "/srv/app"}}
```

**需要扫码登录的机器人：掉线只通知人**

```json
"chat-bot": {"container": "chat-bot", "mutable": true, "login_sensitive": true}
"chat-bot:LOGIN_LOST": {"targets": ["chat-bot"], "needs_scan": true}
```

## 上线前用模拟智能体演练

把 `config.json` 的 `agent` 临时换成 `{"kind": "mock", "plans": {...}}`，给某个故障码写一个请求序列（和真实智能体发的 `ops` 请求一模一样），再用 `server-autoops enqueue` 投一条告警、`server-autoops drain` 处理（先停常驻服务）。可以确认 broker 允许和拒绝的正是你想要的。写法参考 `server_autoops/demo.py` 里的 `plans`；`"$sha256:<服务>/<配置别名>"` 会被替换成最近一次 `read_config` 得到的哈希。
