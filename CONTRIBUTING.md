# 参与贡献

谢谢关注！提 issue 或 PR 前请看这几条。

## 开发环境

只需要 Python 3.10+，没有第三方依赖。

```sh
python3 -m unittest discover -s tests -t . -v     # 单元测试
python3 -I bin/server-autoops.py demo             # 离线端到端演示
python3 -I bin/server-autoops.py policy check examples/policy.example.json
scripts/build-release.sh                          # 测试 + 演示 + 构建 dist/
```

## 原则

- **安全边界只能收紧，不能放宽。** 任何让智能体多一种能力的改动（新的 op、新的探针类型、放宽路径检查），都要在 PR 里说明威胁模型，并补上「越权请求被拒绝」的测试。
- **结论只认探针。** 不要加「模型说修好了就算修好」的路径。
- **不丢告警。** 预算、内存、熔断只能推迟，不能丢弃；测试要覆盖重启后的状态。
- **只用标准库。** 运行时不引入第三方 Python 包（`tomli` 作为可选例外）。
- **中文文档同步更新。** 新增配置项要写进 `docs/CONFIGURATION.md`（含义、类型、默认值、示例、什么时候改），policy 字段要同时改 `server_autoops/policy.py`、`schema/policy.schema.json` 和 `docs/POLICY.md`。

## 提交

- 一个 PR 做一件事，附测试。
- 不要提交真实的 `config.json`、`policy.json`、密钥、`runs/`、状态库、日志或截图。
- 安全问题不要发公开 issue，见 [SECURITY.md](SECURITY.md)。
