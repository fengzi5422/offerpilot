# M03 邮箱接入与邮件规范化详细设计

状态：待确认｜P0：QQ/EML；P1：更多邮箱｜需求：F01/F02/F07/F16、A1/A6

## 1. 边界与接口

模块只连接、读取和保存邮件，不创建任务。拟建 `adapters/mail/{base,qq_imap,mime}.py`、`services/mail_sync.py`、`workers/mail_sync.py`、`services/eml_import.py`。

| 接口 | 输入 | 输出 |
| --- | --- | --- |
| test_connection | account_id、取消信号、30 秒期限 | ok、error_code、capabilities、可执行建议 |
| inspect_folder | account_id、INBOX | uidvalidity、uidnext、只读状态 |
| list_uids | folder、min_uid、日期范围/上界 | 排序后的 UID 页，每页最多 100 |
| fetch_message | uid、正文上限 | headers、INTERNALDATE、MIME body sections、附件元信息 |
| import_eml | 文件字节流，最大 10 MiB | job_id，最终 canonical message_id |
| sync | account_id、reason=manual/timer/recovery | sync_run_id；已运行则返回现有 ID |

QQ 候选配置为 IMAPS `imap.qq.com:993`，强制证书验证，不降级明文；实际账号接入在开发验收中验证。只读打开 INBOX，使用 UID 和 BODY.PEEK；不 STORE、不 EXPUNGE、不发邮件。日志屏蔽协议认证和正文。

## 2. 首次与增量同步

首次范围默认近 14 天，可选 30 天或从现在开始。记录首次扫描上界 `UIDNEXT-1`，防止持续来信导致历史扫描永不结束。日期搜索只用于缩小候选，再按 INTERNALDATE 校验；服务器日期搜索粒度不等于精确 UTC 过滤。

“从现在开始”在用户点击开始并读取 UIDNEXT 时建立边界；初始 last_uid=UIDNEXT-1，此后的新 UID 才抓取。明确展示这一开始时间。

每轮流程：

1. 领取账户同步锁，读取游标，只读检查 UIDVALIDITY。
2. 相同生命周期，查询大于 last_uid 的 UID；再次在客户端过滤，避免 IMAP 范围尾端特殊语义返回旧 UID。
3. 按 UID 升序，每批最多 50 条、先取 headers/BODYSTRUCTURE，再取文本部分；未知邮件也抓取有限正文，不以未命中主题关键词直接丢弃。
4. 正文规范化，计算指纹；事务写邮件、位置与 new 状态。
5. 批次全成功才推进到本批最大 UID。单封永久内容错误以持久化失败占位保存，后续可重取；网络错误不记永久失败、不跨越未落库 UID。
6. 继续后续批次；人工暂停在批次边界生效，已提交数据不回滚。
7. 完成时记录 last_sync、next_sync 与统计，不等待解析完成。

UIDVALIDITY 改变：保留旧位置与任务，标记重建；从账户最初 scan_from 边界重新枚举，进行内容匹配，不复用旧 UID。重建较大时分页展示进度，不能直接只重扫近 14 天造成旧未处理范围漏失。账户“从现在开始”的边界保持其原始时间。

IMAP 的 UID、UIDVALIDITY 与 PEEK 语义以 [RFC 9051](https://www.rfc-editor.org/rfc/rfc9051.html) 为协议参考；QQ 的实际兼容能力须通过适配器测试，不能据此假定服务器支持 IMAP4rev2 全部功能。

## 3. MIME 与证据输入

- 使用 email policy 解析 RFC 编码、multipart/alternative、charset；优先 text/plain，否则 HTML 转纯文本。
- 不渲染原 HTML；去除 script/style/form/iframe，绝不加载远程图片、附件或追踪像素。
- 每封最多提取 256 KiB 解码文本、100 个链接、100 条附件元信息。截断加 `text_truncated=true` 并转审核，不将截断结果标为完整解析。
- 默认只保留必要正文片段及证据，解析时临时全文置内存；持续重解析使用保存片段，需全文时由用户触发重新读取。未完成解析的受限文本暂存 cache，成功处理后清理，最多保留 7 天。
- 提取主题、发件人、Date、INTERNALDATE、first_seen_at、References/In-Reply-To、显示文本与真实链接域名。三种时间分别保存，不将同步时刻当收到时刻。
- 只允许 http/https 链接供用户主动打开；javascript/file/data 链接显示为普通文本。URL query 可能含 Token，只在本机受保护详情中按需展示，不写日志/通知/AI。
- 附件只读取名称、类型、声明大小；不保存附件内容。EML 的原始字节仅用于导入解析和指纹，默认不长期保存。

## 4. 状态、重试与移除

账户状态：`unconfigured/ready/syncing/paused/auth_required/network_error/removed`。网络超时按 30/60/120/300 秒加 0–20% 抖动重试；认证失败停止自动重试，等待重新授权；服务器限流尊重可用提示，最长退避 30 分钟。手动同步并发合并，10 秒内重复点击返回原 job。

删除邮箱立即暂停后续同步并删除凭据；默认保留任务与最小来源，邮件缓存由独立选项清理。关闭 AI、暂停邮箱不影响本地任务提醒。移除过程中正在执行的批次可完成保存，但禁止启动下一批。

## 5. P1 扩展

163/126 增加 provider 配置和能力测试，复用同步合同；Gmail/Outlook OAuth 另建 token 刷新与授权适配，凭据仍放系统库。多邮箱每账户独立游标/锁；不要跨账户强制按 Message-ID 合并。

## 6. 验收

同一批抓取三次不增任务；UID 乱序/缺号不跳漏；批次中断游标不越过未落库项；UIDVALIDITY 变化不重复建业务。EML 重复导入返回现有邮件；乱码、HTML、转发、多动作、10 MiB 以上文件有确定处理结果。模拟认证失效显示更新授权码入口；网络恢复续扫且旧任务可正常编辑。
