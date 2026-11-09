# M04 招聘邮件分类详细设计

状态：待确认｜范围：P0｜需求：F03/F08、A5

## 1. 职责与合同

输入 `NormalizedMail` 的主题、发件人、正文片段、转发标记；输出 `Classification{relevance, categories[], rule_hits[], reason_codes[], rule_version}`，不写日期、不直接生成任务。

`relevance=recruitment/non_recruitment/uncertain`。类别枚举：`application_received, assessment, written_exam, interview, attendance_confirmation, materials, reschedule, cancellation, offer, rejection, promotion, unknown`。一封邮件允许多类别。

拟建 `domain/classification.py`、`adapters/parsing/rule_classifier.py`、`resources/rules/classification.v1.json`。规则资源随应用版本管理，不从网络自动加载可执行内容。

## 2. 规则顺序

1. 人工忽略优先：正常同步不再次送审；只有用户主动“恢复并重新解析”才解除。
2. 提取显式动作和否定/变更上下文，例如“取消本次面试”优先于“面试邀请”关键词。
3. 主题、正文动作与时间上下文组合形成 strong/moderate/weak 证据；发件人域名只作辅助，不能因为域名陌生而忽略。
4. 明确招聘动作且无矛盾标 recruitment；仅公司词或“机会/职位”标 uncertain；仅明确营销且无招聘动作才标 non_recruitment。
5. 转发、正文被截断、多个矛盾时间或无法区分新邀请/改期，标 uncertain，交给解析和审核。

| 示例 | 类别 | 后续 |
| --- | --- | --- |
| 已收到您的申请，无需回复 | application_received | 事件记录，无待办 |
| 请完成测评，并上传成绩单 | assessment + materials | 两个动作候选 |
| 原定面试取消，请等待通知 | cancellation | 待确认旧任务影响 |
| 面试改到周四 | reschedule | 待确认；不建第二日程 |
| 热门职位推荐、退订 | promotion | 自动忽略，有可恢复入口 |
| 主题“通知”，正文不充分 | unknown | 待确认 |

## 3. 可解释性与误判反馈

记录命中 rule_id、文本位置、判定原因；UI 显示“规则识别”，不显示未经校准的概率。用户标“非招聘”只影响该邮件的 canonical ID，不默认封禁整个发件人。P1 可显式添加发件人规则，但应可撤销并显示作用范围。

分类结果与规则版本保存在 parse_run。规则升级不批量覆盖已确认结果；用户可选重跑未处理邮件，产生新的解析运行。已忽略/已确认邮件不因软件更新重复出现。

## 4. 失败处理与验收

规则文件 schema 错误时阻止加载新规则，保留随包的有效规则；单条匹配错误记录 rule_id 并将邮件标 uncertain。规则匹配限制输入长度，避免高复杂度正则造成 Worker 卡死。

测试样本覆盖每一类别、否定、改期、同封多动作、未知发件人和广告。至少 100 封获准脱敏样本用于阶段评估，召回目标 95% 是评估目标，非功能上线保证；报告应列样本分布、漏报数与误报数，不能用小样本宣称达标。人工忽略用例在重复同步及规则更新后仍不重复提示。
