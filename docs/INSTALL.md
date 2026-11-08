# 安装

以下命令以 root 运行（或加 `sudo`）。路径都是默认值，可以用 `./install.sh --help` 里的选项改。

## 0. 准备

| 需要 | 检查命令 | 没有时 |
|---|---|---|
| Linux（systemd 或 OpenWrt procd，也可以不用服务管理器） | `ls /run/systemd/system` 或 `ls /sbin/procd` | 用 `--service none`，自己用别的方式常驻 `server-autoops run` |
| Python 3.10+，带 sqlite3 | `python3 -c 'import sys, sqlite3; print(sys.version)'` | Debian/Ubuntu：`apt install python3`；OpenWrt：`opkg install python3 python3-sqlite3` |
| Docker（真正修容器、跑智能体时需要；demo 不需要） | `docker version` | 按发行版安装 Docker Engine |
| 一个 OpenAI 兼容的模型接口和密钥（用 dsh 智能体时） | — | 也可以先用 `mock` 或 `disabled` |

不需要 pip 安装任何东西：程序只用 Python 标准库。TOML 配置需要 Python 3.11+，或在 3.10 上 `pip install tomli`。

## 1. 下载并校验

```sh
curl -LO https://github.com/rmqg/server-autoops/releases/download/v0.1.0/server-autoops-0.1.0.tar.gz
curl -LO https://github.com/rmqg/server-autoops/releases/download/v0.1.0/SHA256SUMS
sha256sum -c SHA256SUMS                                   # 应输出: server-autoops-0.1.0.tar.gz: OK
gh attestation verify server-autoops-0.1.0.tar.gz --repo rmqg/server-autoops   # 可选，需要 gh 2.49+
tar xzf server-autoops-0.1.0.tar.gz && cd server-autoops-0.1.0
```

macOS 上没有 `sha256sum` 时用 `shasum -a 256 -c SHA256SUMS`。

## 2. 先跑离线演示（可选，但推荐）

```sh
./bin/server-autoops demo
```

最后一行应是 `结果：21/21 项通过`。它不需要 root、Docker 和网络。

## 3. 安装

```sh
./install.sh
```

它做的事：

1. 检查 Python 版本和 sqlite3 模块；
2. 把程序复制到 `/opt/server-autoops`，命令链接到 `/usr/local/bin/server-autoops`；
3. 建 `/etc/server-autoops`（755）和 `/var/lib/server-autoops`（700），生成 `config.json`（通知关闭、模型地址是占位符）、空白 `policy.json`（没有任何授权）、`model.env`（600，占位密钥）；**已有的不会覆盖**；
4. 识别服务管理器：systemd 装 `/etc/systemd/system/server-autoops.service`，OpenWrt 装 `/etc/init.d/server-autoops` 并加入 `/etc/sysupgrade.conf`；然后启用并启动（`--no-start` 跳过）；
5. 运行一次 `server-autoops doctor`。这时报「镜像不存在、模型地址是示例」是正常的。

空白白名单下服务是安全的：所有告警都只会记为受阻。

常用选项：`--wait-path PATH`（OpenWrt：开机时多等一个路径，比如第二套 dockerd 的 socket；可以写多次）、`--no-start`、`--service systemd|procd|none`、`--with-mem-guard`（同时装内存观察）、`--prefix`、`--etc`、`--state`、`--bin-dir`。

## 4. 构建智能体镜像（用 dsh 时）

```sh
docker build -t local/server-autoops-agent:0.1.0 /opt/server-autoops/agent-image
docker run --rm local/server-autoops-agent:0.1.0          # 应打印 dsh 版本号 0.2.1-rc.5
```

镜像从 npm 安装固定版本的 `@deepseek-ai/dsh`（MIT），大约几百 MB。**请用普通 `docker build`，不要用 Compose 构建**：Compose 会给镜像打上项目标签，之后智能体容器可能被当成那个 Compose 项目的成员，触发它的守护逻辑。运行时 runner 还会再把这些标签清空一遍。

## 5. 填模型和密钥

```sh
sudo editor /etc/server-autoops/model.env        # AUTOOPS_MODEL_API_KEY=你的密钥
sudo chmod 600 /etc/server-autoops/model.env
sudo editor /etc/server-autoops/config.json      # agent.model.base_url、agent.model.model
```

## 6. 写白名单

```sh
sudo cp /opt/server-autoops/examples/policy.example.json /etc/server-autoops/policy.json   # 或从最小示例开始
sudo editor /etc/server-autoops/policy.json
sudo server-autoops policy check /etc/server-autoops/policy.json
```

写法见 [POLICY.md](POLICY.md)。policy 改动不需要重启服务。

## 7. 配通知（可选）

见 [CONFIGURATION.md 的 notify 一节](CONFIGURATION.md#notify通知)，然后：

```sh
sudo systemctl restart server-autoops      # OpenWrt: /etc/init.d/server-autoops restart
sudo server-autoops notify-test
```

## 8. 接看门狗

任何能执行命令的检查脚本都可以：

```sh
server-autoops alert --service app --fault-code WEB_DOWN --level failure --title "web 异常" --summary "HTTP 503"
server-autoops alert --service app --fault-code WEB_DOWN_RECOVERED --level recovery --title "web 恢复" --summary "HTTP 200"
```

- `--service` + `--fault-code` 要和 policy 里 `faults` 的键对上（`app:WEB_DOWN`）。
- 看门狗自己已经修好了，加 `--auto-recovery-handled`，scheduler 就不会再派智能体。
- 只写告警流、不推送：`--no-push`。

现成的通用看门狗：`/opt/server-autoops/examples/watchdogs/container_http_watchdog.py`（容器 + HTTP 健康检查，带防抖和恢复事件）。可选示例检查：`examples/checks/onebot_status.sh`（OneBot 机器人在线状态）、`examples/checks/egress_country.sh`（出口 IP 地区）。

## 9. 最后自检

```sh
sudo server-autoops doctor
sudo server-autoops status
```

## 维护模式：动服务之前先暂停自动修复

升级、迁移、手动停启被 policy 管着的服务之前，先打开维护模式，否则看门狗报的故障会让智能体把你正在停的服务「修」回去：

```sh
server-autoops maintenance on --for 2h --reason "升级 app" --wait   # --wait：如果有智能体正在跑，等它结束（最多 10 分钟）
server-autoops maintenance status
# ……做你的维护……
server-autoops maintenance off                 # 维护期间积压的告警会接着处理
server-autoops maintenance off --drop-pending  # 或者：维护期间的告警都是预期内的，直接关掉不修
```

- 维护期间告警照常读取、合并、记录；恢复事件照常取消排队中的告警。
- 不启动智能体、不占用每小时/每天的额度、不计熔断。
- `--for` 默认 6 小时，到期自动结束并发一条通知；`--for 0` 表示不自动结束（doctor 会一直提醒）。
- 状态存在状态库里，服务重启后仍然有效；日报里也会提示维护模式还开着。
- 正在进行的那一次运行不会被打断；用 `--wait` 等它结束。

## 可选组件

- **mem-guard**：只观察的内存监视，记录趋势、内存紧张时存进程快照、持续严重时发一条 warning 告警。`./install.sh --with-mem-guard`，参数见 `contrib/mem-guard/mem-guard.env.example`（复制到 `/etc/server-autoops/mem-guard.env`）。
- **小内存 zram 的 sysctl**：`contrib/sysctl/90-low-memory-zram.conf`，逐项看过再复制到 `/etc/sysctl.d/`。
- **OpenWrt**：`contrib/openwrt/hw-watchdog.init`（让晚加载的硬件看门狗驱动生效）、`contrib/openwrt/late-logfile.init`（日志盘晚挂载时补起写文件的日志实例）。复制到 `/etc/init.d/`，`chmod +x` 后 `enable`。

## 升级

```sh
tar xzf server-autoops-<新版本>.tar.gz && cd server-autoops-<新版本>
sudo ./install.sh            # 只替换程序，保留 config.json、policy.json、状态库和运行记录
sudo server-autoops doctor
```

升级智能体镜像时用新标签构建，再改 `agent.image`，不要覆盖正在用的标签。

## 停用和卸载

```sh
sudo systemctl disable --now server-autoops          # 只停用，不删任何东西；不会停止任何业务服务
sudo /opt/server-autoops/uninstall.sh                # 删除程序和服务，保留配置和数据
sudo /opt/server-autoops/uninstall.sh --purge        # 连配置、状态库、运行记录一起删除
```

## 不用 root 试用

```sh
./install.sh --prefix ~/autoops/opt --etc ~/autoops/etc --state ~/autoops/var --bin-dir ~/autoops/bin --service none
~/autoops/bin/server-autoops doctor --config ~/autoops/etc/config.json
```

这样能检查配置和跑 demo；真正操作 Docker 一般需要 root。
