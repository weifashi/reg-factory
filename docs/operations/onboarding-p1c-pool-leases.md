# Pool 邮箱逻辑占用与心跳

本模块防止同一邮箱被多个任务并发操作；**占用成功不代表能注册、能登录或能消费秘密**。只支持隔离合成数据、既有单平台任务，无真实执行器、HTTP或新页面按钮。运行验收以独立验证记录为准。

## 三个入口，各做一件事

```text
existing page -> existing fixture routes                  [unchanged]
trusted UoW  -> task -> auth -> mailbox/state -> lease
                              |
              claim ----------+-> mailbox secret metadata -> audit
              assert_current -+-> binding checks only
              renew ----------+-> live TTL update -> audit
                              |
                  final policy + locked row snapshots
                       -> live auth + last DB clock
                       -> provisional return
caller confirmed COMMIT -> report success (NOT an execution permit)
```

- `claim`：任务须为 QUEUED/PREFLIGHT、没有取消标记，邮箱启用且HEALTHY/AVAILABLE，平台usage为UNUSED，last_task为空或本任务；没有另一活动任务、没有任何逻辑hold、没有未决步骤/回执。只检查当前邮箱秘密有效，平台秘密仍按用途另验。
- `assert_current`：只确认当前任务、邮箱、平台、双凭据版本、owner标签、fence与未到期租约匹配，返回None；不写业务表、不发执行许可。
- `renew`：已存在的有效占用续5–60秒，默认30秒；过期不能复活。默认保留暂停/待人工/冲突/取消标记下的占用，不自动恢复任务。

三入口都需要实时tasks:manage、同owner及精确隔离policy，不能用缓存权限、旧fixture token、任意字典代替。combined/card本段不支持；owner标签不是worker身份证明。输入秘密引用、版本相等也不是执行授权。

## 容易混淆的业务规则

| 情况 | 实际结果 |
|---|---|
| 平台身份UNKNOWN/NEW_CONFIRMED，平台秘密暂为空 | 其他条件满足时可取得逻辑占用；不确认账号不存在、不启动注册 |
| EXISTING却没有平台秘密 | 拒绝新占用，需后续人工处理；不拿邮箱密码冒充平台密码 |
| 平台秘密过期/撤销 | 不单独禁止逻辑占用/心跳；实际登录消费必须另行拒绝无效秘密 |
| 邮箱秘密过期/撤销 | 拒绝新claim；已有live hold可以保持心跳，不因此允许邮箱认证 |
| 邮箱禁用、导出、健康/usage变差，任务暂停或待人工 | 不单独阻断已有live hold心跳，不清未知事实、不继续业务 |
| 当前邮箱/平台ref、版本或身份与任务pin不同 | 拒绝当前绑定，保留hold，不热换任务历史pin |
| TTL到期、旧fence、owner/task不匹配 | 拒绝续租；不能拿“到期”当作可抢占或已停止证明 |
| 任务已安全终态 | 不新claim、不续租，不自动删除历史hold/fence |
| COMMIT回执丢失 | 可能已提交；不返回成功、不暗重试，先核对真实状态 |

## 动哪些字段（没有新增表或列）

- claim：`resource_leases.task_id/owner_id/fence/lease_until/hold_reason/stopped_evidence_ref/version/updated_at`；`onboarding_tasks.version/updated_at`；新增一条`lease.claim`审计。新lease默认行可先在事务内建立，后续失败须整体回滚。
- renew：仅`resource_leases.lease_until/version/updated_at`及一条`lease.renew`审计。task版本、generation、fence、hold_reason与双pin均不改变。
- assert_current：只读与锁定，不新增审计或回执。三入口都不改邮箱/平台状态、配置、秘密正文、卡、预约或旧业务回执，不DELETE历史。

调用方拥有完整事务，遇任何异常必须退出并回滚，不能吞掉异常提交半成品。最终检查使用一次有限流程：policy、已锁行快照、实时认证、最后一条DB clock；同一时刻检查session、lease与claim邮箱秘密期限，之后不再SQL或外部I/O。这个时点不是未来COMMIT或消费者执行时刻，后续用途入口仍须独立复验。

## 接入与回退

当前用户页面操作前后完全相同；新入口仅供未来受信调用方。不能持mailbox/session锁后反调task-first入口，也不能循环多任务反锁；未来新批次须有独立原子创建路径。无释放、恢复成功、停止证明、combined子步骤或真实provider能力。

回退先停止新调用者，再撤入口及测试登记；无需DDL回滚。已形成的lease、fence、task版本与audit必须保留，不通过删除占用或重置计数“回滚”，不以TTL推断外部操作停止。确认提交失败与COMMIT_UNKNOWN必须分开记录。
