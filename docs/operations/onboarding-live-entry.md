# 2026-09-30 接线进展索引

下文保留此前内部入口阶段的历史记录。原 HTTP/BOOT/页面接入的后续实现与最终验证请看 [原控制台真实入口接线](onboarding-live-web.md)。不能把本轮接线测试解读为首次注册、支付、赠金或整体验收完成。

---

# 真实任务内部入口与原邮箱排他边界

日期：2026-09-29。**这是代码接入记录，不是上线完成或真实云联调通过记录。**

## 这次做了什么

```text
原 emails.txt（授权的精确邮箱）
        |
        +-- 共享 OS 管理权边界 -- 原领取/导出/扫描/恢复也必须遵守
        |
        v
原 mailbox_registry + secret_objects（密文只存一份）
        |
        v
固定 gcloud 身份 + 官方 Billing GET
        |  只证明指定已有结算账号可见；不证明试用/赠金/费用批准
        v
短时、同进程/同会话 admission
        |
TX1  INSERT live task + Google RESERVED + mailbox lease + 独立回执
        |
        +-- COMMIT 不确定 -> 不启动，不据此重试外部操作
        v
TX2  原始 hold 交接 + 固定配置派生 plan
        |
        +-- COMMIT 不确定 -> 不构造执行器
        v
确认提交后构造 LiveExecutor
        |
        v
LiveRunManager 持有 run_managed（仅内部组件，原页面 HTTP 尚未挂载）
        |
        +-- 取消 / 关服 -> 等待 DB 线程和 finalizer，不把 HTTP 断开当回滚
```

仍在同一 reg-factory 工作树复用现有表和执行器，没有另建一个产品。

## 规则和字段影响

没有新增数据库表/字段/迁移；本机正式预览数据库未迁移、服务未重启。

|位置|本轮写入/核验|没有做的事情|
|---|---|---|
|`mailbox_registry`|核验 owner、原凭据引用/版本、source_fingerprint、停用与健康状态|不把 UNKNOWN 写成 HEALTHY，不改原密码|
|`mailbox_platform_states`|Google 使用同一邮箱 secret_ref/version，HISTORY_UNRECONCILED → RESERVED，固定 last_task_id|不新建独立 Google 密码，不把历史洗成 UNUSED，其余平台不动|
|`onboarding_batches`|首批精确指定单邮箱、固定全局配置版本|不允许批次覆盖配置，不假装已支持任意多账号真实开通|
|`onboarding_tasks`|新记录明确 INSERT execution_mode=live；QUEUED/version2 → handoff RUNNING/version3|不把旧 synthetic 记录 UPDATE 成 live|
|`resource_leases`|使用原 mailbox hold/fence/version；只转交证明充分的未启动 hold|超时不自动释放，不拿进程失联当停止证明|
|`operation_receipts`|独立 `live.batch.create_one`、`live.command.pause/cancel` 命名空间及 MAC|不冒充旧 `pool.batch.create`，重放只观察，不再启动|
|`live_run_plans`|必须严格等于任务 pinned v2 配置经 plan_for 派生的计划|新全局配置头不覆盖旧任务计划，不接受任意项目/结算覆盖|
|`audit_events`|复用固定 task.create/lease.claim/task.pause/task.cancel 审计|不记录密码、token、银行卡或原始 provider 错误|

新增本地管理权控制文件：

- `<原 data-root>/.managed-mailbox-boundary.lock`
- `<原 data-root>/runtime/state/managed_mailbox_ownership.json`

后者只记录邮箱摘要和 managed/legacy-exposed 管理权。它不是第二个邮箱库存，也不是云资格证据。首次登记检查旧领取/导出/恢复等历史；已有凭据曾被旧消费者交付时，不能自动推断已经无人使用。

**跨平台边界：** 目前纳管邮箱由新持久任务体系独占管理，旧文件领取器不再交付它；这保证不被两套领取器同时消费，但不代表所有旧平台执行器已迁入新租约体系。完整跨平台复用仍须后续接入，不能把本轮限制永久描述为需求已经全部完成。

## 暂停和取消

`live_commands` 只记录 PAUSED 与单向 cancel_requested，不释放邮箱、不取消已经发生的外部结果。实际执行器停止后，原 stop-proof finalizer 核验：

- 无外部效果且停止证据完整：安全释放，并恢复原 `HISTORY_UNRECONCILED`。
- 不能核清、出现外部效果或未知提交：保持占用，等待核验。
- 云资源链验证成功：仍不宣称试用、赠金、整个开通流程成功；Sub2API 保持停用，不调用收费模型。

## 旧流程影响

原 next/latest/retry、显式注册标记、资产单条/批量导出、Outlook 恢复、实际 provider 扫描，以及独立 register/K12 调用方使用同一边界。只读列表/统计不等于消费。凭据交付或 provider 操作前先持久记录旧消费事实；失败不允许绕过或吞掉管理权拒绝继续注册。

所有参与进程必须升级并使用同一个物理 data-root。未升级进程、跨主机、任意复制材料、同 UID 删除整套控制文件，不在此 OS 锁保证内。不要为了恢复一次失败导入而删除控制文件：DB 回滚/提交不明时，持久管理权有意不自动释放。

该管理边界仍是单 JSON 全量持久写、读取上限 8 MiB；永久 legacy 标记不按 TTL 清理。本轮批量过滤只减少重复读取，最后交付前仍重读并落盘，不提供无限容量、自动迁移或自动解除。K12 备份遇到任务中的明确结构化凭据字段会整体拒绝，不是对任意日志文本做秘密检测。

## 原请求重放、历史查询与后台生命周期

- `live_batches.observe_request` 先按原 owner/key、请求 MAC 和固定任务证明观察回执。已提交请求不重新预检、占邮箱或启动；未查到仅是当时观察，不证明旧事务回滚。
- `live_read` 保留原任务和日志白名单 DTO。已取消、凭据过期、租约释放或全局配置头更新，都不应抹掉原任务历史；当前权限、归属、原 v2 配置和新创建证明仍要核验。列表包含原 fixture/synthetic 历史，未知模式不悄悄跳过。
- `live_commands` 原 key 可以在真实安全释放后核对原暂停/取消回执；新 key 仍走严格资源图，不能借历史回执获得新执行权。
- `LiveRunManager` 固定一个服务端登记的邮箱与依赖；进程内只允许一个启动/执行任务。请求取消前尚未移交的启动线程必须等完，不能在后台迟到启动。已移交给 manager 的执行任务不因 HTTP 请求消失而失去管理。
- 关服会先禁止新启动、取消自有任务，再等待数据库线程与实际停止核验。已经进入数据库的提交仍可能完成；此时保留占用等待核对，不声称回滚。只有本地槽位释放，不等于数据库资源安全释放。
- `Submission.result` 和创建回执的完整 summary 是内部对象，含引用/摘要，后续 HTTP 必须重新白名单投影，不能原样返回。

**页面 → 操作 → 结果：** 本轮没有改原页面或 HTTP 挂载；8799 上点击现有按钮不会自动使用这些新内部服务。右侧实时状态、配置编辑和运行路由还需完成组合验收，而不是靠改文案隐藏这个差别。

## 当前未交付项

1. 原页面 live HTTP DTO/路由、全局 v2 配置编辑，以及新历史读取/后台生命周期组件的启动挂载、右侧日志与整体验收。
2. 首次开通的真实联系人保存、支付 iframe、授权验证码渠道、官方试用与 credits 证据、代理出口的完整联调。
3. 任意批量账号、多平台执行器统一租约、跨进程受控恢复、备份恢复及完整上线验收。
4. 真实 Google/结算/Vertex/Sub2API 联调和另行获批的 Gemini 费用测试。

当前 8799 页面不会因内部代码落盘自动升级；没有以删除“测试/未接入”说明来冒充真实接通。

## 暂停接入与回退边界

本轮没有上线动作，因此无需对预览库执行降级迁移。若后续挂载后需要回退，先关闭新任务入口、等待 manager 关服完成并逐项核对未决任务；保留数据库回执、租约、密文与管理权文件。不能删除 JSON/lease 后重新领取，也不能仅回退旧消费者代码使其无视已纳管邮箱。涉及外部项目、服务账号、密钥的清理需按确切回执和单独授权处理，页面取消不负责外部回滚。

## 验证记录

原始证据目录：`/workspace/gcloud/.local/live-entry-evidence`。

- `entry-create-pg-second.log`：第一版新增入口 8 项真实隔离 PostgreSQL 通过；HTTP/token 为测试替身，没有云写入。
- `entry-runtime-regression-first.log`：84 项初轮 **失败**，4 failures/1 error。含一个根命令模块名错误、跨平台夹具断言、原 PG 锁等待夹具不再适用于 OS 先锁，以及一个待诊断的 executor DEPENDENCY_UNAVAILABLE。该失败记录保留，不算验收通过。
- `entry-whole-offline.log/json`：一次 356 项离线通过；之后仍有代码改动，最终验证以末次重跑为准。
- Actor 别名、取消线程未 join、提交尾检、启动依赖顺序的 RED/GREEN 日志同目录。
- common/caller 子任务证据在 `/tmp/rf-managed-mailbox-evidence` 与 `/tmp/rf-legacy-caller-evidence`；代理结果不替代根最终联合验证。
- 根 `entry-whole-offline-final.log/json`：2026-09-29 16:54 UTC，397 项通过、0 skip；不代表 PG 或外部验收。
- 根 `legacy-final-root.log`：165 项共享边界/调用方及旧消费者回归通过；外部 provider 为测试替身。
- 根 `entry-pg-final-core.log`：141 项中 1 failure / 1 error，尚未验收；其中一次初始 fixture 迁移失败、一次消费线程门限 60 秒未到达。两例各精确诊断两轮均通过，但原失败未复现、根因未确认，不能说已修复；详见 `core-failures-diagnostic-conclusion.md`。未调大超时或改生产断言。
- 启动依赖前验导致旧故意不完整的 stop-proof fixture 缺少 keyring，已修正仅测试的验证 mock；生产依赖检查未放宽。
- `live-new-composition-pg-first.log`：最新历史读取/原键观察/后台组合等 44 项首轮中只有 1 个测试错误：测试把配置写服务返回的 `config_id` 写成 `id`，在 SQL 前 KeyError；`observe-config-diagnostic.log` 已定位。仅修测试取值，整组重新验证；3 项真实 manager + DB + stop/finalizer 组合首轮已通过，不代表 HTTP 或云验收。
- 根 `live-new-composition-pg-final.log`：修正测试取值后，最新 44 项完整重跑通过，341.552 秒、0 skip；包含正式 source→admission→双提交→manager→真实停止核验，以及两次真实 COMMIT 后 lost-ack 的零调度与原键观察。它不替代上面 141 项回归失败的未决诊断。
- `live-entry-runner-final-with-manager-db.log`：测试入口覆盖检查 10 项通过；本轮 Python 编译与 `git diff --check` 通过。未跑浏览器页面验收（本轮未挂载新 HTTP/UI），未执行真实云写入或收费调用。

尚未以这一切片宣称达到上线标准。
