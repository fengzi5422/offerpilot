# P3 本机版实现与交付计划

> 使用 subagent-driven-development 完成独立工作包，按本计划逐项测试、审查；主协调者保留集成职责。范围已由用户批准，可在修正审查问题后直接实施，无需再次请求范围批准。

**目标：** 在保留0.2.2和用户运行数据的基础上完成可配置AI、申请看板、163/126、webhook提醒、准备事项、ICS和正式发布验证。

**架构：** 沿用FastAPI/SQLAlchemy/SQLite/Vue/WebView2。外部I/O与SQL事务分离；AI异步有界串行工作器，提醒每通道独立计划；申请历史复用审计，准备项通过新表关联独立custom任务。共享配置/凭据和网络验证服务；备份恢复与清理覆盖所有新增数据。

**依据：** `docs/superpowers/specs/2026-11-10-p3-local-release.md` 与 `docs/p3-delivery-plan.md`。工作目录 `.worktrees/p0`，分支 `codex/p3-local-release`。现有v0.2.2原包只读保留。

## S0 公共边界与基线

- [ ] 核验0.2.2 manifest各文件SHA256，记录分支和用户保留数据位置；不能把真实数据库复制到测试样本。
- [ ] `backend/tests/test_version.py`：health、FastAPI、diagnostics、backup manifest版本相等；`scripts/test_release.py`比较pyproject、lock、frontend与安装器。先运行确认health 0.2.0失败，再新增`app/version.py`统一后端来源。
- [ ] `backend/tests/test_integrations.py`与`test_outbound.py`：默认关闭零联网、配置验证、错误补偿、GET不泄露密钥、非HTTPS/私网/重定向拒绝、localhost AI显式允许、设置改动使在途请求失效；先失败再实现`integrations.py`、`outbound.py`。
- [ ] 配置独立`Setting`记录，类型限定、密钥只存vault随机引用。API不使用带密钥的幂等回执，存储失败删除新引用、成功删除旧引用；禁用和删除独立。备份副本清引用/停用，恢复不联网，全部清理删除新增引用；覆盖`data_tools.py`行为测试。
- [ ] `uv run --frozen pytest backend/tests/test_version.py backend/tests/test_integrations.py backend/tests/test_outbound.py -q`和Ruff通过再提交公共边界。

## A AI / 0.3.0

- [ ] `test_ai.py`先覆盖规则充分不请求、关闭无请求、必要文本脱敏、证据定位、未知字段/虚构公司/时刻拒绝、输出大小、认证/限流/超时回退；实现`ai.py`（无工具兼容Chat Completions接口、严格schema、确定性时间校验）。
- [ ] `test_ai_worker.py`先覆盖并发1、有界持久队列、不占规则线程、3次错误熔断15分钟、最多一次网络/429延迟重试、认证不重试。新`ai_worker.py`读取Job快照，网络无SQL/maintenance锁；结果generation/邮件版本/设置版本校验。清理/恢复/退出/关闭后迟到结果不写回。
- [ ] `mail.py`首次规则提交后仅对缺失/歧义排队；人工主动AI重解析独立路由。不改已接受/忽略/人工字段，新建议必须审核，action身份按证据稳定且无重复；失败保留规则和最小正文，不把临时全文无限期保存。
- [ ] `AISettings.vue`和设置页：地址/模型/Key/启用及传输说明，默认关闭；测试请求固定虚构内容，结果显示受理/失败原因；收件页显示规则/AI来源、审核和重新建议。产品不显示内部网络/队列术语。
- [ ] 本地模拟服务端到端、全部检查和独立窗口测试后更新版本0.3.0、变更/验收记录，打包归档标签。真实AI必须用户自行配置，有真实结果才标真实通过。

## B 看板

- [ ] `test_application_board.py`先测试阶段任意跳转/历史、同公司不同岗位独立、关联双方version、重复命令、拆分保持task/reminder身份、原子失败回滚。
- [ ] `application_board.py`复用Application/Audit，原子关联/解除/迁移；API返回关联事件/来源/历史。禁止单纯公司名自动合并；旧Task普通patch关联路径同样版本防护。
- [ ] `ApplicationsPage.vue`七阶段、创建/编辑、阶段选择、事件关联/拆分与可见来源，TaskForm提供关联选择；窗口窄屏和键盘操作可用。JSON/CSV保留关系/历史，备份恢复验证。

## C webhook

- [ ] `test_webhook.py`先测HTTPS、payload隐私、稳定key、成功/400/401/429/5xx/超时/redirect/响应上限；实现一次请求适配器。
- [ ] `test_multichannel_reminders.py`：每通道独立提醒/结果、关闭/换目的地抑制旧计划、只排未来、完成/改期失效、unknown不重发、恢复/退出保护。`tasks.py`按通道创建计划，`reminders.py`依channel路由，窗口通道失败不影响另一通道结果。
- [ ] WebhookSettings配置与虚构测试按钮、回执/失败显示。默认隐私泛化且明确程序关闭无法发出。真实手机回执与模拟结果分别记录。

## D 多邮箱

- [ ] `test_mail_providers.py`：provider/address匹配、固定TLS主机、163/126 ID命令、只读BODY.PEEK、认证失败、移除重连复用、跨账户同MIME、游标与network恢复。
- [ ] `mail_providers.py`路由，沿用QQAdapter注入兼容现有测试。账户凭据独立，不发送测试邮件，不写服务器状态。
- [ ] `AccountConnect.vue`厂商选择/授权说明、设置页各账户独立同步/暂停/移除/重连与失败指引。真实163/126只由用户主动配置，模拟可独立开发。

## E 准备事项

- [ ] `test_preparation.py`覆盖模板幂等、增删改勾选、父任务校验/version、人工文本保护、独立提醒任务关联、父取消/改期保留、重启/恢复。
- [ ] 新迁移0005及Preparation表；`preparation.py`服务/API/`PreparationChecklist.vue`集成TaskForm。无日期不排提醒，有用户时刻才创建独立custom任务；删除子项抑制其自有提醒而不影响父任务。
- [ ] 扩展SCHEMA_REVISION、支持0004备份、schema白名单、counts、恢复指纹、清理顺序/JSON导出；隔离升级与回滚/恶意备份全量测试。

## F ICS

- [ ] `test_icalendar.py`使用标准库以外解析器往返：精确事件/截止/日期型、未知结束、中文75字节折行、CRLF注入、稳定UID/SEQUENCE、完成取消、隐私链接选择、未知日期排除计数。
- [ ] `icalendar.py`纯快照序列化，复用artifact/job认证下载；导出API参数白名单，Calendar/Settings提供按钮，导出说明非自动同步/不同软件重复导入可能重复。
- [ ] 目标日历实际导入需人工结果，自动解析结果不能冒充软件已导入。

## G 集成、0.4.0、候选与正式

- [ ] 每个包独立测试和审查通过，所有新增UI真实独立窗口可操作，保留响应式和错误状态；全功能0.4.0打包归档。
- [ ] `scripts/smoke_p3.py`使用虚构独立目录涵盖AI模拟服务、看板、准备、邮件双账号模拟、webhook模拟、ICS、恢复；现有windows/desktop/restart冒烟全部通过。
- [ ] 全量`./scripts/check.ps1`、安装前后hash/升级备份/新旧schema阻断回滚/卸载保留数据、未连接服务失败与离线使用、密钥/正文不出日志和诊断；本轮仅隔离安装不改用户安装。
- [ ] 发布报告逐条F01–F19、NFR01–10和A1–A10；许可脱敏100封评估、真实QQ连续7天、5人观察、干净Win10/11、系统通知/DPI/睡眠等不能伪造。提供具体可执行验收表和证据入口，缺项仅1.0.0-rc.1。
- [ ] 用户帮助/隐私/外部服务传输/数据目录/备份升级回退/未签名说明齐全；归档同版本不可覆盖。主分支仅在通过检查后本地整合，不对外上传。

## 当前进度

- 计划审查修正已写设计；独立审查结束，未发现剩余已确认阻断。
- S0、A–F前后端实现和审查已完成。1.0.0-rc.1最终全量377后端/运行/发布测试与63前端测试通过；实际打包程序、独立窗口、QA安装/卸载/重装、正常与异常重启、0.2.2升级及匹配快照回退通过，文档与版本包随交付封存。
- 具体证据见 docs/p3-acceptance.md；真实外部服务、7天、5人和干净Windows保持待验。组合复选项含这些外部条件时，不用整行勾选冒充全部通过。
- 执行调整：各独立包验证后在0.3.0一起集成，0.4.0用于最终程序验收与阶段封存，最终缺外部准入时标1.0.0-rc.1。原始版本均不可覆盖。
