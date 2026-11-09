# M02 数据与事务详细设计

状态：待确认｜范围：P0，标注部分 P1｜需求：G2/G3、NFR-03/04/05/09

## 1. 存储规则

SQLite WAL，启用 foreign_keys、busy_timeout=5000ms，可靠性优先使用 synchronous=FULL。每个用例一个 SQLAlchemy Session；禁止跨线程共享 Session。迁移由 Alembic 管理。

主键 UUID 文本；业务时间在数据库保存 UTC epoch 毫秒 INTEGER，API 用带 Z 的 RFC3339；仅日期保存 `YYYY-MM-DD`，时区保存 IANA 名。通用字段：`id, created_at, updated_at`；可编辑实体增加 `version INTEGER >=1`。默认单 profile UUID，相关外键必须同 profile，暂不承诺多租户隔离。

拟建：`persistence/models/` 按聚合拆分；`repositories/` 提供查询；`unit_of_work.py` 统一提交；`migrations/` 管理版本。

## 2. 表与关键约束

以下 `?` 表示可空，`JSON` 为经模型验证的 JSON 文本，除注明外列为非空。

| 表 | 核心字段 | 约束/索引 |
| --- | --- | --- |
| profiles | id, timezone | 首次生成单 profile |
| mail_accounts | profile_id, provider, address, credential_ref?, state, last_sync_at?, next_sync_at?, last_error_code? | unique(profile_id,provider,address) |
| mail_cursors | account_id, folder, uidvalidity?, last_uid, scan_from?, initial_upper_uid?, state | unique(account_id,folder)；last_uid≥0 |
| email_messages | profile_id, canonical_digest, message_id_header?, subject, from_addr, header_date?, internal_date?, first_seen_at, text_excerpt, text_hash, text_truncated, state, ignored_by_user | index(state,created_at)；digest 非全局唯一 |
| mail_occurrences | profile_id, message_id, account_id?, folder?, uidvalidity?, uid?, import_digest?, received_at? | IMAP unique(account_id,folder,uidvalidity,uid)；导入 unique(profile_id,import_digest) |
| sync_runs | account_id, status, started_at, finished_at?, fetched_count, saved_count, error_code? | 一个账户最多一个 queued/running |
| parse_runs | message_id, parser_version, source, status, lease_until?, source_cache_ref?, result_json?, error_code?, attempt_count | 可重复运行，历史保留；一封邮件最多一个 active |
| recruitment_events | message_id, application_id?, event_key, event_type, state, temporal_json, evidence_json, revision | unique(message_id,event_key) |
| review_items | message_id, parse_run_id, event_id?, reason_codes, proposed_json, state, version, resolved_at? | index(state,created_at) |
| applications | profile_id, company, position?, stage, source, closed_at?, manual_fields, version | 公司/岗位不设唯一，允许同公司不同批次 |
| application_history | application_id, old_stage?, new_stage, actor, event_id?, happened_at | P1 阶段历史，追加写 |
| tasks | profile_id, application_id?, event_id?, action_key?, title, kind, status, priority, temporal_json, due_at?, due_date?, starts_at?, ends_at?, timezone, precision, confirmed, manual_fields, schedule_revision, version | unique(event_id,action_key) 部分索引；index(status,due_at)、index(starts_at) |
| reminders | task_id, schedule_revision, offset_seconds, scheduled_for, fire_at, expires_at, channel, state, next_attempt_at?, lease_until?, attempt_count, idempotency_key, parent_reminder_id? | unique(idempotency_key)；index(state,fire_at)、index(state,next_attempt_at) |
| notification_attempts | reminder_id, attempt_no, started_at, finished_at?, result?, error_code?, provider_ref? | unique(reminder_id,attempt_no) |
| notification_items | reminder_id?, recovery_batch_id?, task_id?, kind, summary_json, created_at, dismissed_at? | unique(reminder_id) 非空时；应用内显示来源 |
| recovery_batches | run_id, recovered_at, task_ids_json, summary_state, attempt_started_at?, attempt_finished_at?, result?, error_code? | unique(run_id,recovered_at)；一个批次只发一次摘要，未知结果不重发 |
| settings | profile_id, key, value_json, version | unique(profile_id,key) |
| audit_events | actor, entity_type, entity_id, action, detail_json, happened_at | index(entity_type,entity_id,happened_at) |
| api_commands | profile_id, route, request_key, request_hash, result_entity_id?, status, expires_at | unique(profile_id,route,request_key) |
| maintenance_jobs | kind, state, progress_json, result_ref?, error_code?, lease_until? | 导入/导出/恢复等持久化进度 |

MIME 正文只保存规范化的有限片段，见 M03。`manual_fields` 是字段名集合，值保存在实体字段中；修改历史存审计，默认不复制整封邮件。`temporal_json` 的公共模型由 M05 定义，展开的时间列供索引查询，必须同事务从 temporal 派生，不接受各自独立修改。

可编辑实体（包括账户、邮件审核聚合、提醒和应用内卡片）均带 version；表中省略这些通用列。邮件状态统一为 new/classified/parsed/review_needed/ignored/parse_failed，候选重复是 review_needed 的原因，不额外发明终态。parse_runs 状态为 queued/running/succeeded/failed/interrupted。账户状态以 M03 为准。

## 3. 关系与去重

一个规范邮件有多个出现位置；一个邮件有多个事件；一个事件可有多个任务；任务可以没有邮件来源。删除来源不级联删除任务。删除缓存时事件保留最小证据或按用户选项清除，并标记 source_unavailable。

相同账户下，IMAP 出现位置唯一键解决重复轮询；UIDVALIDITY 改变后以规范化内容摘要、发件人、Date、Message-ID 一致性匹配旧邮件。只有全部可用关键字段与正文摘要一致才复用；缺失/冲突时保留新记录并进入 duplicate_candidate 审核，禁止仅用 Message-ID 合并。跨账户及转发默认仅提示候选，不自动合并。

`event_key/action_key` 为首次接受时分配的稳定标识，不用数组下标或 parser_version。重新解析先与既有事件/动作对齐，不能确定则产生差异审核。任务自然唯一键防止重复确认和重试制造副本。

## 4. 事务边界

| 用例 | 一个事务内提交的内容 |
| --- | --- |
| 保存同步批次 | 规范邮件、出现位置、待处理状态、连续成功范围游标 |
| 接受解析/审核 | 事件、任务、提醒、审核完成状态、审计、命令幂等结果 |
| 修改任务 | 乐观版本检查、人工字段、schedule_revision、旧提醒抑制、新计划、审计 |
| P1 确认改期 | 原任务更新、旧提醒抑制、新提醒、事件关联和审核结论 |
| 发送准备 | 原子领取、有效性复查、attempt started；不包含网络发送 |
| 发送结束 | attempt 结果、reminder 状态、应用内结果 |

写冲突最多短重试 3 次（50/150/450ms），仍锁定返回 DB_BUSY。乐观更新使用 `WHERE id=? AND version=?`，零行返回 VERSION_CONFLICT；不能覆盖另一个页面修改。

## 5. 数据校验与迁移

`ends_at > starts_at`；`precision=date` 时 due_at 为空；confirmed=false 时禁止自动生成 reminders；终结任务不得有可发送的未来提醒。状态枚举用 CHECK，实体关系用 FK 和服务层同 profile 校验。JSON 经 Pydantic 校验后写入。

升级前使用 SQLite backup API；先在副本试迁移和 integrity_check，再在独占维护状态迁移正式库。失败时保留原始快照、停止业务启动；恢复过程见 M13。拒绝以旧程序写入更新 schema 的数据库。

## 6. 验收

- 批次保存中途异常：整批回滚或仅推进连续已提交边界，不丢未落库邮件。
- 100 次重复同步和重复确认只产生预期任务数。
- 两个旧版本 PATCH 只有一个成功，另一个 409。
- 模拟任务修改事务中断：旧计划整体保留或新计划整体生效。
- 外键、唯一约束和日期精度约束在临时数据库集成测试中验证。
