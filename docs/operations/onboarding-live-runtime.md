# Google 真实执行组合：运行时阶段记录（2026-09-29）

> **不是上线交付。** 本文补充 `onboarding-live-adapters.md`。工作位置仍为原仓库的 `fix/onboarding-minors` 工作树；不另建产品。此记录不包含密码、卡号、验证码、token 或私钥。

## 当前可验证事实

- 2026-09-29 10:14:39 UTC，经用户已授权的 Google 身份，真实 token provider 成功取得令牌并只读请求结算账号列表，HTTP 200、可见账号 **0**、无下一页。不能据此证明仍有试用资格。
- 没有创建 Google 项目、关联结算、绑卡、申请赠金或调用模型，没有向 Sub2API 导入真实 Google 私钥。
- 8799 原页面没有被切换为真实执行；历史任务仍保持 synthetic。数据库新版本只用于可清理测试 schema，未迁移 preview。
- 不能说“只差用户资料”：真实配置/资料入口、首次开通浏览器流程、原 API 接线、完整产品级停止/恢复/释放及运维验收仍未完成。内部同进程停止与零效果取消收尾已实现，边界和剩余缺口见后节。

## 本阶段新增或修改的组件

| 文件 | 大白话 | 不代表什么 |
|---|---|---|
| `onboarding/google_auth_runtime.py` | 通过固定 SDK、固定授权身份取得短期 token；环境清洗、有限时长/输出、身份二次检查 | 不等于浏览器已登录、已绑卡或已获赠金 |
| `onboarding/sub2api_target_runtime.py` | 复核批准的本机进程、监听端口、运行目录和实际可执行文件摘要 | 不是原子 socket 身份认证；不防恶意同 UID/root 的极窄替换竞态 |
| `onboarding/live_runtime.py` | 把未开始任务的原批次占用交给当前进程，提升 fence；提供心跳、发送/只读核对守卫、逐步推进 | 不把旧测试任务升级成 live，不提供重启后的自动接管 |
| `onboarding/live_key_store.py` | Google 返回的 JSON 私钥立即加密保存；仅封闭交给绑定计划的停用导入器 | 没有通用明文读取/公开下载，不是邮箱密码或信用卡通用金库 |
| `onboarding/sub2api_import.py` | 补独立的退出登录目标守卫，并在真正发送私钥前再次检查秘密是否撤销/过期 | 不启用调度，不调用 Gemini，不在未知结果后重发创建 |
| `onboarding/async_runtime.py` | 取消异步操作时先等待已经开始的 DB 线程收尾，再抛取消；不会继续调用消费者 | 不等于数据库事务被取消，也不保证 Python 明文复制可清零 |
| `onboarding/live_executor.py` | 内部单任务组合器连接固定七步、独立心跳、单次许可和实际证据 | 不创建 live 任务，不接网页，不代替首次绑卡/赠金流程或最终停止释放 |
| `onboarding/live_run_store.py` | 固定任务的云资源计划；持久保存真实适配器的白名单结果，与 journal 同事务提交 | 不是用户自报“成功”的接口；不凭生成一个 UUID 冒充证据内容 |
| `004_live_runs.sql` / catalog / migrate | 为计划、实际结果新增只追加的表；显式 target 4 | 不自动升级、不改变历史任务、不放宽旧 synthetic 配置 |
| `tools/onboarding_test_runner.py` | 将新增组件列入显式回归模块集合，漏模块会失败 | 局部测试通过不等于全量或生产验收通过 |

## 数据表与字段

### `live_run_plans`（新增，只允许 app SELECT/INSERT）

```text
 task_id / task_mode  -> 只引用 live task，不提升旧任务
 generation          -> 当前任务代次
 config_revision     -> 固定配置版本
 owner_id            -> 任务创建者
 plan                -> GooglePlan 的非秘密字段
 plan_digest         -> 固定目标摘要
 created_at          -> 数据库时间
```

`plan` 包含专用 project ID、billing account ID、SA ID、owner/task ID、区域、批准的 IAM role 和可选 parent；不含 token、卡号、密码、私钥。首次操作意图出现后禁止首次绑定/更换计划。读取重新验证类型、归属、摘要和当前会话/租约。

### `live_effect_observations`（新增，只允许 app SELECT/INSERT）

```text
 id / receipt_id / task_id / action -> 精确对应单次操作
 incoming_phase / reason          -> 适配器阶段、固定原因码
 external_ref / operation          -> 已校验的资源名 / LRO 名
 secret_ref                       -> 私钥金库引用，不是私钥内容
 account_id                       -> Sub2API 返回账号 ID
 logout / scheduling / verification-> 退出、禁调度、未调用模型的结果
 observed_at                      -> 数据库时间
```

更新既有 `live_effect_journal` 的阶段、固定原因和证据引用，与插入观察记录处于同一事务。发生回滚，两者均不能留下半份成功。返回值跟随 journal 的 **canonical evidence_ref**，不能混合旧成功阶段和后来另一份观察的账号/私钥引用。读取也重新验证 DTO；数据库中形状合法但内容异常的文本不原样输出。

### 既有表的运行时影响

- `resource_leases`：交接改变 owner、提升 fence/version，续租更新期限；不按 TTL 擅自清空占用。
- `onboarding_tasks`：交接设 RUNNING、固定 current_step、提升 version；只有已确认步骤才推进。最终步骤成功尚不自动设整任务 SUCCEEDED。
- `secret_objects`：使用既有 `service_account_json` kind；保存 AES-GCM 密文、nonce、key version 与 task/source-receipt 绑定策略。不会存 CVV 或短信验证码。
- `audit_events`：交接、续租、步骤观察、秘密保存/消费使用既有固定码；不记录 provider 原文。
- 邮箱池、信用卡池、历史平台资格和用户已有数据不被迁移或清零。

## 原页面 → 操作 → 当前结果

```text
原 Google 开通任务页
    |
    +-- 点击运行 -> 当前仍走既有预检/批次记录，不启动真实云操作
    |
    +-- 暂停/取消 -> 保留此前安全取消规则
    |
    +-- 新组件 -> 尚未作为可运行模式挂入按钮
```

已实现的内部组合（不表示已在原页面启用或真实部署）：

```text
新建明确 live 任务（不能提升旧任务）
   -> 原批次租约交接 + 固定计划 [确认 COMMIT]
   -> 独立心跳
   -> 当前步骤/context -> INTENT [确认 COMMIT] -> 单次许可
   -> 固定 API 执行 -> 保存白名单观察 + 更新 journal [确认 COMMIT]
   -> LRO: 有限只读核对；未知/权限/会话/租约问题: 停止，不重发
   -> 私钥加密保存 -> 停用导入 -> 读回 inactive / schedulable=false
   -> run_managed: 本地停止证明 + 独立数据库收尾
   -> 整任务成功、首次试用/支付/credits 仍须另行完整核验
```

计划的 billing 可见性不等于赠金到账或模型费用批准。Google 账号必须与任务选定的邮箱身份一致，不能拿本机一个账号的 token 为任意邮箱执行。

## 证据与保留的失败

私有日志目录：`/workspace/gcloud/.local/live-runtime-evidence`；不通过公开静态目录提供。

- `authorized-google-readonly.json`：上述真实只读核验；cloud_writes=false、model_calls=false。
- `offline-final.json`：本阶段较早版本 **266 / 266，通过，无跳过**；不覆盖后加的计划/证据/组合器。
- `offline-runtime-final.json`：最终修改后的离线套件 **270 / 270 通过、0 failure、0 error、0 skipped**，16.183 秒。
- `runner-runtime-final.log`：测试模块完整性等 runner 回归 **10 / 10 通过**，1.373 秒。
- `runtime-integration-final.log`：私钥存储、worker 租约及适配器组合 **46 条通过**；真实隔离 PostgreSQL + HTTP MockTransport，不是 Google 真实端到端。
- `disclosure-db-negative-control.log`：故意在测试中去掉发送前秘密重检，撤销场景按预期出现 **1 条断言失败**，证明检测有效；它不是产品绿测。
- `run-store-red.log`：计划/证据模块缺失时先红。
- `run-store-hardening-red.log`：import 无确认密钥来源、回拨时间导致证据顺序错误，两条先红。
- `run-store-hardening-green.log`：对应阶段 **15 条通过**，后续持久记录回显边界仍另行验证。
- `run-store-persisted-red.log`：持久证据坏文本投影先红。
- `run-store-migration-final.log`：最后证据投影修复后，计划/证据、catalog、迁移序列 **36 条通过、无跳过**。
- `runtime-safety-final.log`：取消线程收尾、单次许可与停用导入 **39 条通过**；独立审查者也重跑确认。
- `disclosure-target-red.log`：验证私钥之前守卫有效、等待期间失效，旧代码仍 POST 的回归先红；已修为披露检查后再次核发送门和目标。
- `executor-20-final.log`：最终组合器 **20 / 20 通过，105.853 秒**，真实隔离 PostgreSQL、真实加密存储与 HTTP MockTransport。原始日志 SHA-256：`8b6c45eebeb85fbd9cf594e8e53081691221228eba4452308429704bbab9268e`。
- `executor-root-combined-final.log`：主代理在最终代码上重跑执行器、私钥存储、计划/证据、持久许可数据库回归，**67 / 67 通过，182.869 秒**；并非只引用代理口头结论。
  首轮 17 条中 logout/LRO 两例出现 DEPENDENCY_UNAVAILABLE，仅保存于工具输出而非文件；后续独立两例通过但未确定原始 SQLSTATE，不能杜撰根因。后续 19 条通过，再修“无新许可时不得补记旧 INTENT”的 P2，形成最终 20 条。

另修 `tests/test_onboarding_pool_batches.py` 的 secret 过期等待夹具：目标锁前 barrier、无写入 blocker rollback 释放，保留真实 SQL/数据库时钟/2 秒锁超时。目标用例独立进程连续 **5/5 通过**；没有放宽业务超时。此前兄弟 config-session 用例出现期限提交耗时 2730ms 大于 1200ms 的夹具失败，未把它掩盖为通过；固定 metadata 记录在 `pool-batch-expiry-barrier-serial-siblings.log`。主代理最后串行重跑六个真实等待边界和三个新增控制用例，`expiry-root-final.log` 为 **9 / 9 通过，42.839 秒**；这不抹去旧失败，也不证明任意负载下固定短窗口永远稳定。

较早全量回归 `/workspace/gcloud/.local/live-adapters-evidence/acceptance-all.json`：**1282 条、1 failure、1 error、0 skipped，不通过**。其中迁移锁等待与测试时间窗口分别单例通过，但不能将原全量改写为通过。失败原日志保留。不得自动重试未知外部操作来掩盖错误。

## 尚缺的上线门槛

1. 真正资料池（邮箱/Google 凭据、真实账单信息、卡片）及受控消费/下载；不放宽合成类型。
2. live 配置、费用/试用资格证据、单任务可信创建，与原 UI/API 同库身份闭合。
3. 完整后台调度、迟到取消/结果和跨进程恢复；本地停止/零效果安全取消已有内部闭环，但 `CHAIN_VERIFIED` 仍不等于任务终态、不释放占用。
4. Google 首次开通：真实联系人保存、付款 iframe、验证渠道、试用激活与实际 DOM 验证。
5. 代理出口与同账号执行会话、真实 Sub2API 凭据受控装载及失败处置。
6. 实际单账号端到端、备份恢复、故障注入、回归、运维/上线验收。

不自动提权或关闭组织策略，不使用伪造地址/身份，不绕过本人验证，不重复申领不合资格试用。未批准收费 Gemini 调用和调度启用，程序不得发送。

## 回滚边界

未在 preview 安装 004，因此本阶段回退仅需精确撤回新增模块、catalog/runner 条目，不得 reset 整个有大量用户改动的工作树。若后续获授权安装并实际产生外部效果，不可删除 journal/计划/证据以“重置重试”；必须先核对外部状态，保留审计。

## 后续切片：停止收尾与原页面真实记录（2026-09-29）

本节只说明本轮新增变更，不替代上面的完整上线门槛。没有新增数据库迁移、真实开通入口或收费权限。

### 大白话流程

```text
内部 run_managed(executor)
    |
    +-- 执行链退出 -> 关闭发送门 -> 等 DB 线程 / 心跳结束
    |                       |
    |                       +-- 原 executor / worker / PID 的不可复制停止证明
    |                           冻结“已观察 COMMIT_UNKNOWN”，后续错误不能覆盖
    |
    +-- 数据库独立收尾（不信普通 Result、网页自报成功或 stopped=true）
          |
          +-- 已取消 + 首步 + HELD + 零效果/审批/卡关联/异常历史
          |       -> CANCELLED_SAFE -> 邮箱 UNUSED -> 清占用、fence + 1
          |
          +-- 七步精确回执成功 + 停用导入/未调用模型/logout 通过
          |       -> PAUSED / CLOUD_CHAIN_VERIFIED -> 保留占用
          |          首次试用、支付、credits 全部仍是 NOT_VERIFIED
          |
          +-- INTENT / UNKNOWN / 冲突 / 其他不确定
                  -> PAUSED 或保留 CONFLICT -> 保留占用，不重发
```

停止证明不等于所有 OS 子进程退出、Google 长操作消失或 HTTP 注销成功。固定 gcloud 身份/令牌进程清理失败时可能仍完成 OAuth/cache 操作；它不能因此取得新的云资源写入许可。释放还必须满足上面的零写入历史条件，不单凭停止证明。

同一证明只有一次收尾尝试。未知 COMMIT 后只按同一身份读取原回执，不重新执行。旧邮箱已给下一批次后，读取旧回执不会改新任务、租约或平台记录。若先停止并保存 PAUSED，后来才申请取消，暂不自动第二次释放；跨进程恢复及迟到取消仍未完成。

### 页面 → 操作 → 结果

| 页面 | 操作 | 本轮实际结果 |
|---|---|---|
| 原 Google 开通任务页 | 查看历史任务/刷新日志 | 从库读取真实 execution_mode，显示云资源步骤回执，不读取私钥/原始响应 |
| 右侧日志 | 看到 INTENT / UNKNOWN / CONFLICT | 分别显示未确认、待核验、证据冲突；不会写成开通成功 |
| 右侧日志 | 看到 CLOUD_CHAIN_VERIFIED | 明示仅云资源步骤核验、占用保留、首次试用/支付/赠金未核验 |
| 新旧任务页面 | 查看明确 live 的历史任务 | 当前页面只读；不开放启动/暂停/取消/重检写入按钮或其 JS handler |
| 新静态资源遇到旧 API | pool DTO 缺 execution_mode 且原 synthetic=true | 可显示历史记录，但标注执行类型未核实、禁用写入；不把旧硬编码布尔当作真实执行类型证据 |
| 原运行按钮 | 点击运行 | 原任务编排行为不变，尚未连接真实开通执行器 |

后端 schema 2 经迁移校验后明确视为旧 synthetic；schema 3+ 必须查询真实列，缺列/坏值报错，不捕异常降级。原 fixture 的历史 8 字段详情协议保留。前端只读不是新增服务端授权规则；旧命令 API 本轮未扩权，也未接成 live 控制协议。

### 文件与字段/业务规则影响

| 文件/表 | 变化 | 没有变化 |
|---|---|---|
| `live_executor.py` | 进程本地停止凭据、线程追踪、不可被后续错误覆盖的未知提交事实 | 不提供 HTTP 创建入口，不自动授予费用或调度权限 |
| `live_completion.py` | 新内部收尾及只读回执、取消等待收尾后再抛出 | 不跨进程接管、不回滚云端、不凭租约过期释放 |
| `onboarding_tasks` | 收尾更新 status、reason_code、version；取消标记不清除 | 不把七步通过设为 SUCCEEDED，不修改 execution_mode |
| `resource_leases` | 仅安全取消清 task_id/owner_id/lease_until/hold_reason，fence/version 递增 | 保留占用时不写 stopped_evidence_ref，不堵死原只读核验 |
| `mailbox_platform_states` | 仅安全取消恢复 usage_status=UNUSED、清 last_task_id、version 递增 | 不伪造身份确认、不提高资格、不删除实际使用历史 |
| `operation_receipts` | 固定 action=`live.run.stop`，存范围/版本/计划摘要/停止结果 | 不含密码、PAN、令牌或私钥；回执 SUCCEEDED 只代表收尾事务成功 |
| `audit_events` | 固定白名单 action/code 的收尾和释放审计 | 不写 provider payload、原始异常或秘密 |
| `task_read.py` / pool routes | DTO 新增 execution_mode，synthetic 从真实 mode 派生 | 列表游标、本人权限及历史配置关联不放宽 |
| `task_events.py` / 两套页面 JS | live 固定步骤/阶段/原因白名单，有限回执投影、中文状态 | 不显示 target/plan/secret_refs；CONFLICT 仅接受合法配对 |
| runner / 测试 | 新停止证明、收尾、兼容与页面回归纳入明确套件 | 测试 schema 不提升任何 preview 历史任务 |

### 本轮验证状态

- 原账号只读复核：UTC `2026-09-29T12:50:00.454335+00:00`，token 来源通过，Billing API HTTP 200，可见结算账号 0、没有下一页。仅表示该时点的可见性；未创建结算账号或申请赠金。
- 主代理最终离线 runner：`live-stop-offline-final.json`，282 条通过、0 failure/error/skip。
- writer 曾有统一 26 条中 25 通过、1 条前置执行器 DEPENDENCY_UNAVAILABLE；原始 `completion-final-green.log` 文件名不等于结果通过。另一次 fixture 的 session 插入依赖失败，另一次新 batch 测试误复用旧 request_key；均保留原始日志，未放宽业务超时。
- 主代理页面/协议回归：`live-read-ui-final.log`，150 条通过，326.310 秒，退出码 0；覆盖原页面、旧 DTO 只读兼容、任务记录投影和 runner。框架已有 deprecation/resource warning 保留，没有当作本次顺手重构理由。
- 主代理 PostgreSQL/停止/读取组合：`live-stop-read-root-final.log`，130 条中 129 通过、1 failure、0 error，302.280 秒，退出码 1。失败为 `test_failed_logout_is_not_cloud_verified` 的执行器前置结果：实际 `DEPENDENCY_UNAVAILABLE`，预期 `CLEANUP_REQUIRED`，尚未调用收尾函数；不能宣称组合验收通过。
- 脱敏诊断 `db-boundary-diagnostic.json` 重现：第二轮出现心跳事务 `55P03`，同时 `_record_sync` 事务约 2235ms。该证据表明锁等待，但仅凭时间重叠尚不能断言阻塞者；继续捕获 victim/blocker PID 及固定等待类型，不输出 SQL、参数或原始上下文，不放宽数据库超时。
- 没有重启 8799、没有迁移 preview、没有真实云写入、模型调用或调度启用；本轮重启授权已异步询问，未获答复前不执行。

### 数据库短事务通道修复

进一步诊断在三个有界用例中捕获了 13 条 `pg_blocking_pids` 记录：同一 run 的心跳被主链 guard/record 阻塞，主链也被心跳阻塞，阻塞者处于提交阶段的 WALSync/WALWrite。该三轮没有再次出现 55P03，不能涂掉先前失败；它们证实了内部竞争机制，而不是证明所有数据库故障都同因。

`async_runtime.py` 为每次 `run` 创建独立 ContextVar/asyncio.Lock；`live_executor._db`、许可消费、密钥两次消费的 `settled_thread` 共享此短事务通道：

```text
MAIN DB  [ transaction + COMMIT + join ] ---------------------->
HEARTBEAT           [ queue in Python ] [ own transaction ] -->
HTTP     [ outside database lane: heartbeat may continue ] --->
```

不再让同一执行器自己同时打开两个争抢同一任务锁的事务。锁只覆盖线程启动到真实结束，不包住 HTTP、令牌调用或轮询；不同执行器不共用全局锁。排队时取消不启动线程，已经启动时取消则等待线程结束再释放。run 成功、异常、取消均同步恢复上下文。原 2 秒 lock_timeout、5 秒 statement_timeout、30 秒租约、提交未知处理均未放宽；不自动重试外部请求。

无表字段或迁移变更，影响的是事务调度顺序。旧“披露线程持有通道时等心跳在数据库报错”的测试夹具发生自等待，已改为真实的排队 barrier → 释放消费线程 → 心跳失败 → 禁止私钥 POST，仍断言 logout 与 UNKNOWN 保留。

- `db-lane-red.log`：新的确定性离线用例先红，保留原日志。
- `db-lane-pg-counterfactual-red.log`：仅测试进程取消通道，真实任务锁保持超过原 2 秒，心跳出现 **55P03**，1 条按预期失败。
- `db-lane-pg-root-green.log`：生产实现下同一真实 PG 用例 **1 / 1 通过，3.434 秒**；无 provider 请求、无 journal。
- `live-lane-offline-root-final.json`：主代理 **291 / 291 通过、0 failure/error/skip**，16.669 秒测试时间。
- `live-lane-pg-root-interrupted-before-fixture-fix.log`：收到旧夹具不再符合新排队规则的报告后主动中止组合（退出 130），不是成功验收。
- `disclosure-lane-fixture-red.log` 保留旧披露夹具的 TimeoutError；修正排队 barrier 后 `disclosure-lane-fixture-green.log` 单例通过，21.757 秒。没有改生产超时或删除零 POST 断言。
- `live-lane-pg-root-final.log`：最后生产代码和夹具修改后，主代理统一重跑停止收尾、执行器、真实锁回归、密钥存储、计划/证据、持久许可、任务读取、事件、HTTP 和 runner，**176 / 176 通过，519.917 秒，退出 0，无跳过**。配套 JSON 保留源文件与日志 SHA-256。使用真实隔离 PostgreSQL 与 MockTransport，不是 Google / 银行 / Sub2API 真实端到端。
- `live-lane-runner-final.log`：显式模块覆盖等 **10 / 10 通过**，1.557 秒。新增离线/PG lane 模块均已加入对应套件，避免漏跑。

这一组合通过不抹去原 130 条失败、旧全量 1282 条失败或主动中止记录；没有重新完成产品全量上线验收，也没有更改上文的原页面启动、真实资料和首次开通缺口。
