# 原控制台真实任务入口接线

日期：2026-09-30。原仓库工作树 `fix/onboarding-minors`；未创建独立产品。

## 一句话说明

原来内部执行器存在，但页面无法调用；本轮将它接到原深色控制台的运行按钮、任务列表、暂停/取消、右侧持久日志和同一个系统配置入口。**本轮工作流是 `existing_billing_cloud_v1`：沿用本人已有结算账号准备云资源，不是首次注册、绑卡或试用赠金申请。**

代码接线、测试环境联测、8799 发布、真实 Google 联调是四个不同事实；不以其中一个代替其他三个。未经单独批准不执行模型费用测试，也不启用导入 Sub2API 的账号调度。

```text
可信启动配置（私有本机文件；不来自网页或当前登录用户名）
  +-- 固定 owner / 原邮箱源 / 已导入 mailbox_id
  +-- 固定 Google SDK 身份 / 本机 Sub2API 凭据引用
  `-- 已有 Keyring + RequestMac + schema >= 4（仅校验，不自动迁移）
         |
原项目 /、/onboarding/pools  --> 同一控制台
         |
  点击运行 --> 会话/CSRF --> 原键回执观察
                          +-- 已提交：仅返回历史，不启动第二次
                          `-- 未找到：正式 admission
                                      --> TX1 创建确认
                                      --> TX2 移交确认
                                      --> server-owned manager
                                      --> Google / Sub2API 执行器
  右侧日志 <-- 固定白名单的持久事件；不是原始密码/CLI输出
  请求取消 --> 记录取消 --> 已停止证明/最终核验 --> 条件满足才释放邮箱
  关闭服务 --> join 本进程任务与数据库线程；不接管历史未知任务
```

## 页面 → 操作 → 结果

| 页面 | 操作 | 结果和限制 |
|---|---|---|
| 原 Google 任务页 | 运行任务 | live 启动配置就绪且已读 v2 全局配置时，提交固定单邮箱；不弹原创建任务确认框，不调用旧 `/api/run` 或 synthetic 批次接口。 |
| 原 Google 任务页 | 响应丢失后的原请求核对 | 复用原键和原配置版本；返回 `replayed=true, scheduled=false` 时只是观察历史，绝不再次运行。 |
| 原任务列表/详情 | 查看历史、刷新日志 | 按本人和固定配置版本读取；终态和释放后的任务仍可读。日志不暴露原始 provider 输出。 |
| 原任务详情 | 暂停、请求取消 | 202 只表示命令回执已提交；不能宣称云端回滚或已释放邮箱。live 不开放旧 recheck 重接管。 |
| 系统配置 | 读取/保存 | 同一个 `global_configs(scope=pool)` 配置头。旧 v1 可看但不冒充 v2；新保存严格 v2，只影响新任务。需要 `config:manage`。 |
| 资源管理 → 邮箱池 | 查询/过滤/翻页/详情 | 复用原紧凑表格；本人范围、原游标和过滤。UNKNOWN、未核清历史及占用原样保留。live 元数据修改未接，禁用按钮，不回退旧写服务。 |
| 资源管理 → 邮箱池 → 导入 | 预览/确认可信源 | 读取服务端明确登记的原邮箱源，不在 HTTP 输入密码或路径。返回导入 ID 后仍由可信启动配置绑定，不允许网页任意换绑 manager。 |
| 资源管理 → 信用卡池 | live 模式访问 | 本轮未接卡片真实读写/支付链；旧卡片服务不作为真实支付服务开放。原 synthetic 模式仍保留原功能。 |

运行权限与配置管理权限分开。当前 UI 的 live 运行需要读到 v2 全局配置，因此没有 `config:manage` 的操作者不能借启动按钮读取账单配置或绕过限制。

## 文件与原业务规则影响

| 文件 | 本轮变化 |
|---|---|
| `onboarding/live_mailbox_read.py` | 新 LivePoolPolicy 只读邮箱适配器；复用纯 SQL/DTO/游标，不放宽旧 SyntheticPolicy，不读取密文或执行 graph。尾部再次核验权限和 MAC。 |
| `webui/onboarding_live_boot.py` | 新本机可信组合。私有文件拒绝重复字段、链接、权限不符、未知项；无绑定时只读/配置可用，不猜 owner，不生成密钥、不登录、不跑云。 |
| `webui/onboarding_auth.py` | 增加明确 live 字段、固定路由许可清单、read-ready 与绑定区分、manager 关服 hook。原 HTTPS、Origin、CSRF、会话、错误去敏与 not-committed 证明范围保持。 |
| `webui/onboarding_live_routes.py` | 四个新 live URL；同名旧 task/config URL 使用 helpers，不重复注册。精确 DTO，屏蔽 source 路径/秘密引用/指纹/准入对象，数据库线程取消时等待收尾。 |
| `webui/onboarding_pool_routes.py` | 原 mailbox/config/tasks/events URL 按可信 BOOT 分派；原 synthetic 行为保持。 |
| `webui/onboarding_routes.py` | 原 task detail/pause/cancel 分派；live 模式的旧 synthetic/fixture 历史只读，不借旧命令提升或重跑。 |
| `webui/server.py` | 明确 protected+live 启动；加载现有钥匙和私有 binding 引用，禁止 off+live。原 `/api/run`、旧脚本直接调用保护不撤销；保留 `/onboarding#config-card` 的原受保护环境编辑器链接，不使旧功能失效。 |
| `webui/onboarding_console.py` | 只注入枚举 `data-pool-mode`，不用请求参数决定执行模式。保留原导航、尺寸、资源 Tab。 |
| `webui/static/onboarding-pools.js/html` | live 能力、固定单邮箱运行、v2 配置、可信源导入、live 历史/命令；使用 textContent，未知响应不显示伪造成功，受理后读失败不抹掉回执。 |
| 新测试 `live_boot/live_api/live_api_db/live_mailbox_read/live_ui` | 启动、真实会话/PG、双提交丢回包、取消释放/历史重放、Chromium 页面流程和窄屏。外部服务是测试替身，不冒充 Google 实测。 |
| `tests/test_onboarding_lifecycle.py` | 原页面渲染明确加入模式参数；原诊断/受保护环境编辑器、保护与生命周期断言保留。 |
| `tools/onboarding_test_runner.py`、对应测试 | 明确登记新增模块，保留所有测试文件覆盖和禁止 skip 的规则。 |

## 表、字段、存量数据

**没有新增 SQL 表、列或迁移文件；本轮没有修改 8799 实际数据。**

- 邮箱读取使用 `mailbox_registry` 的本人归属、分组、健康、停用、版本、时间字段，平台状态及 `resource_leases` 的逻辑占用；不改变使用资格或清理过期占用。
- 配置保存沿用已实现的 `global_configs` v2 JSON 字段：schema_version、workflow、billing_account、region、project_prefix、service_account_prefix、approved_role、google_auth_profile_ref、instance_ref、group_ref、timeout_seconds、concurrency、scheduling_enabled、model_verification_enabled。
- 运行/命令/导入由原内部服务写入 `onboarding_tasks`、批次及配置 pin、`resource_leases`、`operation_receipts`、`audit_events`、`secret_objects`、`live_run_plans`、效果记录等已有表；Web 不直接拼 SQL 修改资格或任务模式。
- 旧任务固定旧配置，不随最新配置漂移。旧 synthetic 数据不自动转换为 live，不为已有密码复制第二套 Google 密码。

## 启动注册与上线缺口

现有 Settings JSON 不增加字段。另用私有文件配置 `ONBOARDING_LIVE_BINDING_FILE`，精确字段：

```text
owner_id             明确授权的本机操作者 UUID，不取“当前登录者”当默认
data_root            原邮箱池数据目录，服务端固定；源 emails.txt 遵守原私有文件规则
mailbox_id           已正式导入的 ID；首次仅导入阶段可为 null
sub2api_config_file  私有管理凭据文件；mailbox_id 为 null 时此项也为 null
```

`ONBOARDING_POOL_MODE=live` 必须同时使用 protected 模式。无 binding 时允许只读/配置，不允许启动；binding 不合法时 fail closed。BOOT 仅做本地验证，实际 run 仍重新核验 source、当前权限、已固定配置、Google 身份及官方 billing。capabilities 可用不代表 Google 登录有效、赠金到帐或 worker 存活。

待完成/验收：首次注册/支付 iframe/授权验证码/赠金证据与代理链、任意批量和跨平台完整复用、live 卡池及邮箱管理写入口、跨进程恢复与运维回滚验收、真实云/Sub2API 与另行批准的 Gemini 费用测试。前轮较大 PG 回归的两次间歇失败尚未确认根因，不能用本轮小组合通过宣称已解决。

## 回退

先停止接受新任务，调用 manager shutdown 等待自有任务/数据库线程结束；结果不明确时保留任务与占用待核对。撤销 live BOOT 开关可撤下入口，但**不能删除回执、密文、租约或直接把 live/v2 数据改成 synthetic/v1**。v2 配置头存在时，旧 v1 写入可能按原 guard 拒绝；回退代码不是自动数据兼容迁移。任何预览库升级、重启与真实外部操作单独确认。

## 验证证据

原始输出位于 `/workspace/gcloud/.local/live-entry-evidence`。所有测试使用自造材料；没有读取/打印真实银行卡、CVV、Google 密码或 access token。

2026-09-30 本轮最终验证：

| 检查 | 结果 | 原始证据 |
|---|---|---|
| offline 套件 | 397 通过；0 失败、错误、跳过 | `live-web-offline-final.log` / `.json` |
| 完整 Web 套件 | 445 通过；0 失败、错误、跳过；包含 21 项新增 Chromium 用例 | `live-web-all-final.log` / `.json` |
| 新邮箱读取 + HTTP/PG 组合 | 18 通过；串行执行完毕后才启动 Web 套件 | `live-web-pg-serial-final.log` |
| HTTP/PG 测试断言再加固后重跑 | 3 通过；检查账单身份表及审计整数字段的严格类型，属于上面 18 项的重复验收 | `live-api-db-hardened-final.log` |

以上合计 860 项所选检查，不是全项目全部套件，也不是 Google/银行/Sub2API 真实端到端验收。真实 PostgreSQL、ASGI 会话/CSRF、私有 BOOT、manager、取消收尾有组合测试；外部效果使用替身或在首次效果之前停住。Chromium 用例的 API 响应被拦截为测试数据。没有实际创建 Google 项目、申请赠金、绑卡或调用 Gemini。

验收期间发现并处理：原 `/onboarding#config-card` 环境编辑器被额外页面别名遮蔽，已撤回该别名；正常 `auth.session_touch` 审计写入与只读断言冲突，改为只允许精确预期的认证审计、仍严格检查业务表不变。最初错误启动的重叠 Web 验收已主动中断，保留 `live-web-all-aborted.log` 和 `live-web-first-overlap-note.txt`，不计通过；最终 PG 与 Web 已按顺序完整重跑。

三个代理分别实现和交叉审查 UI、HTTP/PG、邮箱适配与可信启动边界；主代理检查原始输出并复跑上述套件。审查记录见 `live-http-adapter-spec-review.md`、`live-boot-dispatch-spec-review.md`、`live-api-db-final-quality-review.md`。报告页和原界面已做窄屏/桌面溢出检查；HTML 进度说明位于 `/home/weifashi/www/gcloud_onboarding/live-web-report.html`，不是已部署截图。

上述开发验收时 8799 尚未更新。**随后用户明确授权“更新 并验证”，2026-09-30 已完成本机预览数据库 2→4 与 protected+live 重启，见 [部署验收记录](onboarding-preview-live-update-20260930.md)。** 当前无可信邮箱绑定，运行/导入禁用，不能因此宣称真实开通已验收。完整上线缺口仍以本文“启动注册与上线缺口”为准。
