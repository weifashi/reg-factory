# 本机 8799 更新与验证 · 2026-09-30

## 授权与范围

用户明确授权“更新 并验证”。仅更新本机 reg-factory 预览，不修改远端服务、Sub2API 实例或 Google 资源，不执行绑卡、试用申请、模型调用、账号调度；没有提交或推送 Git。平台未提供适用于本机的 Project Orchestrator managed operation 工具，本次使用已有本机迁移接口和精确进程核验脚本，未声称存在 Orchestrator Run/Host 回执。

目标：`https://8799s--main--wfs--weifashi.coder.tbc.5ok.co/#google`。源码为 `/workspace/reg-factory/.worktrees/onboarding-minors`，分支 `fix/onboarding-minors`，HEAD `6b6d723df004398f9cd38e53e29adccc88362bf5` 加已验收工作树改动。保留所有既有未提交改动，不拉取或覆盖其他分支。

## 操作记录

1. 核对旧进程 PID、启动时间、工作目录、TLS、仅监听 127.0.0.1:8799、protected 模式、关闭 K12 自动启动；核对本轮 21 个已验收文件哈希。
2. 保存原命令/环境、配置/密钥目录及当前源码备份；停止精确匹配的旧进程，不终止其他服务。
3. 对已停止应用的原数据库 schema 做 custom-format pg_dump，验证归档目录并记录 SHA256；目录 0700、文件 0600。未执行实际恢复演练。
4. 调用现有 `apply_all(..., target_version=4)`，schema 2→4，每版分别提交，核对连续版本与 catalog 校验和。003 增加任务 `execution_mode`、约束及 `live_effect_journal`；004 增加 `live_run_plans`、`live_effect_observations`。未新增迁移脚本。
5. 按原命令重启，仅把 pool mode 改为 live，protected/TLS/登录保护及自动启动禁用保持。未登记来源、manager 或 mailbox_id；启动本身不调用 provider。
6. 登录前比对原表原字段逐表摘要：除 schema_migrations 以外原行不变。全部旧任务仍为 synthetic，三张 live 表为空。

旧 PID `2858915`，新 PID `4106506`；替换完成时间 `2026-09-30T02:21:10Z`。原 6 个邮箱、2 张卡、6 条任务保留；旧配置不自动改为 v2，也不升级旧账号资格。

## 验证结果

- 本机 TLS 证书验证通过，health/login 200，未登录 capability 401。
- 实际会话登录后，原首页、邮箱页、原诊断/受保护配置编辑器、capabilities、全局配置、邮箱列表、任务列表、详情与事件接口均 200；静态 JS 与部署源码哈希一致。
- capability 如实返回 live、source unavailable、start unavailable；无来源时运行/导入不可用。旧 v1 配置只读展示，旧任务控制按钮禁用；没有把其显示成真实执行。
- Chromium 实际访问部署服务（未 mock API），1440/375 宽度无横向溢出，手机菜单开关正常，最终页面错误 0、浏览器业务写请求 0、意外外域请求 0；每个验收登录会话退出。
- 完成后再次比对业务表：原数据不变；仅登录、读取、退出导致预期的认证会话/限流/审计写入。新 live 表仍为空。
- 外网通过系统 CA 验证 TLS，health/login 均返回 Coder 认证重定向 303，不是 502。**外网仍要求先登录 Coder；未证明匿名外网应用返回 200，也未修改端口共享权限。**

最终证据目录：`/workspace/gcloud/.local/preview-launch/live-web-20260930/`。

| 证据 | 内容 |
|---|---|
| `prepared.json`、`backed-up.json`、`migrated.json`、`started.json`、`complete.json` | 一次升级的目标、代码哈希、备份、迁移、重启和启动不写业务数据证据 |
| `smoke.json`、`smoke.log`、`browser-network.json` | 最终登录/API/浏览器/网关检查；最终启动请求在约 1.53 秒内完成配置读取 |
| `google-live-1440-verified.png`、`google-live-375-verified.png` | 实际服务截图，已人工查看；375 截图等侧栏退出视口后拍摄，不截动画中间态 |
| `mailboxes-live-1440.png`、`settings-live-1440.png` | 原项目邮箱和配置页面 |

保留失败检查而不计通过：首次公网检查的 bundled CA 不认识本机信任链，改用系统 CA 且未跳过 TLS；手机检查脚本的字符串 wait_for_function 被 CSP 拒绝，改用 Playwright 原生视口断言，未放宽 CSP；另有一次默认 5 秒页面启动断言超时，未改业务代码，补请求时序记录并在明确 15 秒上界下完整重跑通过。该单次超时根因未确定，不宣称性能问题已修复。

独立代理只读核对了阶段记录、21 个源码哈希、备份权限/完整性；最终结果由主代理读取原输出确认。

## 回退与未完成项

私有备份位置：`/workspace/reg-factory/.local/onboarding-p1b/preview-20260928/upgrade-live-web-20260930/`。含数据库归档、旧启动环境、配置/密钥及源码；不得公开或上传这些文件。

回退先确认当前 PID/启动时间并停止接收请求，再根据原环境切回已验证的兼容模式、重启并重新验收。当前未生成 v2 配置或 live 任务，但未来一旦有新数据，不能直接切换并假定旧模式能写。代码回退不等于数据库降级；若需要回到 schema 2，必须先停止服务、明确确认恢复范围并使用一致备份，不删 live 记录/租约强行降级，不将 live 标为 synthetic。迁移结果未知时应先核对版本和校验和，不能盲重试。此次未执行回退。

**本次只证明预览升级与页面/API 读取链生效。** 仍无真实邮箱来源/绑定及 v2 结算配置，不能运行；真实卡池读写/支付未接入，旧 2 张卡仅保留数据。首次注册、验证码、赠金、批量执行和真实 Google→Vertex→Sub2API 全链路验收仍未完成，不能作为整体上线交付。
