# M10 设置、安全与隐私详细设计

状态：待确认｜范围：P0，AI 设置 P1｜需求：F01/F09、G5、NFR-07

## 1. 设置归属

拟建 `config/{paths,settings}.py`、`security/{session,csrf,redaction}.py`、`adapters/secrets/windows_keyring.py`。

| 配置 | 默认值 | 保存位置/修改效果 |
| --- | --- | --- |
| display_timezone | Asia/Shanghai，首启可改 | SQLite；改变显示不改变绝对时刻 |
| sync_interval_seconds | 180，范围 120–300 | SQLite；下一轮生效 |
| initial_scan | 14 天 | 账户游标；显式回补才扩大范围 |
| AI enabled | false | SQLite；开启需记录出站说明版本和用户选择 |
| quiet_hours | disabled | SQLite；启用后重算未发送计划 |
| notification_privacy | generic | SQLite；隐藏公司/岗位详情 |
| autostart | false | SQLite + 当前用户启动项，失败需回滚设置 |
| 正文缓存 | 解析后最小化，临时缓存最多 7 天 | SQLite/受限 cache |
| 数据目录/日志等级 | 用户 LocalAppData / INFO | 启动参数；生产只允许安全路径 |

业务设置放 SQLite 以支持事务；settings.json 仅保存数据目录定位等启动级配置，不重复保存提醒模板。环境变量只供开发/部署控制路径，不作为用户凭据存储方案。

## 2. 凭据管理

`SecretStore.set/get/delete(ref)` 仅由服务层调用，生产强制 Windows 安全凭据后端。检测到明文/空后端直接失败，不降级保存文件。数据库 credential_ref 形如 `offerpilot/mail/<uuid>`。

新增账号先写凭据，再写账户；数据库失败删除刚建凭据。替换凭据用新 ref，连接测试成功后切换引用，最后删除旧 ref；任一步失败保持旧有效引用。删除账户先停同步，再删凭据并清空引用；删除失败记录可重试清理状态并显示原因。不得向前端回显授权码。

恢复备份后所有账户需重新授权，外部 AI/推送 disabled；不能仅因旧 ref 字符串还在就读取当前机器的其他凭据。

## 3. 本机 API 会话

只绑定 127.0.0.1，Host 精确匹配实际端口，不接受任意 localhost 别名或代理头。生产静态 UI 同源；不启用通配 CORS。

启动器为打开 UI 生成 256-bit 一次性随机引导码，有效期 60 秒，放 URL fragment，不在 query、访问日志或 Referer 中。页面立即移除 fragment，通过同源 POST `/api/v1/session/bootstrap` 交换 session Cookie。校验 Host、Origin、JSON Content-Type、引导码与一次性消费；失败响应不给业务信息。

Cookie HttpOnly、SameSite=Strict、Path=/，仅当前运行实例有效；本地 HTTP 无法依赖 Secure Cookie，明确其保护范围不包含已控制本机用户账户的恶意程序。会话空闲 12 小时或程序退出失效，用户从托盘重开页面重新授权。

所有业务读 API 需会话；写 API 再校验同源 Origin 与 X-CSRF-Token。CSRF token 从认证后的 session 接口取得，只在内存保存。拒绝 Origin:null、表单 Content-Type 和无 Origin 的浏览器写请求。静态资源 CSP 使用 self，禁止远程脚本、frame、object；敏感响应 Cache-Control:no-store、Referrer-Policy:no-referrer。

## 4. 输入和文件边界

API 不接受任意系统路径；导入通过上传流，导出返回服务生成的短期 artifact ID，用户用浏览器下载。服务内部文件路径由 UUID 生成并校验位于允许数据目录，拒绝路径穿越、绝对路径和 reparse point 逃逸。

普通 JSON 请求上限 1 MiB；EML 10 MiB；恢复包上限 200 MiB、解包后 1 GiB、最多 100 个文件，必须采用流式计数，不能只相信声明大小。压缩包拒绝符号链接/绝对路径/上级路径。上传内容不执行脚本或程序。

## 5. 日志与出站

日志采取字段白名单：request_id/run_id、模块、实体内部 ID、错误码、耗时、计数。禁止 headers、Cookie、authorization、完整 URL query、邮箱授权码、AI Key、正文及原始请求体。邮箱地址只显示掩码，异常栈先去除第三方响应/请求内容。具体保留和诊断导出见 M14。

AI 和外部推送分别授权；开启一项不意味着另一项获准。无遥测默认上传。用户可以查看将发出的脱敏示例，并随时关闭未来出站。

## 6. 验收

外站表单/脚本不能读取或修改任务；错误 Host、Origin、CSRF、重放引导码均失败；本机未认证请求读不到邮箱。凭据后端异常不产生明文回退。导出及日志搜索测试秘密哨兵字符串结果为零。恢复后未重新授权不会自动连接邮箱或模型。
