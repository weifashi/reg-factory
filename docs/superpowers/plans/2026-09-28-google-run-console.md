# Google 统一运行入口与右侧日志 Implementation Plan

> **For agentic workers:** Use subagent-driven-development with isolated file ownership. 用户已批准原项目运行入口及右侧日志整合；不得提交、推送或扩大真实执行权限。

**Goal:** 将 Google 模拟任务变为「填写参数 → 运行 → 内部预检 → 确认 → 创建 → 右侧状态/持久事件」，保留原项目任务库、认证与资源规则。

**Architecture:** 复用统一 shell、现有 batch API、共享 RFPoolRuntime 及任务命令；增加只读、owner-scoped 的安全事件投影，使用原数据库审计而非编造 worker 日志。真实执行模式明确不可用；当前模拟模式仅处理任务编排，不声称完成注册/绑卡。无新表字段、迁移、provider/付款/密钥操作。

**Tech Stack:** FastAPI / PostgreSQL / vanilla JS / Playwright / unittest。

## Contracts
- 新主按钮 `#run-google`，模式 `#google-execution-mode`（synthetic 可用，real 禁用）；原 `#batch-form`、`#preflight`、`#confirm-batch` 保留在可折叠高级检查区兼容既有诊断。初次 run 预检通过才确认写入，取消确认不写入。
- 新右侧 `#google-log`、`#google-log-state`、`#google-run-status`、`#refresh-google-log`；`#task-history` 包含原任务列表/筛选；既有详情、暂停/取消/核验按钮保留。用户输入通过 textContent 渲染。
- GET `/api/onboarding/tasks/{uuid}/events`（onboarding:read，受保护 pool-ready）：返回 `{task_id, mode:'synthetic', execution_connected:false, events:[{id:decimal-string, occurred_at:ISO-string, action:'task.create'|'task.pause'|'task.cancel'|'task.recheck', outcome:code, version:positive-int|null}], has_more:boolean}`。至多最近 100 条，按 id 升序；有更早事件则说明，不假装完整。只投影上述四种 allowlisted audit 事件，跨 owner 拒绝，无原始 audit summary、邮件、密钥、秘密正文或任意外部输出。
- run 按钮内部复用现有预检、固定 N/CAS/幂等/未决回执保护；结果未知不自动重试。接受批次后选中首任务并读取日志，接受后的读取失败不标成写入未知。
- 日志分开标记“本页操作”和“服务端记录”；真实执行器未接入始终显式。刷新只读，无自动执行命令；旧原任务不因本轮获得执行权限。

## Steps
- [x] T1 后端：先失败测试，添加安全事件投影及准确路由权限，覆盖 ownership / 参数 / 上限 / 字段脱敏 / 非业务副作用。
- [x] T2 UI：在原双栏布局里突出运行入口与黑底日志，历史折叠；保留所有既有 DOM id 和其他模块，移动端不溢出。
- [x] T3 orchestration：先失败浏览器测试，运行→预检→确认→创建→选中→日志；不足/取消/401/未知结果/晚回包/重复点击/输入变化覆盖。
- [x] T4 回归：按冻结最终代码顺序运行新增专项、现有 pool/card/unified/console、auth 路由；不把 fixture 超时或跳过视为通过。
- [x] T5 预览：仅重启本机原预览服务使新路由生效，保留账号/数据/权限；真实浏览器验证页面与现有模拟日志，避免消耗用户留下的预检邮箱。保存截图与证据，汇报未接通的真实执行边界。

## 本轮文案修订（用户要求移除页面“模拟”等字样）
- 范围：仅 Google 工作台标题、按钮、日志、确认框及回执文案；沿用已批准的原项目双栏设计。
- 不变：内部 synthetic 标识、真实执行禁用、权限和预检/确认/回执规则；其他池的测试数据限制不变。
- [x] 增加文案回归，先确认旧页面失败。
- [x] 清理展示文案，保留明确的执行服务未接入说明。
- [x] 浏览器专项与受影响页面回归，验证本机现有预览，不创建新批次。

## 运行入口取消二次弹窗（用户已明确批准）
- 仅主按钮改为“点击 → 自动预检 → 通过即提交 → 右侧记录”，移除该处理器的 window.confirm；高级分步入口及其他敏感操作不变。
- 更新主按钮说明；保留参数变更拦截、权限、足量 N、单请求保护及未知回执处理，不新增真实执行能力。
- [x] 先改测试验证无弹窗，确认旧实现失败。
- [x] 最小实现和相关浏览器回归，本机预览只读验证，不消耗用户邮箱。

## 批次创建成功不再显示重复顶部横幅（用户截图反馈）
- 用例先验证成功后横幅隐藏；旧实现已失败。
- 仅替换 batchAccepted 中的成功提示，清空旧提示；右侧日志、批次回执保留。后续读取失败、提交被拒绝仍显示错误/警告。
- 更新高级入口测试等待目标到批次回执；无接口、字段、权限或执行能力变化。
- [x] 专项/高级入口回归与静态检查（41 项通过）。
