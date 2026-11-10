# P1 事件管理本机验收

日期：2026-11-10，版本 0.2.0，schema 0004_event_details，rules-v2。用户授权“先完成事件管理”。以下证据仅代表本机工程验收，不替代 [P0 正式待验项目](p0-acceptance.md)。

| 要求 | 证据与结果 |
| --- | --- |
| 识别日期时间可填入 | review.test.ts 候选预填/多候选不擅自选择；浏览器导入合成 labeled 邮件，日期 2030-01-02、15:00–16:00 和地点自动填入 |
| 修改后实际保存 | 浏览器把时刻改为 15:15、地点改为示例路 100 号 3 楼，确认后从已保存事件重新打开，值一致且提醒按新时刻安排 |
| 缺年份候选选择 | 浏览器导入 yearless 邮件，2026/2027 两候选；点击 2026 候选后实际输入值为 2026-11-12 和 14:30，再保存成功 |
| 地点及会议链接 | test_events.py 保存/编辑/备份往返、非法链接、CSV 公式保护；标签前后顺序均有回归 |
| 改期关联原记录 | 浏览器显式选择 2030-01-02 原事件，确认比较后变为 2030-01-03 16:00，地点/链接保留，列表仍共 2 项（另 1 项为缺年份测试），无重复 |
| 取消、版本与事务 | 自动测试覆盖 stale 版本、跨邮件/普通候选拒绝、取消、提醒失效、幂等重放与重建失败全回滚 |
| 重解析保护 | 独立审核复现旧 accepted 重复事件和 ignored 复活，新增失败回归后修复；同封另一独立同类动作仍保留 |
| 周日历 | 跨日、地区午夜、相邻半开边界、确实重叠、未知结束可能冲突、仅日期不虚构冲突、无效范围/地区测试；浏览器日期跳转及点击编辑成功 |
| 旧版数据升级 | 0003 live 数据升级保持任务 ID、版本、时刻和提醒原样，保存升级前快照；0001/0002/0003 备份恢复通过 |
| 界面检查 | 1280 与 640 像素截图检查；640 窗口 content/viewport 均 640；Escape 关闭编辑可用，浏览器 error 0；完整键盘/缩放验收尚未完成 |

## 最终命令

- `.venv/Scripts/python.exe -m pytest backend/tests scripts/test_runtime.py -q`：130 passed，58.03 秒；一条 Starlette/httpx 弃用提示。
- `.venv/Scripts/python.exe -m ruff check backend scripts`：All checks passed。
- frontend 下 `npm test -- --maxWorkers 1`：22 passed；`npm run build`：vue-tsc/Vite 成功，最终 index-qHkhL2Q6.js。
- PyInstaller / Inno Setup 生成 0.2.0 独立程序和安装器；最终哈希及程序冒烟结果见 [实施日志](implementation-log.md) 的 P1 节。
- `scripts/smoke_windows.py` 使用 `.runtime/p1-packaged-final`：验证启动、界面、登录、任务、排队导入、独立解析、地点/区间、日历、原 ID 改期、诊断、备份恢复、会话重置。

浏览器会话 offerpilot-p1-events；独立数据 .runtime/p1-browser-qa。页面快照/截图在 .playwright-cli 和 output/playwright/p1-calendar*.png，均被 Git 忽略。没有读取真实邮箱/凭据，也没有覆盖或关闭用户要求保留的安装。新安装器尚未在干净 Windows 安装验收；未自动升级用户安装。
