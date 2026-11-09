# OfferPilot Lite 技术概要设计（HLD）

**版本：** V1.0（本地优先、可落地版）  
**日期：** 2026-11-09  
**平台：** Windows 10/11 首发；个人本地运行，后期小规模扩展  
**配套文档：**《OfferPilot Lite 产品需求文档 V1.0》

> **架构结论：** 一个 Python 主进程承载本地 API、邮箱轮询、任务调度、通知分发和托盘管理；SQLite 持久化业务数据；Windows 凭据库保存邮箱授权码；前端是内置静态资源，通过默认浏览器打开本地界面。开发时前后端分离，发布给普通用户时打包成一个图形安装包。不要在首版引入 Docker、Redis、消息队列和多 Agent 服务。

## 1. 架构设计目标与关键决策

### 1.1 优先级排序

1. **普通用户无门槛使用：** 安装→点击图标→连接邮箱→测试提醒；开发工具只出现在开发环境。
2. **本地数据掌控：** 不依赖自建云后台；用户的任务和邮件元数据存储在本机；AI 可关闭。
3. **可靠性可解释：** 发生断线、重试、漏提醒、时间歧义时有可见状态和修复路径。
4. **模块可替换：** 邮箱、解析器、提醒渠道与 UI 通过清晰接口耦合，而非整个系统微服务化。
5. **后续可迁移：** 使用稳定业务主键、UTC 时间、数据迁移与适配器接口，为可选常开服务预留边界。

### 1.2 推荐技术栈

| 层/组件 | 技术选择 | 采用原因及限制 |
| --- | --- | --- |
| 运行时 | Python 3.12（开发环境锁定版本） | 邮件、时间、AI 生态成熟；最终安装包捆绑 Python 运行时 |
| 本地 API | FastAPI + Uvicorn（单 worker） | 便于前端通信与将来扩展；只监听 `127.0.0.1` |
| 前端 | Vue 3 + Vite（编译成静态文件） | 容易做待办与看板；用户电脑无需 Node.js |
| UI 承载 | 系统默认浏览器 | 避免首次引入 WebView2/桌面容器依赖，界面仍像普通应用 |
| 数据访问 | SQLAlchemy 2 + Alembic | 数据模型与升级迁移清晰；避免手写散乱 SQL |
| 数据库 | SQLite（WAL、单机模式） | 不需安装数据库；不支持跨机器直接共享同一数据库文件 |
| 邮箱接入 | 标准库 `imaplib` + `email` | 最小依赖、支持 UID 增量同步；网络部分隔离到 Adapter |
| 时间解析 | `datetime` + `zoneinfo`，必要时 `dateparser` | 优先明确格式；模糊日期返回待确认 |
| AI 接口 | OpenAI 兼容 HTTP API（可选） | 可接多家模型；本地 Ollama 作为高级可选项 |
| 调度 | 数据库提醒表 + 单个 Worker 定时扫描 | 任务在 SQLite 中即为事实来源，避免进程重启丢提醒 |
| 系统凭据 | Python `keyring` / Windows Credential Locker | 邮箱授权码与模型 Key 不写入业务库 |
| 系统托盘 | `pystray`（可视实际稳定性评估） | 最小化到后台、打开界面、暂停同步、显式退出 |
| 打包安装 | PyInstaller `onedir` + Inno Setup | 交付 Windows 安装程序；包含运行时与前端编译产物 |
| 测试 | pytest + SQLite 临时库 + 邮件样本库 | 支持规则回归、断网与恢复模拟 |

**备选说明：** 若未来强烈需要完整独立桌面窗口，可在不修改核心服务的前提下添加 `pywebview` 壳；但 Windows 依赖 WebView2 运行时，安装测试和打包复杂度会增加。当前先不引入。

## 2. 部署与运行形态

### 2.1 普通用户看到的体验

- 下载 `OfferPilot-Setup.exe`，双击安装；用户无需执行 pip/npm/docker 命令。
- 第一次启动自动打开浏览器里的 `http://127.0.0.1:<本地端口>`，显示配置向导；不需要账户注册。
- 任务运行时，托盘图标显示“运行中 / 暂停 / 连接错误”；关闭浏览器页面并不等于退出后台程序。
- 用户通过托盘菜单可“打开界面、立即同步、暂停同步、退出”；如勾选“登录 Windows 后自动运行”，启动时只在托盘运行。
- 退出前弹出提示：“退出后不会继续收取邮件与发送提醒”；电脑睡眠或关机也不能可靠执行本地提醒。
- 安装与卸载不会默认偷偷上传数据；卸载前提供“保留或删除本地数据”的明确选择（若安装器不能可靠实现，则用软件内清理功能）。

### 2.2 一进程、数条逻辑执行链

```text
                    Windows 用户
                        |
             本机浏览器 UI (Vue)
                        |
            127.0.0.1 本地 HTTP
                        |
      +-----------------+------------------+
      |        OfferPilot.exe 进程          |
      |                                    |
      |  FastAPI（UI/API）                  |
      |  Mail Worker（定期 IMAP 同步）      |
      |  Parse Worker（规则/可选 LLM）      |
      |  Reminder Worker（定时扫描）        |
      |  Notification Adapter（系统/外部）   |
      |  Tray + App Lifecycle              |
      +-----------------+------------------+
                        |
             SQLite + Windows 凭据库
                        |
                本地数据与备份目录
```

一个进程 **不意味着** 所有任务堵在一个线程。API 与网络 I/O 使用事件循环或受控后台线程；IMAP 的阻塞调用不得占用 UI 请求线程。调度器只在单实例运行，避免重复扫描及通知。

### 2.3 建议数据目录

```text
%LOCALAPPDATA%\OfferPilot\
  config\settings.json          # 非敏感配置
  data\offerpilot.db            # SQLite 数据文件
  data\backups\                 # 数据备份
  logs\app.log                  # 脱敏诊断日志，滚动清理
  cache\                       # 临时资源，允许清理
```

**原则：** 安装目录与用户数据目录分开；升级程序不能覆盖数据库；Windows 凭据库中保存邮箱授权码和外部模型 Key 的引用项，数据库只保存 `credential_ref`。备份默认不包含密钥。

## 3. 模块设计及职责边界

| 模块 | 责任 | 禁止事项 |
| --- | --- | --- |
| `mail` | 连接邮箱、同步游标、抓取/规范化 MIME、记录结果 | 不直接创建提醒，不把认证信息写日志 |
| `classifier` | 规则过滤和邮件分类 | 不凭关键词直接写死截止时间 |
| `parser` | 生成规范化事件草稿、证据片段和不确定性 | 不执行邮件中指示的命令或调用未知链接 |
| `application` | 归档公司/岗位申请，关联新旧招聘事件 | 不盲目覆盖用户手工编辑的字段 |
| `task` | 创建、编辑、完成、取消、合并待办 | 不依赖通知渠道是否成功才能保存任务 |
| `reminder` | 根据任务规则计算待发送时刻，恢复/取消计划 | 不直接调用邮箱读取逻辑 |
| `notification` | Windows 通知及可选推送适配 | 不泄漏完整邮件内容到第三方渠道 |
| `settings/security` | 用户配置、凭据引用、隐私与日志策略 | 不返回明文密钥给前端 |
| `api/ui` | 页面数据与用户操作、状态反馈 | 不承载复杂分类规则或 IMAP 逻辑 |

### 3.1 关键接口（概念协议）

```python
class MailAdapter(Protocol):
    def test_connection(self) -> ConnectionResult: ...
    def sync_since(self, cursor: SyncCursor) -> list[NormalizedMail]: ...

class EventParser(Protocol):
    def parse(self, mail: NormalizedMail) -> ParseResult: ...

class NotificationAdapter(Protocol):
    def send(self, message: NotificationMessage) -> DeliveryResult: ...
```

生产代码还需要超时、取消、异步/线程边界、重试上下文和凭据获取，不建议直接把上述示意接口当成最终实现。关键是三个 Adapter 的调用方不依赖具体邮箱或机器人供应商。

## 4. 核心数据模型（SQLite）

至少包含下表，所有业务表建议使用 UUID 文本主键及 `created_at / updated_at` UTC 时间；关键字段建立唯一约束和索引。**单用户首版可以不做登录，但数据模型保留 `profile_id` 扩展位。**

| 表 | 关键字段 | 唯一性/用途 |
| --- | --- | --- |
| `mail_accounts` | id, provider, address, credential_ref, status, last_sync_at | 连接与健康状态，不存明文授权码 |
| `mail_cursors` | account_id, folder, uidvalidity, last_uid, last_sync_at | `(account_id, folder)` 唯一，重启续扫 |
| `email_messages` | id, account_id, folder, uidvalidity, uid, message_id, subject, from_addr, received_at, digest, state | `(account_id, folder, uidvalidity, uid)` 唯一 |
| `applications` | id, company, position, stage, source, closed_at | 公司/岗位招聘流程聚合，可手动维护 |
| `recruitment_events` | id, message_id_fk, application_id, event_type, temporal_json, evidence_json, parse_status, parser_version | 保留解析依据与版本；允许一封邮件多事件 |
| `tasks` | id, application_id, event_id, title, due_at, due_precision, status, priority, user_edited | 单独待办实体，可手动创建 |
| `reminders` | id, task_id, fire_at, channel, status, attempt_count, next_attempt_at, idempotency_key | 唯一 `idempotency_key` 保证逻辑提醒不重复排程 |
| `notification_attempts` | id, reminder_id, attempt_no, started_at, finished_at, result, error_code, provider_ref | 跟踪每次投递尝试；审计不保存敏感正文 |
| `audit_events` | id, actor, entity_type, entity_id, action, timestamp, detail_json | 记录用户修改、系统关联与状态切换 |

`user_edited` 应具体实现为“字段级人工覆盖标记”或独立 revision，避免 AI 再解析时覆盖用户手工校正的日期。数据库首次使用自动迁移，更新版本前做检查点和备份。

### 4.1 核心状态机

**邮件处理状态：** `new → classified → parsed / review_needed / ignored / parse_failed`。`parse_failed` 允许重试；人工忽略的邮件不应每次扫描重新提示。

**任务状态：** `pending → in_progress → done`，或 `pending/in_progress → cancelled`；过期视图用 `due_at < now` 派生显示，不建议把“逾期”做成不可逆状态。

**提醒状态：** `pending → claimed → sent`，失败可进入 `retry_wait → claimed`；超过最大尝试后 `failed`；任务完成/取消后 `suppressed`。由于第三方发送可能在服务超时后实际成功，因此需要约束重复发送并承认外部渠道可能存在“至少一次投递”语义。

## 5. 邮箱监听与同步设计

### 5.1 QQ 邮箱接入（首版）

- 用户在 QQ 邮箱网页中启用 IMAP 服务，生成单独授权码；程序使用授权码而非 QQ 主密码登录。
- 初始使用 **2～5 分钟可配置轮询**；首版默认 3 分钟，按邮箱限流及异常动态退避。
- 用户选择首次扫描范围：近 14 天、近 30 天或只关注此后的新邮件。历史扫描应限制批次规模，提供进度与暂停。
- 首次同步可先检查邮件头（发件人、主题、日期、UID），有潜在价值再获取正文；但不能只靠关键词过滤，否则可能漏掉格式不典型的招聘邮件。建议抽样复核被跳过的类别。

### 5.2 增量同步算法与边界

```text
对每个 mailbox / folder:
  1. 从 SQLite 读取 uidvalidity + last_uid
  2. 与 IMAP SELECT 返回的 UIDVALIDITY 比较
  3. 若一致，使用 UID SEARCH/ FETCH 获取 last_uid 之后的记录
  4. 若不同，启动该文件夹的安全重扫/重建索引，不复用旧 UID
  5. 抓取邮件并先以唯一键写入 email_messages
  6. 正文解析与业务任务写入同一受控事务或幂等工作流
  7. 当批次持久化成功后更新同步游标
  8. 网络异常则保留旧游标，退避重连并告警
```

IMAP UID 只在对应 UIDVALIDITY 生命周期内稳定，不能把不同文件夹的 UID 当作全局邮件 ID。跨文件夹/转发邮件可使用 `Message-ID + 内容指纹` 协助识别，但 `Message-ID` 可能缺失或重复，不能独自作为唯一键。

相对期限（“收信后48小时”）的参考时刻属于业务歧义：建议记录 IMAP `INTERNALDATE`、邮件 `Date` 与本机实际同步时刻，并要求用户确认推算依据；转发邮件尤其不能强行以同步时刻替代真实收到时刻。

### 5.3 邮件内容处理

只提取纯文本/安全 HTML 转文本内容，解析 MIME 编码、HTML 实体、链接与附件元信息。默认**不下载全部附件**、不执行脚本、不加载远程图片或追踪像素。邮件链接仅供用户手动打开，显示真实目标域名、提醒谨慎登录。首版不自动填表或点击招聘网站链接。

## 6. 事件解析引擎：规则优先，AI 可选

### 6.1 分层处理

1. **规则分类：** 结合主题、发件人、正文与否定词判断“招聘通知/普通邮件/待核查”，并识别常见事件类型。
2. **规则抽取：** 提取日期时间、公司和岗位关键词、动作提示，保存原始表达与 evidence span。
3. **可选 AI：** 只在规则不完整、复杂自然语言或用户主动重新解析时调用；输出严格 JSON 并做 schema 校验。
4. **确定性解析与审核：** 将明确日期转换为带时区的时间；对缺字段、相对时间、低置信度和矛盾邮件转入审核队列。
5. **事件关联：** 按公司/岗位/邮件线程/内容语义寻找已有申请与事件，无法可靠关联则人工选择。

### 6.2 示例结构化协议

```json
{
  "category": "interview_invitation",
  "company": "示例科技",
  "position": "算法工程师",
  "actions": ["参加技术面试", "确认是否出席"],
  "time": {
    "raw_text": "11月12日下午14:00（北京时间）",
    "kind": "absolute",
    "timezone": "Asia/Shanghai",
    "resolved_at": "2026-11-12T06:00:00Z",
    "precision": "minute",
    "needs_confirmation": false
  },
  "evidence": ["请于11月12日下午14:00参加线上面试"],
  "source_message_id": "internal-uuid",
  "confidence": 0.93
}
```

以上是**结构示例，不代表 AI 概率已校准**。上线前须用测试集判断何种分数/规则应进入人工确认。模型不得只输出最终日期而没有原文及参考时区。对于同邮件包含多个事件，协议应允许 `events[]`。

### 6.3 AI 接入原则

- 默认仅启用本地规则，不需要模型 Key，也不影响邮件监听和任务提醒。
- 用户可选择兼容 OpenAI 格式的模型 API（例如 DeepSeek）或自行安装 Ollama；本地小模型识别准确率需独立评测，不保证低配置电脑效果。
- 启用外部模型前显式告知处理的数据和成本；支持“仅发送主题/必要正文片段”及敏感信息遮盖；不发送邮箱授权码、会话 Cookie、招聘网站登录 Token。
- 结构化结果必须使用 Pydantic 等校验；超时/429/解析失败直接转 `review_needed` 或规则结果，不阻塞同步队列。
- **防提示注入：** 邮件正文始终是不可信数据，不允许指挥 AI 修改提醒策略、调用文件系统或向第三方发送内容；不配置任意工具执行权限。

## 7. 提醒调度与通知设计

### 7.1 使用数据库作为事实来源

首版不必为每个提醒依赖 APScheduler 的内存 Job。所有提醒先写入 SQLite `reminders`，单个后台 Worker 每 15～30 秒扫描 `fire_at <= now` 且状态可执行的记录，然后执行“占有→发送→落结果”。这样应用重启后可重新扫描未完成提醒，不会丢失任务定义。可用 APScheduler 驱动周期扫描，但**持久业务事实必须在数据库中**。

```text
Task 创建或修改
   → 在同一事务中重建有效 reminders
   → Reminder Worker 读取到期记录
   → 原子 claim（避免两个扫描循环同时领取）
   → NotificationAdapter.send(...)
   → 写入 notification_attempts + 最终状态
   → 失败: 指数退避重试 / 达上限标为 failed
```

在 SQLite 中通过事务（如 `BEGIN IMMEDIATE`）及唯一幂等键实现受控领取；应用只允许单实例运行。对第三方通知接口，超时后的投递状态可能未知，不能无条件立即重复发送。通知消息需包含标题、时间及本地任务入口，不必含完整邮件正文。

### 7.2 休眠、离线与迟到提醒策略

- **程序运行且电脑未休眠：** 定时扫描并调用可用通知渠道；仍可能受系统通知权限、网络和渠道限流影响。
- **浏览器窗口关闭，但后台托盘运行：** 仍应同步、排程与发送 Windows 通知。
- **电脑休眠/关机或显式退出：** 无法保证准点同步或发送。重新启动时补同步，记录 missed 状态；过期超过阈值的通知不连续轰炸，改为“有 N 件可能错过事项”的汇总提醒。
- **跨设备可靠提醒：** 后续可以选择常开电脑、家庭小服务器或远程托管 Worker；手机仅接收推送而不代表本地电脑可以关机后继续监听。

### 7.3 渠道 Adapter 与 QQ 限制

| 渠道 | 首版建议 | 前提或限制 |
| --- | --- | --- |
| Windows 系统通知 | P0 | 软件/用户会话需运行；测试通知按钮必需 |
| 应用内提醒中心 | P0 | 无通知权限时仍有可见状态 |
| Telegram / Bark / ntfy 等 | P1 | 经用户选择、密钥安全存储、外部网络/服务可用性需验证 |
| Webhook 自定义通知 | P1 | 配置端点、签名与数据最小化；默认不发送敏感正文 |
| QQ 机器人 | P2 / 实验性 | 官方主动推送限制明显，不作为唯一关键提醒渠道 |
| 邮箱二次提醒 | P2 | 需要单独 SMTP 授权与反垃圾考量，不能默认开启 |

Tencent 官方机器人文档注明 2025-04-21 起不再提供主动推送能力，故不推荐把“QQ 私聊定时提醒”写成首版必达能力。实际使用任何第三方通知都需提供“测试发送”和“失败状态”。

## 8. 本地 API 设计（首版）

所有 API 以 `/api/v1` 开头，均走本机 loopback，页面可经静态资源服务直接加载。以下为接口规划而不是最终协议：

| 方法/路径 | 用途 | 注意点 |
| --- | --- | --- |
| `GET /health` | 检查本地服务是否就绪 | 只返回非敏感状态 |
| `GET/POST /accounts` | 查询/创建邮箱连接 | `POST` 的授权码直接进入凭据库，不在响应中回显 |
| `POST /accounts/{id}/test` | 连接测试 | 返回分级故障原因 |
| `POST /sync/trigger` | 立即同步 | 后台执行、返回任务 ID |
| `GET /emails` | 显示解析邮件与状态 | 默认只返回摘要与必要证据 |
| `POST /emails/{id}/review` | 人工确认或忽略 | 应记录审核者和修改字段 |
| `GET/POST/PATCH /applications` | 求职岗位记录 | 关联邮件与招聘事件 |
| `GET/POST/PATCH /tasks` | 待办查询及修改 | 截止时间修改后重算 reminders |
| `GET /calendar` | 日历事件 | 默认只读 |
| `GET /notifications` | 提醒结果列表 | 显示 send/missed/failed 等 |
| `POST /notifications/test` | 测试提醒渠道 | 不携带真实招聘内容 |
| `GET/PATCH /settings` | 非敏感配置 | 密钥单独写安全凭据，不返回明文 |
| `POST /backup/export` | 用户主动备份 | 检查路径、隐私、文件权限 |

### 8.1 本地 API 安全边界

- **只绑定 `127.0.0.1`，不绑定 `0.0.0.0`。** 限制 `Host` 为 loopback 地址，避免 DNS rebinding 风险；不开放任意 CORS。
- 使用 SameSite 会话 Cookie、跨站请求伪造（CSRF）校验及 `Origin` 检查保护写操作；本地运行并不代表网页来源一定可信。
- 生成不可预测的启动会话凭据；禁止在访问日志中记录敏感 token。未来若远程访问，必须重新设计认证、TLS 与权限，不可简单修改监听地址。
- 限制导入文件尺寸、导出路径和附件类型；任何后端文件操作只能在白名单数据目录内进行。

## 9. 开机启动、程序更新与异常恢复

### 9.1 启动与托盘体验

- **单实例锁：** 若重复双击，只打开已有实例的本地页面，不启动第二个同步 Worker。
- **用户可选开机自启：** 首次安装默认询问是否启用；仅使用当前用户级启动项或计划任务，明确可关闭，不要求管理员权限运行后台逻辑。
- **启动顺序：** 检查数据目录与凭据库 → 校验数据库/迁移 → 初始化服务与 Worker → 启动托盘 → 打开欢迎页或后台安静运行。
- **退出处理：** 通知 Worker 停止接新工作、提交数据库事务、持久化同步状态、关闭服务，记录正常退出或异常恢复标记。

### 9.2 升级/迁移

版本安装包不覆盖用户数据；升级前自动生成带时间戳的本地 SQLite 备份（使用 SQLite backup API，避免直接复制运行中的 WAL 数据文件）。数据库迁移使用 Alembic；迁移失败时提示恢复而不是继续运行损坏的数据结构。代码层保持兼容旧数据库版本的迁移路径。

### 9.3 常见错误与用户可理解的提示

| 系统故障 | 友好提示 | 可操作按钮 |
| --- | --- | --- |
| 授权码错误 | “无法连接邮箱，请检查是否启用了 IMAP，并重新生成授权码” | 查看教程 / 更新授权码 / 重试 |
| 邮箱网络超时 | “暂时无法连接网络，已保留任务并将在稍后重试” | 立即重试 / 查看状态 |
| 解析不完整 | “这封邮件的截止时间尚不明确，请确认” | 查看原文 / 编辑时间 |
| 外部 AI 失败 | “智能解析不可用，仍可继续使用基础识别和提醒” | 切换规则模式 |
| 系统通知被关闭 | “Windows 可能阻止提醒，请先发送测试通知” | 测试通知 / 打开系统设置说明 |
| 电脑中断运行 | “恢复运行后发现部分事项可能已错过” | 查看漏提醒列表 |
| 数据迁移失败 | “升级未完成，已保留原始数据备份” | 恢复旧版本 / 导出诊断 |

## 10. 推荐项目目录

```text
offerpilot/
  README.md
  docs/
    PRD.md
    HLD.md
  backend/
    app/
      main.py                 # 启动、生命周期、FastAPI
      api/                    # HTTP 路由与输入校验
      domain/                 # Mail/Event/Task/Reminder 规则
      services/               # 业务用例与事务管理
      adapters/
        mail/                 # imap、未来 Gmail/Outlook
        ai/                   # 规则、OpenAI-compatible、Ollama
        notification/         # Windows、Webhook、其他
        secrets/              # keyring 适配
      workers/                # mail_sync、parse、reminders
      persistence/            # models、repositories、迁移
      security/               # 本地会话、CSRF、脱敏
      config/                 # 非敏感配置与路径
    tests/
      fixtures/eml/          # 脱敏邮件测试样本
      test_sync.py
      test_parser.py
      test_reminders.py
  frontend/
    src/                      # Vue 页面/组件/状态
    dist/                     # 发布时构建产物（自动生成）
  packaging/
    pyinstaller.spec
    OfferPilot.iss            # Inno Setup
  scripts/
    dev_start.ps1
    build_windows.ps1
```

**推荐依赖方向：** `api -> services -> domain`；`services -> adapters(接口) / repositories(接口)`；具体实现通过依赖注入接入。跨模块只传类型明确的数据结构，避免 services 直接调用全局 UI 对象。

## 11. 本地开发与可执行里程碑

### 11.1 开发者准备（仅开发人员执行）

- 安装 Python 3.12、Node.js LTS 与 Git，创建虚拟环境；用户安装包中不需要这些软件。
- 后端依赖使用锁定版本/受控范围；开发命令 `python -m ...`，前端 `npm run dev/build`；正式发布经 PyInstaller 打包。
- 为首次实现提供 `scripts/dev_start.ps1` 一键启动脚本，并在 README 展示操作步骤与常见错误。
- 建议使用 `pytest` 在无真实邮箱情况下先跑脱敏 `.eml` 测试，防止开发反复触发生产邮箱限制。

### 11.2 推荐顺序和每阶段完成标准

| 阶段 | 工作内容 | 退出标准 |
| --- | --- | --- |
| M1：本地壳 | FastAPI 静态前端、SQLite 迁移、单实例、诊断页 | 新用户双击程序打开本地页面并保存一条任务 |
| M2：业务模型 | 邮件导入、规则识别、事件关联、待办 CRUD | 通过 `.eml` 的正向/反向样本测试 |
| M3：提醒 | Reminder Worker、Windows 通知、重试、恢复 | 任务修改/取消/重启后无重复提醒 |
| M4：邮箱 | QQ IMAP、凭据安全存储、增量游标、回补 | 连续多轮同步无重复，断网后恢复 |
| M5：易用性 | 向导、托盘、安装包、开机自启可选 | 非技术用户无需命令行完成首启 |
| M6：AI 可选 | Schema、模型配置、规则回退、审核页面 | AI 错误不会破坏已有正确任务 |

### 11.3 自动化测试建议

**单元测试：** 日期与时区（UTC+8、UTC+9、跨日、夏令时）、任务重算、状态机、幂等键、分类规则。  
**集成测试：** 临时 SQLite、模拟 IMAP 断线、UIDVALIDITY 改变、通知返回错误或超时、升级/回滚。  
**端到端测试：** 新安装→连接测试→同步→审核→生成任务→更改截止→Windows 通知→完成任务→退出/重启→确认未重复。  
**异常测试：** 凭据库不可用、AI 返回非法 JSON、邮件 HTML 恶意内容、附件超大、通知权限关闭、电脑从休眠恢复。

**验证重点：** 每个工作包既要有 happy path，又要有至少一个明确失败路径；测试不能依赖单个真实招聘企业邮件。

## 12. 扩展架构：什么阶段才需要升级

| 触发条件 | 可能变化 | 首版为什么不用 |
| --- | --- | --- |
| 多台设备都要持续接收提醒 | 可选远程 Scheduler + 身份认证 + 安全同步 | 引入托管费用、合规及跨设备数据安全问题 |
| 用户数显著增长 | SQLite → PostgreSQL、后台任务队列 | 当前单机单用户无需复杂运维 |
| Gmail/Outlook 正式支持 | OAuth、官方通知 API、订阅续期、差异化 Adapter | 开发/验证成本高于首版 QQ IMAP |
| 需要更复杂 AI 流程 | 单次解析 → 多步抽取与规划，带人工审核 | 先验证识别收益，不为 Agent 而 Agent |
| 大规模邮件积压 | Worker 分离、队列、限流与批处理 | 首版批次处理 + SQLite 足够起步 |
| 用户需要独立桌面窗口 | 在本地服务外增加 pywebview 或其他原生壳 | 首版可免 WebView 运行时，安装更简单 |
| 深度跨平台移动提醒 | 手机伴侣 / PWA / 合规推送网关 | Windows 端无运行时不能保证全天同步 |

## 13. 发布前必须完成的检查清单

- [ ] 安装包可在无 Python/Node/Docker 的普通 Windows 机器上启动。
- [ ] 设置向导能连上 QQ 邮箱，错误时可以自行修复。
- [ ] 系统数据库、凭据库和日志均存储在正确的用户目录。
- [ ] 只监听 loopback；写 API 有 CSRF/Origin 防护。
- [ ] 读取同一邮件多次不产生重复待办。
- [ ] 模糊日期不会被标为精确截止时间。
- [ ] 修改、完成、取消任务后，旧提醒被更新/抑制。
- [ ] 退出、断网、系统休眠后有明确状态与补同步机制。
- [ ] 提醒渠道有测试按钮，通知尝试记录不暴露隐私。
- [ ] AI 关闭时核心任务流程仍可用。
- [ ] 备份、恢复、数据库迁移至少演练一次。
- [ ] 用户可停止监听、删除账户凭据和本地数据。

## 14. 参考与技术边界说明

以下为本设计所参考的公开资料。邮箱与机器人厂商可能调整政策，发布前仍需以实际账号重新验证。

1. QQ 邮箱授权码与 IMAP 配置（阿里云帮助中心，2026-10-10）：https://help.aliyun.com/zh/model-studio/connector/qq-mail
2. QQ 机器人发送消息及主动推送限制（腾讯官方开发文档）：https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/send.md
3. Python keyring / Windows Credential Locker：https://keyring.readthedocs.io/en/stable/
4. pywebview Windows 平台依赖（作为后续备选桌面壳）：https://github.com/r0x0r/pywebview/blob/master/docs/guide/installation.md
5. APScheduler 持久化调度注意事项：https://apscheduler.readthedocs.io/en/master/userguide.html
6. Windows 定时通知的系统限制（Microsoft Learn）：https://learn.microsoft.com/en-us/windows/apps/design/shell/tiles-and-notifications/scheduled-toast

**结语：** MVP 应把“数据模型正确、同步可恢复、任务可校对、提醒可追踪、安装足够简单”放在 AI 炫技之前。只要这五点成立，未来升级到跨邮箱、手机提醒或多用户服务都有清晰的技术路径。
