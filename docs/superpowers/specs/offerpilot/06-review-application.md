# M06 审核与求职申请详细设计

状态：待确认｜P0：审核/手动归属；P1：阶段聚合/改期｜需求：F08/F12/F13、A4/A5

## 1. 领域与接口

拟建 `domain/{review,application}.py`、`services/{review,application,event_reconciliation}.py`。审核与事件接纳属于同一用例边界，保证“已审核”不会出现但任务没生成。

`review(message_id, expected_version, decisions[], request_key) → ReviewResult`。decision 为 `accept/edit/ignore/link_existing`，包含 event_key、选定动作、修改字段、时间参照、application_id?。接受一封邮件的多个动作在同一事务完成；每个动作可单独取消勾选，未选动作记录 declined，不会下次自动补建。

`ApplicationService.create/update/link_event/unlink_event/change_stage` 均使用 version。申请包括 company、position、batch_label?、stage、source、manual_fields、closed_at。batch_label 为 P1 可选字段，迁移时添加。

## 2. 审核原因与状态

| reason_code | 必须显示 | 可完成的决定 |
| --- | --- | --- |
| TIME_AMBIGUOUS | 原文、候选、参照、时区 | 选定或手填时间 |
| COMPANY_MISSING | 发件人、主题、证据 | 补公司/关联/暂不关联 |
| DUPLICATE_CANDIDATE | 现有任务和新草稿对比 | 关联、保持独立、忽略 |
| RESCHEDULE/CANCELLATION | 旧任务、新建议、受影响提醒 | 修改/取消既有任务或保持 |
| USER_OVERRIDE_CONFLICT | 人工值、解析建议、来源 | 保留人工值或显式替换 |
| SOURCE_TRUNCATED/INVALID | 已有片段、缺失范围 | 重取、手填、忽略 |

审核状态 `open → accepted/ignored/superseded`。新解析结果可将旧未处理建议 superseded，但不得更改已接受结果；UI 必须显示“有更新的解析结果”。提交旧 version 返回 409 并刷新差异，不能静默按新草稿提交。

人工忽略设邮件 ignored_by_user；已产生任务的邮件再忽略时默认只停止后续解析，弹出既有任务列表供选择是否取消，不能默默删除任务。

## 3. 事件接纳与重解析

首次接受建立稳定 event_key/action_key。再次运行按事件类型、动作类型、证据片段和现有关系生成对齐建议；数组顺序变化不产生新身份。找不到唯一对应关系时进入审核。新增动作只能经用户确认或满足首次自动接纳规则，不能因 parser_version 变化重建所有任务。

同一 request_key 重试返回同一结果。审核事务包含：版本校验、事件写入、字段覆盖保护、任务/提醒建立或更新、review 关闭、审计和幂等命令结果。任意失败全部回滚。

## 4. 申请阶段（P1）

枚举：`applied/assessment/written_exam/interview/awaiting_result/offer/closed`，允许任意用户明确跳转和重开。自动事件只给阶段建议；用户接受后写 application_history，记录 old/new、事件、actor、时间。拒信建议 closed，不删除未关联任务；终止申请时列出相关未完成任务让用户统一取消。

关联候选顺序：用户指定关系 > 同线程且公司/岗位一致 > 公司+岗位+招聘批次完全匹配 > 模糊候选。公司名相同不足以自动合并；同公司不同岗位、年度校招、实习与正式岗位分开。手动拆分将选定事件与其任务一起迁移，保留稳定任务 ID、提醒计划和历史。

## 5. 改期/取消流程

P0：送审，显示候选旧任务；用户通过编辑/取消任务完成处理，再标记该邮件已处理。不得创建带未来提醒的新日程来“代替”旧日程。

P1：确认窗口列旧时间、新时间、来源、旧提醒和新计划。用户选择原任务并确认后：锁定该任务、校验版本、修改 temporal、schedule_revision+1、抑制旧计划、创建未来有效计划、关联新事件、关闭审核，同事务提交。cancel 分支只抑制提醒与取消任务。

若旧通知已经交给系统，界面说明无法撤回用户已见内容，尝试移除系统通知卡片；不能宣称外部副作用已回滚。提交期间再收到改期邮件，保留下一条审核，不能用到达顺序直接覆盖人工决定。

## 6. 验收

重复提交审核一次有效；广告忽略后不重现；重新解析保留人工字段；同公司两岗位不混并；拆分保持任务 ID。改期事务中任意位置故障不留下新旧两套可执行提醒。用户明确保留旧时间时不被新解析改掉。
