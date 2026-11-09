# M11 本地 API 详细设计

状态：待确认｜范围：P0，标注 P1｜需求：F01–F15 的交互合同

## 1. 通用合同

基址 `/api/v1`。除 `/health`（非敏感存活）、静态文件和 session/bootstrap 外均需 M10 会话；写请求还需 CSRF。拟建 `api/routes/{accounts,emails,reviews,tasks,applications,notifications,settings,backup,jobs,session}.py` 和 `api/schemas/`。

成功实体响应 `{data:..., request_id}`；分页 `{data:[], next_cursor:null|string, request_id}`。默认 limit=50，最大 200，游标基于排序字段+id 编码，非法游标 422。UTC 时间 RFC3339 Z，日期字符串和时区独立。

错误 `{error:{code,message,fields?,retryable},request_id}`，无异常栈/凭据。400 语义错误、401 会话失效、403 来源/CSRF、404 不存在、409 版本/状态冲突、413 超限、422 字段校验、429 限流、503 暂时不可用。

创建/审核/导入/同步使用 `Idempotency-Key` UUID；route+key+request_hash 记录在 api_commands，保留 7 天。同 key 同内容返回原结果；异内容 409。任务自然唯一约束提供超出保留期的业务去重。PATCH/动作要求 expected_version，响应带新 version。

## 2. 端点清单

以下路径均在基址下；`{id}` 为 UUID。表中未展开字段见对应模块的公共合同。

| 方法/路径 | 请求 | 响应/副作用 |
| --- | --- | --- |
| GET /health | 无 | ready、version；无账户信息 |
| POST /session/bootstrap | code | 会话 Cookie、csrf_token |
| GET /session | 无 | csrf_token、expires_at |
| GET /accounts | 无 | 掩码地址、状态、同步时间 |
| POST /accounts | provider=qq,address,authorization_code,initial_scan | 201 Account；密钥立即入安全库 |
| PUT /accounts/{id}/credential | authorization_code | 200；不回显 |
| POST /accounts/{id}/test | 无 | 202 job_id |
| PATCH /accounts/{id} | expected_version,paused? | Account |
| DELETE /accounts/{id} | expected_version；保留任务 | 202 清理 job |
| POST /sync/trigger | account_id | 202 sync_run_id；并发合并 |
| GET /jobs/{id} | 无 | queued/running/succeeded/failed/interrupted,progress,error? |
| POST /emails/import | multipart file | 202 job_id |
| GET /emails | state?,account_id?,cursor? | 摘要页，无完整正文 |
| GET /emails/{id} | 无 | 来源、证据片段、解析结果、可用链接 |
| POST /emails/{id}/reparse | mode=rules/ai,expected_version | 202 job_id；AI 需已启用 |
| GET /reviews | state=open,cursor? | 审核列表 |
| POST /emails/{id}/review | expected_version,decisions[] | 事件、任务 IDs、new_version |
| GET /tasks | view?,status?,from?,to?,cursor? | TaskSummary[] |
| POST /tasks | TaskCreate | 201 TaskDetail |
| GET /tasks/{id} | 无 | TaskDetail + 提醒摘要 |
| PATCH /tasks/{id} | expected_version,changes | TaskDetail |
| POST /tasks/{id}/complete,cancel,reopen | expected_version | TaskDetail；路径为三个独立动作 |
| POST /reminders/{id}/snooze | expected_version,minutes | 新计划与旧 suppressed |
| GET/POST /applications | 查询 / company,position?,stage | 列表 / 201 Application |
| PATCH /applications/{id} | expected_version,changes | Application |
| POST /applications/{id}/links，P1 | event_ids,expected_version | 关联结果 |
| POST /events/{id}/reschedule，P1 | task_id,new_temporal,expected_task_version | 更新任务、提醒摘要 |
| GET /calendar，P1 | from,to,timezone | entries/conflicts/uncertain_entries |
| GET /notifications | state?,cursor? | 应用内结果列表 |
| POST /notifications/test | channel | 202 job_id |
| POST /notifications/{id}/dismiss | expected_version | 关闭卡片 |
| GET/PATCH /settings | 无 / expected_version,changes | 非敏感设置 |
| GET /diagnostics | 无 | Worker/邮箱/通知/数据健康 |
| POST /exports | format=csv/json,entities[] | 202 job_id |
| POST /backups | include_evidence=false | 202 job_id |
| GET /artifacts/{id} | 无 | 认证下载流，过期 404 |
| POST /restores/validate | multipart backup | 202 job_id；不替换数据 |
| POST /restores/{id}/apply | confirmation_token | 202；进入独占恢复 |
| POST /privacy/purge | scope,preview_token | 202；清理见 M13 |

需补充的维护端点：`POST /privacy/preview` 接受 scope 返回数量与短期 preview_token；`GET /restores/{id}` 返回校验摘要与 confirmation_token。确认 token 绑定内容哈希、当前数据库版本和 10 分钟有效期，不代替 CSRF。

## 3. 具体交互示例

```json
{
  "expected_version": 3,
  "changes": {
    "temporal": {
      "kind": "absolute", "raw_text": "用户确认：2026-11-12 14:00 北京时间",
      "timezone": "Asia/Shanghai", "timezone_source": "user_confirmed",
      "starts_at": "2026-11-12T06:00:00Z", "precision": "minute",
      "needs_confirmation": false
    }
  }
}
```

这是 PATCH tasks 请求。成功返回 version=4、重算后的 next_reminder_at 与被抑制的旧计划数；若当前版本为 4，则返回 409 VERSION_CONFLICT 和 current_version，前端重新取详情，不能自动覆盖。

## 4. 任务、进度与客户端恢复

job ID 在 sync_runs、parse_runs 或 maintenance_jobs 中有持久化实体；统一接口做类型映射，不只在内存保存。进度为 phase、completed、total?，未知总数不用假百分比。服务重启后查询能返回 interrupted 或续跑状态。

前端写请求响应丢失时先以同幂等 key 重试；对带 expected_version 的修改先读最新详情，只有内容一致才视为完成。AI/邮箱/备份使用 202，不能长期占用浏览器请求等待。

## 5. 验收

OpenAPI 与前端类型一致；分页稳定；重复创建不增实体；非法状态/日期精度组合被拒绝；所有敏感读写均有会话保护。任务版本冲突、账户认证失败、备份过大有不同错误码与用户行动建议。健康端点不能泄漏数据目录或邮箱地址。
