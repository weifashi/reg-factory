# P1c Task4a：合成邮箱导入运维边界

## 已接通什么

仅新增两个内部 Python 服务，不新增 HTTP 路由、页面、秘密读取接口或任务调度入口：

```text
preview_import(settings, actor, text, group_ref, *, vault, mac)
    └─ 文件解析 + 合成资料校验 + 实际数据库身份复验 → 安全预览与整批摘要
import_text(settings, actor, text, group_ref, preview_digest, request_key, *, vault, mac)
    └─ 同批重算 → 原子写入或整批拒绝 → COMMIT 成功后返回固定结果
```

仅接受既有隔离 PostgreSQL 实例、带私有 manifest 的临时 schema2、真实操作员会话及 `mailboxes:manage` 权限。权限从数据库复验，不信缓存 Actor；不自动给现有操作员授权。Vault 与 MAC 必须为精确受控类型，并绑定同一私有密钥目录和 Settings。该服务不能用于真实账号资料或生产库。

## 操作和结果

| 操作/输入 | 结果 |
|---|---|
| 预览合格的合成文件 | 返回行号、规范邮箱、原解析器 provider、分组、固定错误码、源文件计数、整批摘要；不写数据库资料、秘密、回执或审计 |
| 看到 `accepted_count` | 只是源文件解析得到的条数；不是“本次一定能导入”的承诺。预览不查询数据库既有邮箱、不区分新增/跳过，不核验实际邮箱健康或注册历史 |
| 含非空 `account_password` 及对应旧别名 | 预览报 `PLATFORM_BINDING_REQUIRED`，确认整批拒绝。不猜 Google、不覆盖邮箱密码、不静默丢字段。后续显式平台归属仍需实现，这是首批临时边界而非永久产品限制 |
| 文件中坏行、重复邮箱、冲突、超限、未知字段 | 确认整批拒绝，不部分导入。沿用原解析器 256 KiB/1000 候选限制；合成字段校验还可能拒绝解析成功的资料 |
| 本人已有邮箱、凭据指纹相同 | 跳过，返回已有 ID；不改变原分组、凭据版本、健康、历史、租约或其他字段 |
| 本人已有邮箱、凭据不同或旧指纹无法验证 | `VERSION_CONFLICT`，整批回滚，不进行覆盖或解密比较 |
| 邮箱全局已属另一操作员 | `FORBIDDEN`，整批回滚，不返回对方 ID、姓名或资料 |
| 本人新邮箱 | 一个加密秘密、一条邮箱、七条保守平台状态与终态管理回执同事务写入 |
| 凭据/分组/条目改变，却沿用已有请求键 | `IDEMPOTENCY_CONFLICT`；不能借请求键复用旧结果 |
| 相同键、相同规范请求重放 | 会话和权限重新核验后，返回原回执中的 created/skipped IDs；不再写资源或审计 |

`group_ref` 最长 128 字符，不接控制字符。`request_key` 为 1～128 字符的字母、数字、点、下划线、冒号、连字符。所有输入仍受现有类型与安全限制。provider/source 继承旧解析器规则；`icloud` 映射 `icloud`，`outlook/graph/microsoft` 映射 `outlook`。不能据此宣称 Gmail、IMAP 或新的取码渠道已经实现。

## 数据库影响：复用五张现有表，无新增表或字段

前置 schema2 来自既有 002 池迁移。本次服务不修改 SQL、不执行生产迁移。下表区分直接写入和既有默认值；跳过分支不修改资源行。

| 表 | 本次新建记录的字段和值 |
|---|---|
| `secret_objects`（复用 Vault） | 直接写 `id`、`kind=mailbox_credential`、`key_version`、随机 `nonce`、加密 `ciphertext`、服务器构造的 `access_policy`；`revision=1`、`version=1`、`expires_at=NULL`、`revoked_at=NULL`、`created_at/updated_at` 由既有默认值产生。无明文秘密暂存 |
| `mailbox_registry` | 直接写 `id`、`owner_operator_id`、`email_norm`、`source_type`、`group_ref`、`credential_ref`、`source_fingerprint`。既有默认值：`credential_version=1`、`health=UNKNOWN`、`disabled=false`、`ever_registration_attempted=false`、`sale_eligibility=UNVERIFIED`、`pool_status=AVAILABLE`、`last_used_at=NULL`、`version=1`、创建/更新时间 |
| `mailbox_platform_states` | 每邮箱七行，直接写 `id`、`mailbox_id`、`platform`、`usage_status=HISTORY_UNRECONCILED`。平台为 google/claude/chatgpt/grok/kiro/github/k12。默认 `identity_status=UNKNOWN`、`credential_ref=NULL`、`credential_version=1`、`last_task_id=NULL`、`evidence_ref=NULL`、`checked_at=NULL`、`version=1`、创建/更新时间 |
| `operation_receipts` | 直接写 `id`、`task_id=NULL`、`scope_operator_id`、`action=mailbox.import`、`resource_revision=pool-admin:<owner UUID>`、`idempotency_key`、服务器重算 `request_hash`、`phase=SUCCEEDED`、`fence=0`、`generation=1`、`result_summary`。summary 仅 `created_ids`、`skipped_ids`、`request_key`；`result_code/external_ref=NULL`，`version=1` 及时间使用默认值 |
| `audit_events`（复用 append） | Vault 新秘密写 `secret.put`，服务新回执写 `mailbox.import`。直接写 `actor_id`、`task_id=NULL`、固定 `action`、UUID `object_ref`、固定 `outcome_code`、UUID `correlation_id`、空 `before_summary`、仅 `version:1` 的 `after_summary`；`id/created_at` 使用既有默认值。无邮箱、密码、原文或自由错误文本 |

`ever_registration_attempted=false` 仅表示本地尚无注册事实，不能解释为证明“从未注册”。导入不产生 `NEW_CONFIRMED`、`HEALTHY`、`ELIGIBLE` 或平台 `SUCCEEDED`，不覆盖任何既有已知历史。

## 事务、并发和回执

```text
有界解析/类型校验/摘要
        │
        ▼
真实目标 + operator/session/权限复验
        │
        ├─ 已有同请求终态回执 → 严格校验 JSON/UUID/数量 → 原结果
        │
        └─ 规范邮箱排序 → 查/锁已有邮箱 → 同凭据跳过
                           └─ 新建 savepoint：secret + audit + mailbox + 七平台
                              └─ 仅指定 email 唯一约束竞态可回滚候选后读胜者
        │
        ▼
所有资源处理完 → 最后写终态管理回执 → 固定审计
        │
        ▼
目标/会话/权限/密钥复验 → COMMIT → 返回结果
```

其他 SQL、审计或安全检查失败都整批回滚。不同请求内容的回执竞争败方必须抛错并回滚本事务候选资料；不能直接返回别的请求结果。相同请求的竞争结果必须收敛到同组资源 ID，不能留下跳过造成的孤立秘密。

回执严格校验固定键、UUID 列表、数量、请求键和终态管理元数据，不把任意数据库 JSON 透传。提交确认丢失时保留 `COMMIT_UNKNOWN`，不自动重试、不把未知报告为成功或确定失败。调用方应保留原请求键/原规范输入；恢复后显式同键重放，由持久回执判断既有结果，不能另造键重新执行。

## 摘要和密钥生命周期

- 每条规范 typed mailbox JSON 使用 owner 绑定的 `mailbox.credential.v1` MAC，持久化到 `source_fingerprint`；不含原行号或分组，不下发逐行 MAC。
- 按规范邮箱排序，将每条 32-byte MAC 聚合；批次头绑定分组、版本、固定 skip 模式及条数，再用 `mailbox.import.v1` MAC。允许源文件大于 64 KiB，不放宽现有 RequestMac 的 64 KiB 单消息上限。
- AES 密钥与 RequestMac 密钥物理分离；没有丢失密钥时的裸 SHA、随机新键或自动接受旧指纹回退。
- 本次操作固定 AES 版本/材料，并复验 MAC 域探针，覆盖导入、预览、全部跳过及回执重放；检测到单向材料变化则拒绝/回滚。
- 不声称能防同 OS 权限攻击者的任意 ABA 换回，也不等于已实现生产密钥轮换。MAC 密钥关乎历史凭据等值和回执摘要；不得无协调替换后继续接受历史资料。生产密钥保管、轮换与历史指纹迁移仍待实现及验证。

## 停用/回退

当前没有线上入口或生产迁移可回滚。若需撤销这批服务，先停止调用方和隔离测试进程，再仅撤销本批服务/测试/文档、runner 注册以及单个审计动作注册；保留此前 Vault、解析器、schema2、认证和其他用户改动。不要整仓 reset、删除工作树或退回已有迁移版本。

若已有隔离实验数据需核对，先保留回执、审计和关联密钥，以便区分已提交/未知提交，不能先删 key 或手改指纹/回执。仅由现有 manifest 校验工具清理本次归属的临时 schema 和测试密钥目录；不清空永久隔离 schema、其他测试任务或生产数据。将来生产启用需另行制定经过批准的备份、密钥保留和恢复验证方案。

具体测试、独立审查和最终验收证据见当批验证记录；本说明不宣称全量测试已绿，也不构成生产交付。
