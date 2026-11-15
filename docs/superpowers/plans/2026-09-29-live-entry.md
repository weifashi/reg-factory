# 后续状态（2026-09-30）

本文件下方包含 2026-09-29 的阶段快照。后续已实现原 HTTP/BOOT/同一页面 live 分支、v2 配置/可信来源接口、只读邮箱列表及生命周期挂载，见 [接线记录](../../operations/onboarding-live-web.md)。这些代码的测试结果、尚未发布状态与剩余工作在该记录中分别说明；本计划的首次开通与上线目标没有标记完成。

---

# 原项目真实运行入口：后续实施清单

承接已确认的“复用邮箱池、统一全局配置、原运行按钮与右侧日志”方案。本文件是待实施清单，**不是已接通记录**。首次开通、支付、赠金与完整上线仍须单独完成真实验收。

## 不改变的业务规则

- 不把旧 synthetic 任务升级成 live；新任务 INSERT 时明确模式。
- Google 密码沿用用户授权的原邮箱池密码，不新增第二个 Google 密码管理池。
- 邮箱跨平台状态独立，同一邮箱不能被两套领取器并发消费。
- 配置只由全局配置派生，固定任务配置/凭据版本；批次不允许任意覆盖。
- 未核清历史不因导入成功变成 UNUSED；网络超时、TTL、进程失联不证明可释放。
- Sub2API 导入保持停用、不可调度；不发起未获批准的模型调用。
- 原页面同一项目，不另起界面或数据库来冒充交付。

## 第一阶段：可信材料与配置（先不访问云）

- [x] 原 `emails.txt` 精确邮箱桥接：复用现有解析器，但明确归属和来源版本。拒绝重复/冲突记录、来源替换、fixture 数据；不调用带游标/领取副作用的 `get_email`。首批精确授权账号，尚未扩展任意批量邮箱。
- [x] 新 live **写入**秘密策略。保留旧 `SyntheticPoolPolicy` 和 fixture validator，不全面松绑。真实材料使用可辨认的新 access_policy/payload 版本；不把旧密文改标签。
- [ ] 受控浏览器消费者与任务凭据版本绑定；当前写入服务没有明文 getter、消费者或 worker 启动。
- [ ] 复用 `secret_objects` 与 `mailbox_registry`，同邮箱密码只保留一个版本来源；Google 平台绑定引用此来源，不复制一份可独立修改的密码。
- [x] 统一全局配置 v2 **领域服务**：workflow、region、project/SA 前缀、billing_account、approved_role、Google 授权 profile 引用、Sub2API target/group 引用。保留旧 v1 历史读取。新 UI/HTTP 写入口仍未接入。
- [x] v2 只接受固定服务端 profile/target 引用，不接收任意 SDK 路径、秘密文件或 URL。并发/超时只允许已有执行器支持的值，调度与模型验证固定关闭。

验收：精确账户、归属/session、版本更改、重复、错误来源、fixture 混入、未知权限均有拒绝用例；列表/日志/异常不回显秘密；旧导入和历史配置回归不变。

## 第二阶段：明确新建 live 任务与内部启动

- [ ] 可信资格证据绑定邮箱 credential/config 版本；gcloud 身份须匹配同一邮箱。不能把账单 open、语法正确或密码存在当作赠金/历史/费用批准。
- [ ] 初次真实验收只允许一个明确指定且已授权的邮箱，后续再扩展已确认的按数量调度；不是永久删掉批量需求。
- [ ] 新 `live_batches.create_one` 只接引用/版本/request_key，事务内明确 INSERT live。复用锁序、CAS、固定回执；禁止旧 `create` 后 UPDATE mode。
- [ ] 兼容 `live_runtime.handoff` 依赖的原创建证明：若改变回执 action/summary，增加严格的 live 解析，不能放宽旧 synthetic 证明。MAC 绑定 live/workflow/来源版本。
- [ ] 首次创建 COMMIT 确认后，再开启 task-first 事务做 handoff + bind_plan。项目/SA 名从任务 ID 稳定派生；billing 仅来自固定全局配置。
- [ ] 第二次 COMMIT 确认后才构造 LiveExecutor → run_managed；任一 COMMIT_UNKNOWN 不启动，不重发、不重新接管。

验收：旧任务逐表快照不变；明确来源、旧 request_key、旧配置/凭据、过期 session、错误身份均拒绝；两处未知提交均零启动；已有停止/取消/保留占用规则回归。

## 第三阶段：原页面与真实单账号联调

- [ ] 在原邮箱池/系统配置/Google 任务页接同一个可信入口，保留右侧日志。解除按钮 disabled 本身不算接通。
- [ ] 云资源链明示适用已有结算账号，不冒充首次开通、支付、赠金已验证。
- [ ] HTTP/浏览器/后台运行组合先在本地桩与隔离 PG 完整验收，再做已授权单账号、已确认 billing 的真实联调；不自动提高权限或减弱组织策略。
- [ ] 接完浏览器真实联系人保存、支付 iframe、授权验证码渠道、首次试用、credits 证据与代理出口后，才能验收用户要求的首次开通链。
- [ ] 补后台调度、迟到取消、跨进程恢复、备份恢复、故障/安全/全回归及运维记录，满足后才能宣布上线完成。

## 当前事实，不混淆责任

原邮箱池代码已有密码存储渠道，但本机当前 preview 的实际 data-root 未发现 `emails.txt`；未读取其他账户或原始密码。授权 Google 账号只读 Billing API 在 2026-09-29 12:50 UTC 返回可见结算账号 0。即使材料补齐，上述入口、可信来源、配置映射、资格与浏览器流程仍是待完成代码，不能全部归因于用户缺资料。

迁移、重启、真实费用测试、调度启用分别按授权执行，不因本清单存在而自动获批。

## 本次落地范围

`live_mailbox_source.py` → `live_input_vault.py` → 原邮箱/秘密/回执表；`live_config.py` → 原全局配置头。
没有第二产品、第二套邮箱数据库或复制 Google 密码。导入后仍是 UNKNOWN / HISTORY_UNRECONCILED；导入成功不是开通资格。共享 v2 解码器的旧任务调用方另加严格 v1 guard，避免把真实配置误用于旧任务。

实现与验证记录见 [可信输入接入记录](../../operations/onboarding-live-inputs.md)。勾选项是这部分代码已实现，不代表第三阶段的页面、真实开通、支付、赠金或完整上线验收已完成。

## 下一切片最初定位的代码契约（历史调查，以下实现进度另列）

- `live_runtime._graph` 仍要求旧 `pool:` 邮箱策略和独立 platform secret。正式入口须严格采用 pinned v2 配置、`pool-live-v1` 邮箱秘密，并让 Google pin 引用同一邮箱 secret/revision；不能撤掉本轮旧 writer/read guard 来“兼容”。
- `pool_cancellation._creation` 只认旧 `pool.batch.create/BATCH_ACCEPTED`。live 创建必须有独立严格证明解析器，不能冒充旧 action 或把旧 action 验证扩成任意白名单。
- `live_run_store.bind_plan/read_plan` 需以任务固定 v2 配置重算 `live_config.plan_for` 并比较，不读取最新 head 覆盖旧任务。
- `handoff` 当前在事务内返回进程本地 provisional handle。可信 composition 必须在 `with unit_of_work` 正常退出后才返还该 handle；第一次 create 或第二次 handoff/bind_plan 的 COMMIT_UNKNOWN 均零构造 Executor、零启动。原回执重放不授予再次启动权。
- `common/emails.next_email/latest_email/retryable_email/mark_registration_started` 和 `common.asset_store.get_email/_claim_record` 及延迟导出领取都不查询 PG 租约。PG 内的 UNIQUE/lease 无法防这些旧脚本抢同邮箱。接入执行前，须统一纳管排他规则；未统一时拒绝旧入口领取已纳管邮箱，不能仅增加一个没有其他参与者遵守的锁。
- 导入仍然 UNKNOWN/HISTORY_UNRECONCILED；当前 `mailbox_update` 只支持分组/停用，没有可信资格升级服务。缺口属于代码，不能让调用者传 `qualified=true` 绕过，不能归因于只缺用户账号。
- WebConfig/server、pool 路由和原按钮仍是 off/synthetic；task detail/command 仍使用 v1 固定配置读取。先完成内部 live 领域链、同凭据及排他协调、两提交零启动反例，再接同一页面的 live read/control/start。

## 2026-09-29 本轮实现进度（不等于上线验收）

- 已写入 `live_admission`：同进程/会话、短时、不可由 HTTP 自填的证据，只适用已有结算账号的资源准备流程。官方身份和 Billing 可见不等于赠金资格。
- 已写入 `live_batches.create_one`：原表显式 INSERT live、独立 MAC/回执/创建证明、同源 mailbox secret 的 Google pin。原 UNKNOWN 与未核清历史不被改写为已验证。
- 已写入 `live_entry.start_committed`：依赖类型/固定目标/本地钥匙就绪前验，TX1 确认后 TX2 handoff+固定计划；TX2 确认后才构造执行器。回执重放不启动；内部 manager 已调用，原 HTTP 尚未挂载。
- `live_runtime/live_run_store` 已改为严格 pinned v2、新创建证明与同源秘密；不保留 synthetic-to-live 兼容后门。
- 新 `live_commands` 提供 pause/cancel 领域服务，原 finalizer 对安全取消恢复 HISTORY_UNRECONCILED，而非 UNUSED；原 HTTP 控制路由仍待版本化分派。
- 原文件消费者已接入统一 managed ownership 边界及拒绝传播。根联合 165 项离线回归通过；它阻止旧领取器并发交付纳管邮箱，不代表全部旧平台已迁入 PG 租约或跨平台需求已全部完成。
- 已写入 `live_batches.observe_request`、`live_read`、`LiveRunManager` 以及释放后的原命令历史重放；原请求只观察不重启、终态历史可读、关服 join 自有线程。最新 44 项 PostgreSQL 组合测试通过，内部组件与 HTTP/boot 挂载仍区分；较大 141 项回归的两次间歇失败根因仍未确认。
- 新增/修改隔离测试、独立 SPEC 与质量审查；首轮回归失败记录保留，最终证据以 [内部入口记录](../../operations/onboarding-live-entry.md) 与其末次日志为准。

**尚未完成：** 原页面真实读/写/运行/日志组合和后台生命周期的 server 启动挂载、首次支付与赠金全链、任意批量和跨平台消费者完整迁入、真实外部联调与上线运维验收。当前 PG 回归仍有失败；没有对 8799 实施重启或正式数据迁移。

2026-09-29 16:33 UTC 再次只读检查授权账号：Billing API HTTP 200，可见账号 0（无后续页），云写入 0。此事实仅说明当前联调材料状态，不能替代上述待完成代码。
