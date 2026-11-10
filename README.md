# OfferPilot Lite

本地优先的招聘邮件、求职待办与提醒助手，目标平台 Windows 10/11。

当前交付版本 **1.0.0-rc.4（候选版）**：可选 AI、申请看板、多邮箱、手机/webhook、准备事项、ICS 已集成。最终交付检查为 446 项后端/运行/版本测试、68 项前端测试通过；实际独立窗口、隔离安装与重启持久化通过。默认年为 2026，可在设置中逐年调整；明确日期时间自动填入并形成待办，列表按事件时间升序。

RC4 修复正常 AI 结果被误拒绝、中文/英文日期不能填入、同封多事项丢失、部分成功未保存和内容错误暂停整个队列。AI 返回事件 JSON，按原文核对日期、24 小时时刻和地点；晚上七点为 19:00。当前配置模型用四封固定虚构邮件验证通过，不代表所有邮件准确率已达标。7 天真实运行、许可样本和新用户观察等正式发布准入仍待验证。

- [P3验收与发布准入](docs/p3-acceptance.md)
- [最终交付与维护总结](docs/delivery-summary.md)
- [使用帮助](docs/user-guide.md)
- [隐私说明](docs/privacy.md)
- [当前进度与下一步](docs/progress.md)
- [P3 本机正式版交付计划](docs/p3-delivery-plan.md)
- [版本变更记录](CHANGELOG.md)
- [实施与验证日志](docs/implementation-log.md)
- [P0 逐项验收与待验记录](docs/p0-acceptance.md)
- [P1 事件管理验收](docs/p1-events-acceptance.md)
- [独立窗口与启动修复验收](docs/desktop-acceptance.md)

- [详细设计总览](docs/superpowers/specs/2026-11-09-offerpilot-design.md)
- [评审与验收清单](docs/superpowers/specs/offerpilot/15-review-and-acceptance.md)
- [原始 PRD](OfferPilot_Documents_v1.0/docs/OfferPilot_PRD_v1.0.md)
- [原始技术概要](OfferPilot_Documents_v1.0/docs/OfferPilot_Technical_Design_v1.0.md)

原始文档保留在导入目录，新增设计和实施记录集中在根目录 docs/。历史设计中的“待确认”是原始评审状态，P0 授权以 [实施计划的确认记录](docs/superpowers/plans/2026-11-09-p0.md#approval) 为准。本轮增量以 [P1 事件管理设计](docs/superpowers/specs/2026-11-10-p1-events.md) 为准，P3增量范围以用户确认的 [本机版计划](docs/p3-delivery-plan.md) 为准。

## 本机使用

本机候选版归档位于 `.worktrees/p0/dist/releases/1.0.0-rc.4/`，包含 `OfferPilot-Setup-1.0.0-rc.4.exe`、完整便携 ZIP 和 SHA256 清单；从新检出源码构建时，归档在该检出的 `dist/releases/`。安装产物不纳入 Git 源码，请按下方命令构建。安装器可进行当前用户安装；便携包解压后需保留整个 `OfferPilot` 文件夹。关闭窗口后后台托盘继续运行；右上角“退出程序”或托盘“退出”会停止同步与提醒。程序默认使用 `%LOCALAPPDATA%/OfferPilot`，开发与检查数据使用项目 `.runtime/`，互不混用。

连接邮箱后，应用运行期间自动扫描新邮件，默认每 3 分钟一次，可在设置改为 2 或 5 分钟；另加最多约 30 秒调度以及网络/解析时间。明确规则事项自动进入待办，AI 建议与不确定事项进入待确认。无需点击同步；退出或关机期间停止处理，下次运行补收。设置中可启用登录 Windows 后自动运行。

已保存的任务、事件、默认年份设置和提醒计划写入本机 SQLite 数据库，正常退出、关机与重启不会清空；程序重新打开后读取原目录。关机期间不发送本机提醒，重新打开时按已保存计划处理迟到提示和补同步。设置中可导出备份。

各版本安装包和完整便携 ZIP 归档在 `.worktrees/p0/dist/releases/<版本>/`，同目录 manifest 记录 SHA256 和源提交；已有版本不能覆盖。

本机现有试用入口继续使用指定测试数据目录，具体位置见[试用说明](docs/p3-trial.md)。普通安装快捷方式使用默认数据目录；切换入口时应核对数据目录，避免误认为原数据丢失。

安装后从桌面或开始菜单打开独立窗口，不启动浏览器。会议等外部链接仍由系统打开。需要 Microsoft Edge WebView2 Runtime 与 .NET Framework 4.6.2+（Windows 10/11 通常已具备）；缺少时会显示中文提示，可从 [Microsoft 官方页面](https://developer.microsoft.com/microsoft-edge/webview2/) 安装运行组件。

如果提示旧版正在使用数据，请先保存操作，从旧版托盘“退出”后重新打开新版；不需要卸载旧版，也不要删除本地数据。新版安装器不会强制关闭运行中的程序。

首次可以跳过邮箱，先添加待办或导入 `.eml`。收件识别可直接核对日期、时刻、地点和会议链接；多日期点击候选填入。改期/取消邮件需选择原事件；日历支持跳转日期和点击编辑。QQ 邮箱使用 IMAP 授权码，凭据存 Windows 凭据管理器；规则解析不需要模型密钥。时间不明确时进入待确认，只有确认的精确时刻才安排定时提醒。

## 开发环境

Python 由 uv 按 `.python-version` 创建项目 `.venv`，依赖冻结在 `uv.lock`；前端使用 Node 22 LTS 和 `frontend/package-lock.json`。当前验证环境为 Python 3.12.13、Node 22.19.0、npm 10.9.3。

```powershell
# 在仓库目录执行；不修改系统或 Conda 基础环境。
uv sync --frozen
cd frontend
npm ci
cd ..
./scripts/dev_start.ps1

# 全部自动化检查
./scripts/check.ps1

# Windows onedir；安装包另需 Inno Setup 的 ISCC.exe
./scripts/build_windows.ps1
./scripts/build_windows.ps1 -Installer
# 未加入 PATH 时可指定编译器；也检查项目 .tool-cache/inno/compiler。
./scripts/build_windows.ps1 -Installer -CompilerPath 'C:/path/to/ISCC.exe'
```

`.env.example` 只记录非敏感配置示例，不自动加载；可通过启动参数 `--data-dir` 或 `OFFERPILOT_DATA_DIR` 指定目录。不要把授权码、真实邮件和数据库提交到 Git。

## 日志与恢复

数据目录的 `logs/` 保存脱敏结构化日志，限制文件大小和轮换数量；业务变更另有数据库审计记录。日志不记录正文、授权码、会话密钥或链接查询参数。

运行期间每 24 小时生成一次不含凭据的本机备份，在 `backups/automatic-*.zip` 保留最近五份；手动下载和恢复前快照独立保留。设置页提供后台 CSV/JSON、备份、两阶段恢复和诊断包下载。已知旧备份先在隔离副本中校验并迁移。恢复替换当前数据后，需从托盘重新打开并重新授权邮箱。自动备份只有程序运行时才会执行。

邮件独立后台解析；成功后删除临时全文，失败暂存最长 7 天。诊断仅下载版本、健康、数量和白名单日志，不含业务库、正文、邮箱地址或凭据，不自动上传。
