# 原项目入口与能力复用调查（2026-09-28）

只读研究基线 main 6b6d723、worktree HEAD 9ee49e1；以下是调查与后续方案，不是已完成整合。未读取真实邮箱/代理秘密、未调用外部平台。产品代码应继续在 reg-factory 内建设；独立工作区不是独立产品。

## 现状
- 原一级邮箱池：`webui/static/index.html:42,347–363`；只有文本导入/重复跳过/总数，没有统一管理表。`app.js:193–224` 负责页面切换。
- 旧 `/api/mailpool`：`webui/server.py:2288–2363`，读写明文 emails.txt；新 `/api/onboarding/mailboxes` 使用 registry/platform-state SQL 表，不是同一数据源。
- 现有格式解析已通过 `onboarding/legacy_mailboxes.py` 复用 `common.account_records`。
- 原代理页：`index.html:546–620`，`app.js:277–405`；Clash/固定/住宅代理与轮换、出口测试已有。`common/proxy_switch.py:98–156` 可给子进程和浏览器 profile 配代理；`common/concurrency.py:90–147` 已处理部分共享出口并发规则。
- `server.py:3090–3140` 只验证出口IP，不证明所谓“干净”信誉或赠金资格；Google 尚未进入 `proxy_switch.py:56` 的平台枚举。
- Outlook 收信 `common/mailbox.py`、其他渠道 `common/temp_email.py` 可选择适用适配，但不能当作任意邮箱均可自动收码。

## 不可直接解除的隔离
protected 根路由选 onboarding.html（server.py:2723–2727），旧handler与middleware双层拒绝旧执行。旧 app.js 在初始化时调用 scripts/status/custom SMS（2747–2753），旧 index 还有 inline style；直接切回旧index或加载整个app.js会触发拒绝，放开旧路由/CSP则引入安全倒退。

## 业务规则冲突
1. `common/emails.py:77–91,115–116` 实际读写全局 outlook_registration_emails.txt，不能凭“平台独立”注释断言跨平台复用已正确实现。
2. 旧文件锁 `emails.txt.<platform>.reserve`（:98）不是跨进程跨平台统一邮箱锁；新SQL hold与旧消费者不自动互通。
3. 不把邮箱密码等同所有平台密码；新解析区分 password/account_password，Google凭据复用须明确定义适配，不能破坏其他平台规则。
4. 原代理查询可能回显认证秘密；复用底层工具，不直接公开原 `/api/proxy`，应通过受保护/脱敏/审计接口封装。

## 后续最小整合顺序（待页面评审和实现）
A. 原项目导航结构：复用布局/样式，protected采用安全壳，不载入旧启动脚本；一级邮箱池，二级列表/导入/使用记录，平台筛选；信用卡池并列；Google开通任务入口；底座诊断下移。保留当前新API与已实现列表代码。仅完成A不得声称旧数据和执行器统一。
B. 统一邮箱源：解析旧记录、预览/核清历史；建立领取/回写适配，逐个切换旧消费者；全部切换前不得开放真实跨平台并发，避免两套记录与占用冲突。
C. 复用网络出口工具：任务冻结代理配置，浏览器与gcloud使用同一出口；不在执行途中轮换全局Clash；UI称“出口可达/当前IP”，不称“已证明干净”。

本调查没有修改表、字段、原导航或消费者；没有迁移旧数据，也未打通真实Google/银行/Vertex/Sub2API。
