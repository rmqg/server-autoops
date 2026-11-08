# server-autoops v0.1.0

第一个公开版本：Linux 服务器上「有白名单的」自动运维。看门狗报故障 → 持久队列 → 受限容器里的 LLM 智能体只能通过 root broker 执行 `policy.json` 白名单里的动作 → 结论只认 broker 的探针。

## 亮点

- `policy.json` 白名单（JSON Schema + `server-autoops policy check`）：目标由告警决定，只改白名单里的标量配置键并有范围限制；需要扫码/只诊断的故障不会被改动；名单外的一律拒绝并记为受阻。
- 配置事务：CAS、保持属主权限的备份、原子替换、重启或单服务重建、业务探针、失败自动回滚。
- 不失控：冷却、每小时/每天上限、连续失败熔断后指数退避（1h→24h，永不放弃）、低内存只延后、全局同时一个智能体。
- 通知默认全部 Bark `passive`；Bark 地址只放在 600 权限的文件里。
- `demo`：不用 Docker、不用模型、不联网的端到端演示（21 项检查）；`doctor`：自检并给出修复命令。

## 要求

- Linux，Python **3.10+**（只用标准库；TOML 配置需要 3.11+ 或 `tomli`）
- 真正修容器时需要 Docker；智能体需要用 `agent-image/Dockerfile` 构建的镜像（`@deepseek-ai/dsh` 0.2.1-rc.5，MIT）和一个 OpenAI 兼容的模型接口

## 支持情况

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| Python | 3.10.12、3.11.2、3.12.3、3.14.10 | 3.13（仅 CI）；≤3.9 不支持 |
| 发行版 | Debian 12、Ubuntu 24.04（全新容器，arm64 + amd64） | 其他发行版、OpenWrt/iStoreOS |
| Docker | 29.4.0 arm64：重启、启动、日志、HTTP 探针、改配置与回滚 | 其他版本；`recreate`、`exec` 探针只有单元测试 |
| 智能体 | dsh 0.2.1-rc.5 镜像（arm64）+ 本地假模型：只提供 `ops` 工具、插件到 broker、探针定结论 | 真实模型的修复运行；amd64 镜像构建 |
| 服务管理 | 无服务管理器（`--service none`） | systemd、OpenWrt procd |

详细记录见 `docs/VALIDATION.md`、`docs/COMPATIBILITY.md`。

## 安装

```sh
sha256sum -c SHA256SUMS
gh attestation verify server-autoops-0.1.0.tar.gz --repo rmqg/server-autoops
tar xzf server-autoops-0.1.0.tar.gz && cd server-autoops-0.1.0
./bin/server-autoops demo
sudo ./install.sh && sudo server-autoops doctor
```

然后按 `docs/INSTALL.md` 写白名单、构建智能体镜像、填模型密钥、配通知。
