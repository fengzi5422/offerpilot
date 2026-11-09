# M07 任务与日历详细设计

状态：待确认｜P0：任务；P1：周日历/冲突｜需求：F05/F07/F14、A2/A7

## 1. 模型与边界

拟建 `domain/{task,calendar}.py`、`services/{tasks,calendar}.py`。任务类型 `assessment/written_exam/interview/reply/materials/offer/custom`；状态 `pending/in_progress/done/cancelled`。priority 为 1 高、2 中、3 普通；用户优先级可覆盖推荐值。

TaskCreate：title 1–200 字、kind、temporal、priority、application_id?、notes?（最多 4000 字）、reminder_offsets?。手动任务允许无公司、无邮件、无时间。TaskPatch 只提交变更字段，另带 expected_version；event_id/action_key 不接受普通 PATCH 修改。

## 2. 状态与编辑语义

pending 与 in_progress 可互转，二者可完成或取消；done/cancelled 仅通过 reopen 显式重开。逾期为视图派生，不作为状态。终结任务即刻抑制未发提醒，保留历史结果。

每次手动编辑将字段名加入 manual_fields，记录 actor=user。用户可“恢复自动建议”，只移除指定字段保护并进入差异确认，不立即采用未经确认的模型值。

时间修改或提醒模板修改增加 schedule_revision，事务重算提醒；只改标题不增加排程版本，通知发送时读取最新标题。重新打开任务只生成未来计划，过去计划进入历史，不追发。标题/备注不做自动截断保存，超限返回字段错误。

## 3. 时间与视图规则

- due_at 用于行动截止，starts_at/ends_at 用于日程。面试的提醒基准为 starts_at，回复/测评/材料为 due_at。
- 仅日期存 due_date；显示“具体时间待确认”，不按 due_at=null 判为无截止。
- 精确截止过去且未完成为 overdue；日期型在用户时区日期超过 due_date 后显示“日期已过，截止时刻未确认”，不声称确定延误了多少小时。
- 今天/未来七天按用户显示时区的本地日界线转换成 UTC 区间查询。显示时区改变不改变已有绝对时刻；日期型任务仍保持原始日期/来源时区并提示差异。
- 列表排序：逾期/今天/未来/无时间分组，组内 priority、时间、created_at、id 稳定排序。
- 延后提醒只改变选定提醒，不能改变企业截止。调整截止使用“修改时间”独立动作，UI 明确标识。

## 4. 任务服务合同

`create(command, request_key) → TaskDetail`；`patch(id, expected_version, changes) → TaskDetail`；`complete/cancel/reopen` 与 patch 共用事务和版本校验；`list(filter, cursor, limit) → Page`。

详情返回来源主题、发件人、Date/INTERNALDATE、证据、解析来源、人工字段、最近提醒结果、下一提醒时间。来源被清除时显示不可用，任务仍可使用。

## 5. 日历与冲突（P1）

周视图返回范围内 starts_at 日程和 due_at 截止点；日期型显示在待确认/日期行，不伪造全天占用。冲突计算用半开区间 `[start,end)`，端点接触不冲突，只计算 pending/in_progress 且 confirmed 的日程。

未知结束时间不臆测 1 小时：开始点落入其他已知区间时标“可能冲突”，两未知时长日程同一开始时刻标“可能冲突”；其他未知情况显示“时长未知，无法完整检测”。已知完整区间交叠才标确定冲突。准备时间是用户独立 custom 任务，不修改企业日程。

`GET calendar` 最大 93 天；返回 entries[]、conflicts[]、uncertain_entries[]，支持用户时区参数。拖动企业日程需打开时间修改确认，不允许无提示移动。

## 6. 验收

无邮件手动任务可创建/重启恢复；完成、取消后无新发送；编辑时间旧提醒失效；重复提交返回同任务。今天视图跨 UTC 零点仍正确。相邻区间不冲突、真实交叠能发现、未知时长不作为确定冲突。关闭网络不影响上述操作。
