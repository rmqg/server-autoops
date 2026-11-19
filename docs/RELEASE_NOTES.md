# server-autoops v0.1.1

比 0.1.0 更少「写死的规则」，更多交给 policy 和智能体自己判断；同时把作者本机的路径从包里彻底搬了出去，任何人都能用自己的目录跑。

## 亮点

- **故障码兜底**：`default_targets` 让没登记过的故障码按告警组回退到一组默认目标，不再一律「无映射、受阻」。要不要真的动手，仍由每个服务的 `mutable`/`login_sensitive` 和故障的 `needs_scan`/`diagnose_only` 决定。
- **代码取证与补丁**：智能体可以 `read_file` 只读取证、`patch_file` 打补丁，范围由服务级 `code_roots`/`code_files` 限定，而这两个又必须落在部署方用 `SERVER_AUTOOPS_CODE_ROOTS` 声明的目录里——policy 扩大不了，模型更扩大不了。补丁同样走备份、CAS、原子替换、探针验证和回滚。
- **`host_exec` 探针**：在宿主上直接执行 policy 写明的 argv（不经 shell），脚本必须 root 属主、非 group/world 可写、不是链接。
- **更像人写的通知**：日报正文由模型写一句话结论文本再推送，正文带一行统计；安静日只发一句。「需人工」「连续失败熔断」也只保留结论和最关键的因果，不再把整段诊断原文灌进推送。
- **不再刷屏**：微信桌面看门狗连续 3 次异常、连续 2 次恢复才通知，秒级抖动不会再来回「异常 / 已恢复」。

## 升级说明

- 包内不再有任何站点路径。用到 `read_file`/`patch_file` 的部署，需要在服务单元里设置
  `SERVER_AUTOOPS_CODE_ROOTS=<目录1>:<目录2>`（`os.pathsep` 分隔，最多 4 个绝对路径）。
- 需要把智能体结案摘要写给下游日报的，在 `config.json` 里加 `agent.notification_summary_path`（绝对文件路径）；不配就不发布。
- 配置、policy 格式版本仍是 1，旧 `policy.json` 可直接用。

## 要求

- Linux，Python **3.10+**（只用标准库；TOML 配置需要 3.11+ 或 `tomli`）
- 真正修容器时需要 Docker；智能体需要用 `agent-image/Dockerfile` 构建的镜像（`@deepseek-ai/dsh` 0.2.1-rc.5，MIT）和一个 OpenAI 兼容的模型接口

## 支持情况

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| Python | 3.10.12、3.11.2、3.12.3、3.14.10 | 3.13（仅 CI）；≤3.9 不支持 |
| 发行版 | Debian 12、Ubuntu 24.04（全新容器，arm64 + amd64）；iStoreOS 24.10.8（替换生产服务） | 其他发行版 |
| Docker | 29.4.0 arm64、27.3.1 amd64：重启、启动、日志、HTTP 探针、改配置与回滚 | 其他版本；`recreate`、`exec` 探针只有单元测试 |
