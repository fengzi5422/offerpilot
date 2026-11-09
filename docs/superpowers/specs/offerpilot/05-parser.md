# M05 事件、时间与可选 AI 解析详细设计

状态：待确认｜P0：规则；P1：AI｜需求：F04/F07/F08/F11、A2/A3/A8

## 1. 模块职责

拟建 `domain/{events,temporal,evidence}.py`、`adapters/parsing/{rules,time_parser}.py`、`adapters/ai/{base,compatible_http,redaction}.py`、`services/parsing.py`。

`parse(mail, classification, context) → ParseResult{events[], issues[], source, parser_version}`。context 包含邮件时刻、用户时区和规则版本；禁止读取当前日期来无提示地补年份。解析器只产生草稿；持久化和任务创建由 M06 用例执行。

## 2. 公共数据合同

| 对象 | 字段与约束 |
| --- | --- |
| EventDraft | draft_key、type、company?、position?、actions[]、evidence[]、issues[] |
| ActionDraft | draft_key、kind、title、temporal、links[]、evidence[]；每个动作独立时间 |
| Temporal | kind=absolute/relative/date_only/unknown；raw_text；timezone?；timezone_source=explicit/user_confirmed/account_default/unknown；local_date?；starts_at?；ends_at?；due_at?；precision=minute/date/unknown；reference_kind?；reference_at?；candidates[]；needs_confirmation；reason_codes[] |
| Evidence | message_id、text_hash、segment_id、start、end、quote、field |
| Issue | code、field_path、message、severity=review/error |

Evidence 的 start/end 为规范化文本 Unicode code point 偏移、end exclusive。前端不直接当作 JavaScript UTF-16 下标；后端返回已切分高亮片段，校验 quote 必须与该位置一致。文本被清理后保留 quote 和 evidence_unavailable 状态，不能伪称可还原全文。

示例：

```json
{
  "events": [{
    "draft_key": "e1", "type": "interview", "company": "示例科技",
    "position": "后端工程师",
    "actions": [
      {"draft_key": "a1", "kind": "reply", "title": "确认面试出席", "temporal": {"kind": "absolute", "raw_text": "2026年11月11日18:00前（北京时间）", "timezone": "Asia/Shanghai", "timezone_source": "explicit", "due_at": "2026-11-11T10:00:00Z", "precision": "minute", "needs_confirmation": false}},
      {"draft_key": "a2", "kind": "interview", "title": "参加技术面试", "temporal": {"kind": "absolute", "raw_text": "2026年11月12日14:00（北京时间）", "timezone": "Asia/Shanghai", "timezone_source": "explicit", "starts_at": "2026-11-12T06:00:00Z", "precision": "minute", "needs_confirmation": false}}
    ],
    "issues": []
  }],
  "source": "rules", "parser_version": "rules-v1"
}
```

示例省略 evidence 内容以免把示意偏移当真实证据；生产草稿每个关键字段必须携带合法证据或明确标记 user_input。

## 3. 时间决策表

| 原文 | 保存与展示 | 自动排程 |
| --- | --- | --- |
| 完整年月日+时刻+北京时间 | 确定 UTC，保留原文/时区 | 可以 |
| 完整日期时刻，无时区 | 按账户默认时区提供候选，timezone_source=account_default | 首版要求确认 |
| 11月12日，无年份 | 以邮件 Date 年份生成候选，跨年冲突列候选 | 不可以 |
| 2026年11月12日前，无时刻 | local_date，precision=date，due_at=null | 不可以，待确认具体截止 |
| 明天/本周五 | 依据邮件 Date 和来源时区计算候选，解释周定义为周一开始 | 不可以 |
| 收到后48小时 | 展示 INTERNALDATE 与 Date 候选及转发风险 | 用户选择参照后才可以 |
| 48小时内，无参照 | unknown + REFERENCE_MISSING | 不可以 |
| 明确时间区间 | starts_at/ends_at，验证结束晚于开始 | 确认后可以 |
| 过去的精确截止 | 保存已过期任务并提示，不自动挪到未来 | 不发历史模板提醒 |

夏令时不存在的本地时间必须纠正；重复时刻必须选择 offset/fold。只给开始、不写结束时，ends_at=null，不自动猜面试时长。北京时间映射 Asia/Shanghai，含歧义缩写如 CST 不直接确定。

所有确定结果通过确定性日期解析和时区往返校验；AI 返回 UTC 不可绕过此检查。Windows 安装包包含 tzdata，因为部分 Windows 环境没有系统 IANA 数据库，参见 [Python zoneinfo 文档](https://docs.python.org/3.12/library/zoneinfo.html)。

## 4. 自动创建与人工门槛

自动创建要求：招聘动作明确；公司已提取或用户已关联申请；关键证据存在；动作匹配无冲突；时间需排程时已精确确认；不是改期/取消；不与人工字段冲突。无时刻但明确需要行动可以保存为审核草稿，不悄悄生成带时间提醒。

投递确认/普通进度只记录事件；同封邮件的多个动作分别创建。申请名称缺失可由审核用户补全，也可选择“暂不关联公司”建立手动任务，这一决定须留审计来源。

## 5. AI 可选路径（P1）

默认关闭；用户确认数据发送范围、端点与费用后开启。仅规则不完整或用户主动重新解析触发，已充分且可靠的规则结果无需模型。首版接口合同为单次无工具文本提取，可用兼容 HTTP 服务；不依赖服务商特定工具调用。

出站仅主题、必要正文段、占位符映射后的姓名/邮箱和无 Token 的链接域名；原链接留本机。秘密字段在构造模型请求前白名单筛选。模型没有文件、浏览器、系统工具权限，邮件被放在明确的数据字段；模型指令与邮件内容隔离。

输出限制：最多 10 个事件、每事件 10 个动作、总 JSON 64 KiB；额外字段拒绝；必须能定位原始证据；company/日期等不能凭模型常识补齐。schema 合法后仍运行相同时间/业务检查，confidence 只作参考，不作为单独通过门槛。

超时 30 秒、并发 1；429/网络错误本次回退规则，最多一次后台延迟重试；余额/认证错误停止模型重试并显示设置入口。连续 3 次错误暂停自动 AI 15 分钟。已确认任务保持原值。用户关闭 AI 后不再出站，未发请求取消，已返回结果也仅作草稿。

## 6. 验收

每个时间决策表案例均有固定时钟的测试；同封回复截止与面试开始不得串用。Date、INTERNALDATE、同步时间不同的样本必须显示参照。非法 JSON、恶意指令、捏造证据、巨大输出、超时和无余额均保持规则路径可用。模型返回不同日期不会覆盖用户确认的日期。
