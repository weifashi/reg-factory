# Pool 原子批次：指定邮箱 / 自动取 N 个

本入口只处理隔离合成数据，创建 Google 任务和逻辑占用，**不注册账号、不执行 Cloud/绑卡、不返回执行令牌**。现有页面和旧 fixture 路由仍未接入。运行验收以独立验证记录为准。

```text
指定 N 个邮箱 UUID / 自动 N
    -> 实时身份 + 本人同 key 回执
       |-- 同 key / 同 body -> 原 batch 与 task IDs（不再占用）
       |-- 同 key / 异 body -> 拒绝冲突
       `-- 新 key -> 全局配置版本核对 -> 足量资格 -> 整批锁定
                        |-- 不足 / 忙 / 不合格 -> 整批回滚
                        `-- batch + N tasks + N holds + receipt + audit
                                 -> 确认 COMMIT -> 请求已受理
```

## 固定入口与规则

`pool_batches.create(settings, actor, selection, requested_count, mailbox_ids, expected_config_revision, request_key, *, policy, mac)` 自己管理完整事务。

- N 是精确整数 1–100，不接受 bool；指定模式必须恰 N 个唯一规范 UUID；自动模式列表必须为空。自动按邮箱 UUID 稳定排序，不足不缩水、不跳过忙资源另补一批，不暗重试。
- 只用操作者本人的 HEALTHY、AVAILABLE、未禁用邮箱；Google 状态必须 UNUSED 且没有 last_task。导入默认 UNKNOWN/HISTORY_UNRECONCILED 不变，导入成功不是健康或历史清白证明。
- UNKNOWN/NEW_CONFIRMED 可暂缺独立平台凭据，这仅允许逻辑排队，不证明可以注册或登录。EXISTING 缺平台凭据拒绝；绝不拿邮箱密码冒充 Google 密码。存在的邮箱/平台秘密均须核验归属、类型、版本、撤销及期限，只读 metadata 不解密。
- 任一平台仍有活动任务，或任何既有 hold/task/owner，即使租约过期也不抢占。孤立期限要求核验，不清 UNKNOWN、不重置 fence。
- 全局配置必须是指定的当前 pool revision，整个批次固定一个配置快照。接口没有批次 model/region/实例覆盖字段；旧同 key 回放不因配置换新版或健康改变而新建任务。
- 同一任务系统已有 task-first 管理锁。新批次不锁旧 task，也不循环调用 claim；先 session，邮箱统一 NOWAIT，检查跨平台活动，避免资源→旧 task 反锁。忙则退出整批，不改数据库超时。

## 原子写哪些表字段（无 DDL）

| 表 | 新批次写入 |
|---|---|
| onboarding_batches | 一行 selection_mode/requested_count/selected_mailbox_refs/config_id/created_by |
| onboarding_tasks | N 行 Google pool 任务，固定 batch/config/mailbox 和双凭据 pins；QUEUED、generation1，建1再占用后 version2；不改旧 task |
| resource_leases | N 个 mailbox 逻辑占用；服务端 owner=batch:<UUID>，HELD，30秒期限；fence/version 各加1，stopped_evidence_ref清空；仅干净空 lease 可分配 |
| mailbox_platform_states | 对应 Google UNUSED→RESERVED、last_task_id=新 task、version+1、updated_at；身份/凭据版本不变 |
| operation_receipts | 一条 admin-scope pool.batch.create、SUCCEEDED/BATCH_ACCEPTED、generation1/fence0、七键批次摘要 |
| audit_events | 每任务 task.create 与 lease.claim 各一条，共2N；统一关联批次 receipt，摘要无邮箱/秘密正文 |

邮箱 registry 本身只锁不写；不改变健康、售卖资格、注册尝试标志；不操作卡或预留，不调用旧消费者或真实 provider。所有写入一个事务，任何失败都不能留下半批。回执七键摘要与返回七键不是同一对象：返回仅 batch_id/task_ids/mailbox_ids/config_id/config_revision/receipt_id/phase。

## 提交、重放与回退

最后固定 policy→MAC 稳定性→实时认证→最后 DB clock，同一时点核 session、锁住的秘密和新 lease 期限，之后不再 SQL/文件 I/O。确认 COMMIT 后才返回；SUCCEEDED 仅表示批次受理，不代表 Google 开通成功。

COMMIT_UNKNOWN 可能已落完整批，不能自动换 key 重试；使用原 key/body 明确核对，合法重放返回原 IDs，零新增占用/审计。30秒到期不等于释放或执行完成；未来 worker 接续/停止证明必须独立实现和验证。

回退先停调用方，再撤入口和测试登记；无需结构回滚。保留已落 batch/task/receipt/audit/hold，不删除未知记录、不把 RESERVED 改 UNUSED、不重置版本/fence 来伪装未执行。当前没有新页面操作，不能将未来“创建批次”按钮说成已上线。
