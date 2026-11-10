# OfferPilot Lite

本地优先的招聘邮件、求职待办与提醒助手，目标平台 Windows 10/11。

当前版本 0.2.1：采用 Windows 独立窗口，修复旧实例资源不完整时安装后打开 404；已按用户实测反馈修复日期时间填入，并完成 P1 事件管理：地点/会议链接、候选日期、关联原事件改期/取消、周日历与冲突提示。自动化和本机验收通过；P0 真实邮箱七天、干净 Windows 和用户验收仍待完成。

- [当前进度与下一步](docs/progress.md)
- [实施与验证日志](docs/implementation-log.md)
- [P0 逐项验收与待验记录](docs/p0-acceptance.md)
- [P1 事件管理验收](docs/p1-events-acceptance.md)
- [独立窗口与启动修复验收](docs/desktop-acceptance.md)

- [详细设计总览](docs/superpowers/specs/2026-11-09-offerpilot-design.md)
- [评审与验收清单](docs/superpowers/specs/offerpilot/15-review-and-acceptance.md)
- [原始 PRD](OfferPilot_Documents_v1.0/docs/OfferPilot_PRD_v1.0.md)
- [原始技术概要](OfferPilot_Documents_v1.0/docs/OfferPilot_Technical_Design_v1.0.md)

原始文档保留在导入目录，新增设计和实施记录集中在根目录 docs/。历史设计中的“待确认”是原始评审状态，P0 授权以 [实施计划的确认记录](docs/superpowers/plans/2026-11-09-p0.md#approval) 为准。本轮增量以 [P1 事件管理设计](docs/superpowers/specs/2026-11-10-p1-events.md) 为准，其余 P1/P2 不在范围内。

## 本机使用

当前产物位于 `.worktrees/p0/dist/OfferPilot-Setup.exe`，可进行当前用户安装；便携版为同目录的 `OfferPilot/OfferPilot.exe`，需保留整个文件夹。关闭窗口后后台托盘继续运行；托盘“退出”会停止同步与提醒。程序默认使用 `%LOCALAPPDATA%/OfferPilot`，开发与检查数据使用项目 `.runtime/`，互不混用。

当前用户要求保留的测试安装和指定数据目录见[进度记录](docs/progress.md)。该实例以关闭托盘的测试参数启动；普通快捷方式使用默认数据目录，不是同一实例。

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
