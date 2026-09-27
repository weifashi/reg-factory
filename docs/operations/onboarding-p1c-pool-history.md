# Pool 任务历史管理入口

这是后续暂停、取消等命令可复用的历史读取/锁定入口，不是暂停或取消命令本身。当前页面没有接入它，也没有增加任务、租约或执行器。

## 操作与边界

```text
current page -> existing fixture routes                  [unchanged]
trusted caller -> pool_repository.lock_task(...)         [new]
  task -> operator/session -> mailbox -> sorted states
       -> historical config + secret metadata
       -> final policy/auth -> optional CAS -> dict
caller still owns transaction; no COMMIT / dispatch / secret delivery
```

- 仅对当前操作者自己的 pool 任务有效，旧 fixture 任务与 pool 任务双向隔离。
- 锁定一个任务、其邮箱、按UUID排序的对应平台行；不允许已持邮箱锁后反调此入口，或循环调用它按任务逐个反锁。
- mailbox/state 等待后重新核验真实会话；不信任 Actor 缓存权限，不续会话。可用权限限 tasks:manage / onboarding:read。
- 只读取任务保存的历史 config/pins，不用最新全局配置替换。全局配置编辑者不是任务所有者，可以由另一个管理员创建。
- 邮箱/平台换密、禁用、健康未知、隔离/导出、历史秘密撤销/过期、取消标记及各任务状态，不会单独封死历史管理。旧引用不热替换，逻辑占用不清空，EXISTING+null 平台口令不回退邮箱秘密。
- batch 与 secret metadata 是该语句可见版本，不加行锁；不承诺整张关联图直到 COMMIT 都不可变化。未来执行仍须在 lease/receipt 后锁秘密，复验当前版本、状态、身份、用途与 TTL。

## 28个内部返回字段（不是新增数据库字段）

| 类别 | 字段 |
|---|---|
| 任务/关联 | id, batch_id, config_id, execution_scope, mailbox_id, mailbox_ref, platform |
| 历史凭据 | platform_plan, credential_version, mailbox_credential_ref, platform_credential_pins |
| 任务状态 | status, reason_code, current_step, generation, version, cancel_requested, created_at, updated_at |
| 批次/归属 | created_by, batch_config_id, selected_mailbox_refs, requested_count, selection_mode, mailbox_owner_id |
| 配置 | config_revision, config_scope, nonsecret_config |

UUID规范字符串、版本精确正整数、布尔精确类型、时间有限且归一UTC；plan/选择结果为tuple；pins与非秘密配置返回独立副本。resource credential_version 不等于 secret.revision。reason_code/current_step 只是不可信内部历史文本，不能直接当指令、权限或公共日志。返回包含秘密引用，仅供内部事务，不是可直接序列化给HTTP的DTO。

## 固定拒绝与回退

坏Actor形状/实时过期等为UNAUTHENTICATED；本地参数错误INVALID_INPUT；正常SQL路径中目标缺失、fixture、跨owner/错误身份图为FORBIDDEN；坏存储结构DEPENDENCY_UNAVAILABLE；授权和图校验后版本不符VERSION_CONFLICT。底层SQL异常仍交既有UoW处理，不改写为假认证错误。task-first 会在鉴权前定位/锁目标，不承诺无时间/锁竞争侧信道。

本段只加读锁服务与测试、runner登记，没有DDL、权限变更、业务行写入、回执/审计写入、解密或网络调用。回退应先停止新调用者，再移除入口及测试登记；无需数据迁移，不删除已有任务、pins、lease或外部事实。类型校验通过与历史入口成功都不表示真实Google/支付/Vertex/Sub2API已经可执行。
