# M14 工程环境、日志、测试与 Windows 交付详细设计

状态：待确认｜范围：P0｜依据：用户 AGENTS 指令“从0开始的项目用 Git 管理，做好日志管理和环境设置”

## 1. 当前实况与目标环境

2026-11-09 检查结果：原目录只有两份 Markdown 和配套 DOCX；Git 已初始化 main。检测到 Git 2.54.0.windows.1、Python 3.13.13、Node 22.19.0、npm 10.9.3、uv；Windows py launcher 未枚举出 Python 安装。此处只是本机事实，不代表项目环境已配置完成。

项目目标维持 HLD 的 Python 3.12。批准开发后用 uv 管理独立解释器和 `.venv`，不得污染系统/Conda 基础环境；具体补丁版本在首次安装时记录。Node 使用通过 Vite 兼容检查的受支持 LTS，若当前 22.19.0 不满足所选版本则升级项目工具链，不能直接忽略 engine 警告。

拟建 `pyproject.toml`、`uv.lock`、`.python-version`、`.env.example`、`frontend/package.json`、`package-lock.json`、Node 版本记录、`scripts/{dev_start,check,build_windows}.ps1`。开发命令统一由脚本封装；锁文件提交 Git，CI 使用冻结安装。

环境变量仅 `OFFERPILOT_ENV`、`OFFERPILOT_DATA_DIR`、`OFFERPILOT_LOG_LEVEL` 等非敏感运行参数；开发数据位于 `.runtime/dev`，测试数据用临时目录，禁止测试脚本读取真实用户 LocalAppData 数据库。

## 2. 仓库与提交策略

保留 `OfferPilot_Documents_v1.0/` 原始材料；新增根目录 docs 设计。按 `backend/app/{api,domain,services,adapters,workers,persistence,security,config,runtime}` 和 `frontend/src` 创建代码。

main 保存可验证基线；开发使用 `feat/<work-package>`，每个可运行工作包完成测试后小步提交。提交类别 docs/feat/fix/test/chore；提交说明写行为与验证，不记录凭据。当前不创建远程、不推送、不发布安装包。

更新 CHANGELOG 与 docs/implementation-log.md，记录日期、工作包、改动、命令结果、限制和下一步。决策变更同步到设计评审记录，不能只留聊天。

Git 排除虚拟环境、node_modules、数据库/WAL/SHM、真实邮件、日志、构建产物、.env；仅允许获准脱敏的测试 EML 入 fixtures。提交前检查暂存范围，避免把用户原始私密样本顺带提交。

首轮文档核对：新增 18 份 Markdown（含 README、CHANGELOG、总览、14 个模块、评审清单），已检查本地相对链接、代码块闭合、JSON 示例语法和占位词；不把文档检查描述为应用测试通过。业务代码与运行环境尚未建立。

## 3. 日志设计

运行路径 `%LOCALAPPDATA%/OfferPilot/logs/app.log`，JSON Lines；每文件 5 MiB，最多 5 个轮换文件，总量约 30 MiB，另删除超过 14 天的轮换记录。开发 INFO/DEBUG 可切，生产默认 INFO；DEBUG 同样脱敏。

字段：timestamp_utc、level、event、module、run_id、request_id?、job_id?、entity_id?、duration_ms?、error_code?、safe_detail。只允许已登记的 safe_detail 字段，不直接序列化异常对象或外部响应。

关键事件：app.start/stop/recovery、db.migration、mail.sync.start/complete/fail、parse.review_needed、task.changed、reminder.claimed/result、backup.complete/fail。审计业务历史存数据库，运行日志不作为唯一审计来源。

诊断导出仅含最近日志、版本、模块健康、计数和脱敏配置；不包含业务库、正文、凭据、用户目录完整路径。导出前显示内容清单，默认不上传。异常对用户显示原因+行动+request_id，完整栈仅写脱敏本地日志。

## 4. 测试分层与准入

| 层 | 验证内容 | 建议工具 |
| --- | --- | --- |
| 领域单元 | 时间、幂等、状态、提醒纯函数 | pytest，假时钟 |
| 存储集成 | SQLite 事务/唯一约束、迁移恢复、竞态 | 临时数据库、故障注入 |
| 适配器 | IMAP UID/断线、通知结果、AI 非法输出 | 模拟服务/契约测试 |
| API | 会话/CSRF、版本冲突、错误码、上传限制 | ASGI 测试客户端 |
| 前端 | 审核/任务编辑/连接向导关键路径 | Vitest，浏览器 E2E |
| Windows 实机 | 通知身份、托盘、开机自启、安装卸载、休眠恢复 | 干净用户/虚拟机+人工记录 |

每个业务工作包至少一个成功路径、一个真实失败路径；不为仅文档修改创建镜像测试。核心行为优先先写失败用例再实现。禁止用真实邮箱/付费模型作为默认测试依赖。

技术探索先验证三个高风险项：Python 3.12+通知库冻结打包；凭据库实际后端；托盘主线程与 API 线程协作。未通过时只替换对应适配器并记录，涉及架构/用户体验变化需再确认。

## 5. 安装、更新与卸载

PyInstaller onedir 包含解释器、前端 dist、规则资源、Alembic、tzdata、通知依赖。Inno Setup 当前用户安装，不需后台管理员权限；安装目录与数据目录分开，开始菜单快捷方式包含 AUMID。

开机自启使用当前用户启动项，默认关闭，UI 可撤销。普通首次运行弹向导；autostart 静默托盘。用户不需 Python/Node/Docker。

更新首版采用用户手动安装新版本，启动前备份和迁移；不实现未经验证的后台自动替换。安装器检测正在运行进程，要求正常退出；不覆盖用户数据。签名证书若尚未配置，在内测发布说明如实记录，不能声称已签名。

卸载默认保留个人数据；应用内提供显式清理。若安装器支持“同时删除数据”，必须调用同一受控清理逻辑并明确提醒凭据/备份影响。迁移失败回到恢复界面，旧版本不能强写新 schema。

## 6. 交付门槛

开发原型：至少 10 个合成/脱敏 EML 场景端到端通过。可用 MVP：P0 全项自动化通过、干净 Windows 普通用户安装验证、真实 QQ 连续 7 天无重复任务。发布：备份/恢复/升级演练、5 名非开发用户向导验证；P1 独立增加 AI 和渠道实测。

持续运行和真实用户门槛需要实际时间与用户参与，报告必须标记“未执行/进行中/已通过”，不能用模拟测试替代后声称已完成。
