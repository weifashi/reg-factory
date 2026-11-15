# 原首页与原任务菜单恢复（2026-09-28）

## 修正了什么
此前把受保护首页切到池工作台，只保留深色视觉，没有保留原完整任务导航，用户明确否定。本次不再另造首页：复用原 `webui/static/index.html`、原 `style.css`、原 `app.js` 的目录/任务参数渲染；保留原 `scripts.py` 作为任务定义的唯一来源。原首页 HTML 与基础 CSS 没有修改。

**本次恢复的是完整菜单和参数查看，不是把全部旧执行器开放到公网。** 本地 off 模式原操作保留；受保护预览的旧注册、支付、凭据导出、代理启动等仍阻断，不把禁用按钮称为业务恢复完成。

## 页面 → 操作 → 结果
```text
登录 → / 原首页（不再跳转池页面）
  ├─ 主流程          4 项 → 原标题、说明、参数、高级参数
  ├─ 单平台注册      5 项 → 原参数表单
  ├─ 邮箱注册        2 项 → 原参数表单
  ├─ 邮箱授权        1 项 → 原参数表单
  ├─ 导出/上传       4 项 → 原参数表单
  ├─ 外部工具        2 项 → 用户主动打开审核过的 HTTPS 下载链接
  ├─ 邮箱池              → /onboarding/pools#mailbox-card 现有统一池
  ├─ 环境配置            → 原视图提供 /onboarding#config-card 安全编辑器链接
  ├─ 其他原工具          → 保留原视图，明确显示执行/秘密读取仍隔离
  └─ 新增
      ├─ Google 开通任务 → 现有合成批次界面，不代表真实开户
      ├─ 信用卡池        → 明确未接管理接口的现有入口
      └─ 全局配置        → 现有受保护配置表单

池页面 → 返回原控制台（新标签） → 不卸载池页，不丢失其内存中的待核对请求
```

原 16 个任务的参数仍由 `renderForm/selectScript` 渲染，包含 233 个 schema 参数（其中 1 个隐藏项），没有复制第二份参数配置。任务切换在 protected 不保存秘密草稿；只读控件不能触发原执行。原始 off 模式草稿、指南、轮询、表单操作保留。

## 文件和原规则变化
| 文件 | 改动与影响 |
|---|---|
| `webui/server.py` | ready 的 protected+synthetic 首页从 303 池工作台改为渲染原 index；依赖缺失仍 503，pool-off 仍诊断，off 原行为保留；注册只读 console 路由。 |
| `webui/onboarding_console.py` | 派生原 HTML：去掉 data favicon，抽离 inline style 到同源 CSS，加保护脚本与标志；保留 ID，生成 ID+class 规则保持原隐藏优先级且不用 important。只投影已加载 schema 的表单字段，去执行文件字段和敏感默认值；不读运行配置/凭据。catalog/styles 路由重验 Actor、权限与池就绪。 |
| `webui/onboarding_auth.py` | 仅增加原 app、新 console JS/CSS、两个 console GET 和原支持图片的精确读取许可，要求登录、onboarding:read、池就绪。不开放旧 API，不放宽 CSP/Host/CSRF。 |
| `webui/static/app.js` | protected 分支使用安全目录和原渲染器；转义元数据，停止自动旧 API 调用，危险 handler 早退，保留原工具导航；off 原业务行为不变。 |
| `webui/static/console-protected.js/css` | 会话/内存 CSRF 注销、失败关闭、增量入口与隔离提示；移除受保护页面旧 localhost 链接；修复手机顶栏和 Plus 视图溢出。 |
| `webui/static/onboarding-pools.html` | 增加新标签返回原首页入口；原池 JS、API、回执/CAS/秘密清空规则不变。 |
| 测试/runner | 新 console 后端与浏览器测试纳入显式 Web 套件；更新首页/原 app 资产的旧预期，其余旧执行阻断测试保留；覆盖原 off 回归。 |

## 数据与权限
新增表 0、字段 0、迁移 0。保留预览的隔离 schema、密钥、登录账号和权限；没有导入真实邮箱/卡片、执行原 CLI、请求外部云/银行/模型或改变 Coder 分享权限。所有旧执行 handler 的第二层包装与 middleware 仍保持。应用服务只更新此前授权的本机 TLS 预览，不代表生产发布。

## 验证与已知边界
本机私有证据：`/workspace/gcloud/.local/preview-launch/original-index/`。包含 RED/GREEN、最终套件 JSON、真实 TLS/API 浏览器、截图、HTTP 负例与冻结源码哈希。

独立审查发现并修复：CSS 提取后的 ID 优先级降低导致 Plus 首屏误显示；Plus 的 flex wrap 导致手机内部溢出；三项旧 onclick 缺保护早退；schema 动态文本进入旧 innerHTML 的注入。未删断言掩盖缺陷。初次离线+旧回归的原指南字面断言失败记录保留，恢复原内层判断后重跑。

真实浏览器核对全部 16 个任务的标题和可见参数，原 6 个分类、邮箱池导航、环境配置链接、390/768/1440 页面宽度、JS/CSP 与意外旧请求。HTTP 正反向核对登录、静态文件、新目录、错误 Host/Origin、未登录、无 CSRF 和旧执行阻断。此浏览器经本机 DNS 映射访问实际 TLS 服务与实际 API，不是对外 Coder 已登录整链路证明。

Plus 内部两处原“正在检查”静态提示已改为“保护模式 · 未检测”，并增加回归断言，不以静态文案伪装正在检测。真实执行器适配、统一旧数据消费者、信用卡 HTTP 管理、真实 Google/Vertex/Sub2API 和费用验证均未完成。

## 回退
只回退本轮原首页 renderer/catalog、新资产许可、app 的 protected 分支及对应资源/测试；保留此前认证/导航/池实现和其他未提交代码。身份核验后仅重启原预览进程，保留 schema/keyring/审计；禁止以关闭认证作为公网回退。没有提交/推送/合并主分支。

### 验收过程中保留的失败记录
首轮完整 Web 的 246 项中出现 1 个断言失败和 2 个错误，不能作为通过记录：新增返回原首页链接成为移动菜单首个焦点，旧断言仍期待 Google 项；现明确断言返回链接后继续 Tab 到 Google，未移除键盘循环要求。数据库错误有 PostgreSQL 06:35:47 statement timeout 日志；浏览器超时发生在可见、启用、无动画的按钮上，调查同期记录 I/O full pressure 53.17%，只支持环境停顿解释，不证明具体 Chromium 内部原因。未修改数据库/浏览器超时，也未使用强制点击；等待资源压力下降后重新验证。记录位于 web-final.json/log、affected-final.log、ui-env-investigation.log、ui-env-host-pressure.log、db-timeout-recheck.log。

### 最终验证记录
- `web-verified.json`：247 项，0 断言失败、1 错误、0 跳过；错误发生于隔离库建表阶段。06:48:14 PostgreSQL 日志确认 statement timeout。**整套这次运行未全绿**。
- 同一失败用例独立重跑：1 项通过，3.197 秒（`db-scope-recheck.log`）；没有改迁移/浏览器超时，也没有降低权限检查。这不是把失败整套报告重写成成功。
- `offline-legacy-final.json`：102 离线 + 73 原项目回归，共 175 项通过，无失败/错误/跳过。
- 最新完整 Web 运行中的 11 项新原首页浏览器用例全部通过；移动菜单新增首页链接的正反 Tab 顺序也通过。
- 实际 TLS/API 全部任务巡检与手机/桌面记录见 `browser-live.json`（补充 Plus 两条文案之前）。最终源码首页独立检查见 `browser-final-root.json`：16 个任务、Plus 隐藏及未检测文案、无 JS/CSP 错误。最终源码再跑完整巡检在手机菜单点击处超时，记录 `browser-live-final-source.log`；未将该运行说成通过，也未据此放宽 CSP 或测试超时。

因此可以确认菜单/原参数恢复和预览更新，不把本轮当作整个产品上线验收通过；原执行器、真实云链路及完整发布验收仍未完成。
