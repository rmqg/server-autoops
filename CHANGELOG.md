# 变更记录

格式参考 Keep a Changelog，版本号遵循语义化版本。

## [0.1.0] - 2026-11-08

第一个公开版本。由作者自用的 NAS 自动运维（内部版本，2026-11-08 审查修订后）整理、通用化而来，使用全新的提交历史。

### 新增

- 告警驱动的自动修复：持久告警流游标（轮转、截断、半行、坏行隔离）、SQLite 队列、事件合并与恢复取消、组合码拆分与 `code_aliases`。
- `policy.json` 白名单（格式版本 1）：服务、Docker 守护进程、探针（running/health/http/exec）、可改配置键与范围、故障码映射、`needs_scan`/`diagnose_only`、保护名单；`server-autoops policy check` 和 JSON Schema。
- root broker：10 种结构化操作；配置事务（CAS、属主权限保持的备份、原子替换、单服务重建、探针、失败回滚）；脱敏；审计。
- 智能体后端：`dsh`（受限容器，唯一工具 `ops`）、`mock`（脚本，演示/CI/上线前演练）、`disabled`（只探针）。
- 预算与熔断：冷却、每服务每小时、全局每天上限；连续失败熔断后指数退避（默认 1h→24h，永不放弃，成功清零）；熔断通知每服务每天一条；低内存只延后。
- 通知：Bark（地址只从 600 文件或环境变量读，默认全部 `passive`）、命令、文件；持久出站箱和退避重试；每日日报心跳。
- 维护模式 `server-autoops maintenance on|off|status`：照常记录告警，但暂停自动修复；可到期自动结束、`--wait` 等在跑的运行结束、`off --drop-pending` 丢弃维护期间的告警。
- `server-autoops alert`（看门狗用）、`doctor`（自检）、`demo`（离线端到端演示，21 项检查）、`notify-test`。
- 安装脚本（systemd / OpenWrt procd / 无服务管理器）、卸载脚本。
- 可选组件：observe-only 内存观察 mem-guard、OpenWrt 硬件看门狗与延迟日志补救、小内存 zram sysctl 建议；通用容器 + HTTP 看门狗示例、OneBot 在线和出口地区的示例检查。
- 可复现的 release 包、SHA256SUMS、GitHub Actions 构建来源证明。

### 新手可用性（本地 tag 前移，版本号不变）

- `install.sh` 改成向导：先检查发行版、内核、CPU、Python/sqlite3、tzdata、Docker、内存、磁盘、服务管理器、root，缺什么就打印这台机器上的安装命令；再问智能体、Bark 地址、时区，密钥写进 600 文件；最后自检并按选择打印「下一步做什么」。新增 `--config <答案文件>`、`--non-interactive`；没有终端时不提问。
- 新命令 `server-autoops setup`：同一个向导，装好后可以反复运行；`setup --check-only` 只检查环境。
- 「入门演练」：不需要模型，写入只管 `quickstart-web` 容器的示例白名单和按脚本行动的模拟智能体，15 分钟内看到第一次真实的自动修复（Ubuntu 24.04 全新机器实测）。
- 新文档 `docs/QUICKSTART.md`：从全新服务器到第一次自动修复，每一步写明应该看到什么；README 开头标明难度。
- `doctor` 的每个错误和提醒都带可以直接复制的修复命令；服务刚启动、状态库还在建表时不再误报错误（改成提醒），安装脚本启动服务后等 3 秒再自检。
- 命令出意外时打印中文说明和下一步，不再只给英文异常栈（`AUTOOPS_DEBUG=1` 看完整信息）。
- `docs/TROUBLESHOOTING.md` 收录在 iStoreOS 上替换生产时踩过的坑。
- 新手照文档验收（弱模型扮演新手）：从全新服务器走到第一次自动修复（`repaired`），详见 `docs/VALIDATION.md` 第 6 节；补上 apt 锁被占用时怎么办。

### 相对内部版本的变化

- 所有路径、Docker 守护进程、保护名单、时区、通知、模型都改为配置；不再内置任何业务服务和私人检查项。
- 智能体容器默认改用 `bridge` 网络（内部版是 `host`）。
- 新增 policy 校验：校验失败时不启动任何智能体。
- systemd unit 使用 `KillMode=mixed`，停止服务时不打断 broker 正在执行的 docker 命令。
- broker 的 Unix socket 放进 /run（或 /tmp）下的一次性目录，不再受运行目录深度影响（socket 路径上限约 107 字节；在 nas 演练中发现；`/run` 是符号链接时先解析成真实路径）。
- 安装脚本：没有 tzdata 时自动把示例里的 `Asia/Shanghai` 改成本机固定偏移（在 nas 安装时发现）。
- 安装脚本：只有配置目录在 /etc 下时才写进 OpenWrt 的 sysupgrade.conf（避免把数据盘上的 bark.url 带进配置备份）。
- 安装脚本：服务文件直接调用安装目录里的程序；OpenWrt 可用 `--wait-path` 开机等待额外的 socket 或数据盘。
- 启动器以 `python3 -I` 运行并显式指定包路径，当前目录和 `PYTHON*` 环境变量不能注入模块。
- 新增模拟驱动（`driver: simulated`，需显式开启）和模拟智能体，用于不依赖 Docker 和模型的演示和测试。
