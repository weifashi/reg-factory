# Pool 任务：暂停、请求取消、重新核验

当前为隔离合成环境中的服务入口，**尚未接入页面、HTTP 或执行器，不代表 Google Cloud 自动开通已完成**。本模块只接受管理请求，不证明 worker 停止，也不执行核验。

```text
pause   -> PAUSED（原 CONFLICT 保留）       -> 不释放邮箱/信用卡占用
cancel  -> cancel_requested=true           -> PAUSED / CONFLICT，不写 CANCELLED_SAFE
recheck -> 本任务当前代唯一 pool.inspect   -> 不恢复任务、不清观测、不执行外部调用
                |
                +-> task version + 1 -> receipt + audit -> 确认 COMMIT 才返回
same key + same body -> 原 receipt         -> 不重复写入
same key + new body  -> IDEMPOTENCY_CONFLICT
```

## 调用与业务规则

- `pause/cancel/recheck(settings, actor, task_id, expected_version, request_key, *, policy, mac)` 自己管理完整事务，成功只返回 `receipt_id`、`phase=SUCCEEDED`。SUCCEEDED 表示“请求已受理”，不是任务完成。
- 先核验输入、隔离 policy 与 MAC，随后以任务优先锁顺序验证历史归属及实时 `tasks:manage` 权限。不信任 Actor 缓存权限，不因当前凭据到期或邮箱禁用就禁止管理历史任务；损坏的归属关系仍拒绝。
- 新 key 需要准确任务版本；版本加一，最大 bigint 拒绝。已有同 key 回执先于版本冲突判断：重放不再次加版本或审计，旧代合法成功回执仍可核验。
- `pause/cancel` 对 SUCCEEDED、FAILED_CONFIRMED、CANCELLED_SAFE 终态拒绝新请求。pause 不清已有取消标记，cancel 不把取消请求冒充安全取消。
- `recheck` 支持所有合法任务状态，只建立或引用当前代 `pool.inspect`。已有步骤的状态、fence、时间及观测保持原样；不重置 UNKNOWN/CONFLICT，不把新 marker 当核验结果。
- 历史 INTENT/UNKNOWN/CONFLICT 回执原样保留；不修改 lease、邮箱平台状态、信用卡或秘密正文。
- 外部副作用没有发生在这些入口中。遇 COMMIT_UNKNOWN 可能已提交，不能报成功或换 key 自动重试；先核对状态，必要时明确使用相同 key/body 重放。

## 表字段影响（无 DDL）

| 表 | 写入范围 |
|---|---|
| onboarding_tasks | 新请求 version、updated_at；pause/cancel 可变 status；仅 cancel 将 cancel_requested 置 true。generation/current_step 不变 |
| task_steps | 仅 recheck 首次新增本任务当前代 pool.inspect，NOT_SENT、fence=0；已有行不更新 |
| operation_receipts | 新增 task-scope `pool.command.pause/cancel/recheck`，SUCCEEDED、COMMAND_ACCEPTED、fence=0、当前 generation；summary 恰含 task_id/command/accepted_version/status/cancel_requested/generation/inspect_step_id |
| audit_events | 新增 task.pause/cancel/recheck，ACCEPTED，receipt_id 作关联；前后摘要仅 status/version |

所有候选写入处于同一事务；插入冲突、权限失效、MAC 变化、返回行不合规则整体回滚，不允许把候选写入后的冲突伪装成重放。最后固定顺序 policy → MAC 稳定性 → 实时认证，随后不再执行业务 SQL，退出事务确认提交后才返回。

## 页面与回退

现有 B3 页面没有新增按钮；其原有 fixture 路由行为未改，不可把本模块的内部入口当成已上线页面。未来接入须再验权限、幂等 key 和展示语义，取消按钮应显示“取消请求已受理，等待停止核验”。

回退先停用调用方，再撤本模块及测试登记，不需要数据库结构回滚。保留已受理回执、审计、版本与检查 marker；不得删除未知记录、清占用或重置版本来伪装取消完成。最终运行结果见独立验证记录，当前历史失败未被静态审查关闭。
