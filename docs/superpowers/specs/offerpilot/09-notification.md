# M09 通知通道详细设计

状态：待确认｜P0：Windows/应用内；P1：移动/Webhook｜需求：F06/F15、A10

## 1. 合同和边界

拟建 `adapters/notification/{base,windows,in_app,webhook}.py`。webhook 在 P1 才实施。

`send(NotificationMessage, DeliveryContext) → DeliveryResult`。Message 包含 notification_id、task_id?、title、短说明、display_time、local_route；不包含完整邮件、授权码和会议 Token。Context 包含 idempotency_key、deadline、attempt_no。Result 为 `accepted/rejected_transient/rejected_permanent/unknown`，附 error_code、provider_ref?、can_retry。

适配器只发送一次，不自行循环重试；由 M08 决定重试。测试通知使用固定示例文本，不使用真实招聘信息。health 输出 available/disabled/unknown 和可操作建议，不假定能可靠读取所有 Windows 通知设置。

## 2. Windows 实现

使用 WinRT Toast 适配，通过经打包验证的 Python 包封装，候选 windows-toasts；最终依赖在 M1 技术验证后锁定。应用身份固定 `OfferPilot.Lite`，安装器创建携带同一 AUMID 的开始菜单快捷方式。此依赖需验证，不能只在开发终端弹出一条通知就算安装验收。依据：[Microsoft 桌面 Toast 身份说明](https://learn.microsoft.com/en-us/windows/win32/shell/enable-desktop-toast-with-appusermodelid)。

通知 Tag/Group 使用稳定 reminder 标识的受限长度编码，同一逻辑通知可以替换系统卡片；该机制不能保证用户从未见过重复弹窗。标题最多 80 字，正文最多 200 字，XML 转义，默认隐私模式只显示“求职事项提醒”和时间。

P0 通知点击只在运行中通过受控回调打开本地任务路由，不把会话 token 放入通知。进程退出后不依赖旧 localhost 端口；无法激活时用户从开始菜单打开应用查看历史。P1 再评估协议/COM 激活，不能把远程输入拼成 shell 命令。

发送 accepted 仅表示系统 API 接受；用户可能关闭横幅、处于勿扰或未看到。测试页必须询问“是否看到了通知”，保存用户报告与最后测试时间；用户报告也不成为未来必达保证。

## 3. 应用内通知

每次提醒最终结果都产生或更新 notification_items，即使 Windows 失败也可在 UI 查看。提醒计划还未触发时不标“已提醒”。dismissed 只表示关闭应用内卡片，不完成任务、不取消未来提醒。无通知权限时首页提供持续可见状态入口。

## 4. 外部渠道（P1）

先交付自定义 HTTPS Webhook，或在用户所在网络验证后选一个手机渠道；具体供应商和是否纳入首轮 P1 须单独确认。Webhook JSON：`id, title, body, event_time, task_label`，不含不可从手机访问的 localhost 链接、凭据和完整邮件。

密钥放系统凭据库；默认不跟随重定向；端点只允许 https，默认拒绝 loopback、link-local、私有网段和云元数据地址；发送前解析并校验连接目标，避免 DNS 解析变化绕过限制。自建内网服务需显式高级配置。签名 HMAC-SHA256 覆盖 timestamp、notification_id 和规范化 body，服务商协议可由 adapter 替换。

2xx=accepted；明确限流/未接受可重试；认证/格式错误不可重试；请求已写出后的网络中断=unknown。通道不支持幂等时承认重复可能性，UI 提供控制而非自动无限重发。

## 5. 验收

普通用户安装后的测试通知显示正确应用名；中文、特殊字符不会破坏 XML；系统阻止通知时应用内有结果和建议。测试通知不含真实邮件数据。模拟各 Result 验证调度策略；secret、URL query、正文不得进入日志。P1 的选定通道需真实发送测试后才标可用。
