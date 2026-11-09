# 安全问题报告

如果你发现了 server-autoops 的安全问题（例如智能体能绕过白名单、broker 能被诱导操作名单外的目标或路径、秘密泄露到日志或通知），请**不要**发公开 issue。

请通过 GitHub 的私密漏洞报告提交：仓库页面 → Security → Advisories → **Report a vulnerability**（https://github.com/rmqg/server-autoops/security/advisories/new），或发送邮件至 `ruomuqiuguang@icloud.com`。

请尽量附上：版本（`server-autoops version`）、Python 和发行版版本、最小化的 policy 片段、复现步骤、你看到的结果。请不要附上真实的密钥、Bark 地址或业务日志。

我会在 7 天内回复确认。修复发布前请不要公开细节。

支持范围：只维护最新的发布版本。

安全设计本身见 [docs/SECURITY.md](docs/SECURITY.md)。
