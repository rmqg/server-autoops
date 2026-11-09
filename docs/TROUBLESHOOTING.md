# 排查

先跑这两条，绝大多数问题都会直接给出修复命令：

```sh
sudo server-autoops doctor
sudo server-autoops status
```

日志：systemd 用 `journalctl -u server-autoops -n 200`；OpenWrt 用 `logread -e server-autoops`。每次运行的细节在 `/var/lib/server-autoops/runs/<run_id>/`。

## 按症状

### 告警写进去了，但什么都没发生

1. 服务在跑吗？`systemctl status server-autoops`（OpenWrt：`/etc/init.d/server-autoops status`）。
2. 告警写到 scheduler 读的那个文件了吗？`grep '"stream"' /etc/server-autoops/config.json`，再 `tail -n 3` 那个文件。
3. 第一次启动是从文件**末尾**开始读的，启动前已经在文件里的告警不会处理（防止重放历史）。确实要处理时，停服务后 `server-autoops replay --drain`。
4. 告警的 `level` 必须是 `failure` 或 `recovery`；`warning` 只记录不处理。带 `"auto_recovery_handled": true` 的也会被忽略。
5. `server-autoops status` 里 `blocked` 增加了？说明 policy 里没有这个 `service:code`：

   ```sh
   sqlite3 /var/lib/server-autoops/state/queue.sqlite3 "select service,fault_code,status,last_error from queue order by id desc limit 5"
   ```

   `last_error` 是 `no policy mapping; agent not started` 就去 policy 的 `faults` 里加上这个键（注意大小写和冒号两边）。

### 一直 pending，不启动智能体

先看是不是维护模式没关：`server-autoops maintenance status`（`status` 输出里的 `maintenance` 不是 null）。关掉：`server-autoops maintenance off`。

```sh
sqlite3 /var/lib/server-autoops/state/queue.sqlite3 "select id,service,fault_code,datetime(due_at,'unixepoch'),last_error from queue where status='pending'"
sqlite3 /var/lib/server-autoops/state/queue.sqlite3 "select service,failures,datetime(open_until,'unixepoch') from breakers"
```

| 原因 | 怎么看出来 | 处理 |
|---|---|---|
| policy 有错误 | doctor 报 policy 错误；收到「授权策略无法读取」通知 | `server-autoops policy check`，改好即可，不用重启 |
| 内存不足 | doctor 的 memory 一行；`free -m` | 等内存回落，或调低 `min_available_mb` |
| 冷却 | `due_at` 在几分钟后 | 正常，等待 |
| 熔断 | `breakers.open_until` 在未来 | 先人工修好服务；修好后发恢复事件，或等退避到期自动再试 |
| 每小时/每天预算用完 | 收到「触发限流」通知 | 等到点；或调 `service_hourly_limit`、`daily_limit` |
| 另一个 worker 持有锁 | `run --once`/`drain` 报 `scheduler worker is already running` | 调试命令要先停常驻服务 |

### 运行了，但结论是 failed

看这次运行：

```sh
R=/var/lib/server-autoops/runs/<run_id>
cat $R/result.json | python3 -m json.tool       # verification 里每个目标的探针结果
cat $R/actions.jsonl                            # broker 实际做了什么
tail -n 50 $R/agent.jsonl                       # 智能体看到和请求了什么
tail -n 5 $R/output.log
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `agent.jsonl` 里有 `Unable to find image` | 镜像没构建或标签不对 | 按 INSTALL 第 4 步构建，核对 `agent.image` |
| 有 401/403、`ECONNREFUSED`、模型相关报错 | 模型地址、密钥或网络 | 核对 `agent.model`、`model.env`；`agent.network` 不能是 `none` |
| `运维执行异常：PermissionError` | 不是 root 运行，或 policy 里的文件属主特殊 | 用 root 运行服务 |
| `target is not authorized for this event` 很多 | 智能体想碰的服务不在这个故障的 `targets` 里 | 正常拒绝；需要时把服务加进 `targets` |
| `service is not eligible for mutation` | 服务 `mutable: false` 或 `login_sensitive: true` | 按需改 policy |
| `target is permanently protected` | 名字命中 `protected_name_patterns` | 换名字或调整这个列表 |
| `only allow-listed scalar keys may be patched` | 想改的键不在 `editable_keys` | 确认真的需要后再加 |
| `configuration CAS mismatch` | 读和改之间文件被改了 | 正常保护；会在下次运行重试 |
| `patch failed; original configuration restored` | 改完探针没通过，已经回滚 | 看服务日志找真正原因 |
| `patch failed and rollback verification failed` | 回滚后服务仍不健康 | **需要人工**：备份在 `$R/backups/`，原文件已恢复 |
| 探针 `error: URLError` | HTTP 探针地址不通 | 在宿主上 `curl` 那个地址；注意是宿主访问，不是容器内 |
| `Agent 已超时终止` | 超过 `agent_timeout - agent_grace_seconds` | 调大 `agent_timeout`，或换更快的模型 |

### 结论是「容器在跑、业务未验证」

这个服务只有 `running` 探针，又没有 Docker 健康检查。给它配 `http`、`exec` 或在镜像里加 `HEALTHCHECK`，见 [POLICY.md](POLICY.md#探针-probe)。

### needs_scan：需要人工

可能是：故障本身标了 `needs_scan`；或者运行被打断（重启、超时）时审计里已经有改动；或者结果文件不可信。处理完后，发一条恢复事件或让看门狗报恢复：

```sh
server-autoops alert --service <service> --fault-code <CODE>_RECOVERED --level recovery --title 已人工处理 --summary 已人工处理 --no-push
```

`needs_scan` 状态的队列项不会自动重试；新告警会重新排队。

### 收不到通知

```sh
sudo server-autoops notify-test
sqlite3 /var/lib/server-autoops/state/queue.sqlite3 "select kind,state,attempts,last_error from notifications order by id desc limit 10"
```

| 现象 | 处理 |
|---|---|
| `notify.kind` 是 `none` / 状态都是 `suppressed` | 按 CONFIGURATION 配 notify，并确认 `notifications_enabled` 不是 false |
| `Bark URL file must not be readable by group/others` | `chmod 600 /etc/server-autoops/bark.url` |
| `last_error` 是 `SchedulerError`，attempts 递增 | 后端返回失败：检查 Bark 地址（设备 key）、网络、代理 |
| 手机上没响 | 默认是 `passive`（不响铃）。想响铃：`notify.levels` 里把需要的种类设成 `active` |

### 安装和启动

| 现象 | 处理 |
|---|---|
| `Python 版本太旧` | 需要 3.10+；Debian 11 等旧系统请用更新的 Python |
| `Python 缺少 sqlite3 模块` | OpenWrt：`opkg install python3-sqlite3` |
| `config field 'timezone': ... not available` | 系统没有 tzdata：`apt install tzdata`，或把 `timezone` 改成 `UTC+08:00` |
| `TOML configs need Python 3.11+` | 升级 Python，或 `pip install tomli`，或改用 JSON 配置 |
| systemd 里 `status=203/EXEC` | `/usr/local/bin/server-autoops` 链接坏了：重新 `./install.sh` |
| OpenWrt 开机后服务没起来 | init 脚本会等 `/var/run/docker.sock` 和配置文件就绪；`ls /etc/rc.d | grep server-autoops` 确认已 enable；固件升级后确认 `/etc/sysupgrade.conf` 里有它 |

### 安装向导

| 现象 | 处理 |
|---|---|
| `./install.sh` 没有提问，只打印「没有终端，跳过提问」 | 你是在脚本或自动化工具里运行的。用答案文件：`cp examples/install.env.example install.env && chmod 600 install.env`，改好后 `./install.sh --config install.env` |
| 向导报「这些设置组合起来不合法」 | 什么都没改。按提示改答案或 `config.json`，再 `sudo server-autoops setup` |
| 选了入门演练，但提示「已经有白名单，入门演练不会覆盖它」 | 故意的：不会覆盖你已有的 policy。确实要演练就先备份再清空：`cp /etc/server-autoops/policy.json /root/policy.bak && printf '{"version": 1, "services": {}, "faults": {}}\n' > /etc/server-autoops/policy.json`，再 `sudo server-autoops setup` |
| 刚装完 doctor 说「状态库正在初始化」 | 服务刚启动，几秒后再 `sudo server-autoops doctor` |
| 入门演练报警后容器没被启动 | `server-autoops status` 看 `blocked`：告警的 `--service quickstart-web --fault-code WEB_DOWN` 要一字不差；看 `pending`：可能在冷却，同一个故障 5 分钟内只修一次（`cooldown_seconds`），等一会儿或换个容器演练 |

### 在真实服务器上替换旧版时踩过的坑（iStoreOS / OpenWrt 上实测）

这些在 0.1.0 里都已经修好或者有了应对办法，列在这里方便你认出同样的症状。

| 现象 | 原因 | 处理 |
|---|---|---|
| 智能体一启动就失败，日志里有 `AF_UNIX path too long` 或 socket 连不上 | Unix socket 的路径最多约 107 字节，运行目录放得很深时超长 | 0.1.0 已经把 socket 放进 `/run`（或 `/tmp`）下的一次性短目录；自己设了 `agent.bridge_dir` 的，保持在 60 个字符以内 |
| OpenWrt 上智能体连不上 broker | OpenWrt 的 `/run` 是符号链接，按链接路径建 socket 目录再交给 Docker 挂载会对不上 | 0.1.0 先解析成真实路径；自己设 `bridge_dir` 时写真实路径（`readlink -f /run`） |
| 日报时间差 8 小时；或报 `timezone ... not available` | 精简系统（OpenWrt）的 Python 没有 tzdata，`Asia/Shanghai` 用不了 | 安装脚本检测到没有 tzdata 时自动写成固定偏移（如 `UTC+08:00`）；也可以 `sudo server-autoops setup` 在时区那一步输入 `UTC+08:00` |
| LuCI「下载配置备份」里出现了 Bark 地址 | OpenWrt 的 `/etc/sysupgrade.conf` 里列的目录会被打进配置备份 | 0.1.0 只在配置目录位于 `/etc` 下时才写进 sysupgrade.conf。配置放在 `/etc` 下时 `bark.url` 也会进备份：备份文件不要外传；或安装时用 `--etc` 把配置放到数据盘（固件升级后要自己重装服务） |
| 智能体容器起不来，`docker run` 报网络相关的错误（找不到 bridge 网络一类） | 跑智能体的 dockerd（比如为隔离单独起的第二套 dockerd）配置了 `"bridge": "none"` 或 `"iptables": false`，没有 bridge 网络 | 给那个 dockerd 打开默认 bridge；或者在 `config.json` 里把 `agent.network` 改成 `host`（智能体能访问本机所有回环端口，doctor 会一直提醒，了解风险后再用）。`agent.docker_host` 写那个 dockerd 的 socket |
| 开机后服务没起来，第二套 dockerd 或数据盘还没就绪 | procd 启动得比它们早 | 安装时加 `--wait-path <socket 或数据盘上的配置路径>`（可以写多次） |
| 固件升级后 `server-autoops` 命令没了 | 升级只保留 sysupgrade.conf 里列的文件，`/usr/local/bin` 的链接丢了 | 服务直接调用安装目录里的程序，不受影响；命令用 `/opt/server-autoops/bin/server-autoops`，或重新 `./install.sh` |

### 智能体会不会把我的网页版/别的 Compose 项目弄重启？

不会碰名单外的容器。但如果你用 Compose **构建**了智能体镜像，镜像会带 `com.docker.compose.project` 标签，某些守护 Compose 项目的工具会把智能体容器当成项目成员。用普通 `docker build` 构建；runner 每次 `docker run` 也会把这三个标签清空。检查：

```sh
docker image inspect local/server-autoops-agent:0.1.0 --format '{{json .Config.Labels}}'
```

## 收集信息提问题

```sh
server-autoops version
sudo server-autoops doctor --json
sudo server-autoops status
```

请**不要**贴 `model.env`、`bark.url`、`runs/` 里的原始内容（可能含业务日志）。
