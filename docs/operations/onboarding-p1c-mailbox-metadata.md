# P1c Task4c：邮箱分组与停用命令

## 范围与入口

内部 `mailboxes.update(settings, actor, mailbox_id, expected_version, changes, request_key, *, policy, mac)`。本切片只允许修改本人邮箱的 `group_ref` 和 `disabled`，没有 HTTP / 页面入口、凭据更新、任务取消、分配器或真实外部操作。验证状态单独记录，不以本文代替验收。

复用真实 `Actor`、实时 `mailboxes:manage` 权限、`SyntheticPoolPolicy` 的私有 manifest/schema2/连接核验、独立 `RequestMac` 和统一 UoW。不需 `onboarding:read`，不信任 Actor 权限快照，不刷新会话 TTL。只接受精确类型和与 policy 相同的 Settings。MAC 目录由受信本机装配选取；Settings 没有目录归属登记，不夸大类型/相等性检查。

不构造 Vault/Keyring，不读取凭据、不解密，不依赖 active AES key。RequestMac 仍保留已有 AES 候选材料独立性检查；“不解密”不是“不访问任何密钥文件”。仅合成隔离环境，不是已开放生产邮箱管理。

## 操作 → 结果

| 内部调用 / 将来页面操作 | 当前服务行为 |
|---|---|
| 修改分组 | 只更新 group_ref；空字符串清空，不自动 trim，不改平台状态 |
| 停用邮箱 | 设 disabled=true；不取消正在执行的任务，不释放租约，不改 health |
| 恢复邮箱 | 设 disabled=false；不恢复 ELIGIBLE，不清除使用/注册历史 |
| 邮箱被占用时改分组/停用 | 可改这两项非凭据元数据；任务、租约、凭据 pin 等保持原样 |
| 同值、不同 request_key | 是新管理命令，版本 +1、更新时间刷新、写一份回执/审计 |
| 同 key、相同目标/原版本/内容 | 返回原最小成功回执；不重复更新、不重复审计 |
| 同 key、改目标或原版本或内容 | IDEMPOTENCY_CONFLICT，所有候选变更回滚 |
| 不同 key、旧 expected_version | VERSION_CONFLICT；调用者先重新读取，再决定是否修改 |
| COMMIT 确认丢失 | COMMIT_UNKNOWN；不自动重发，显式同键核验以已落库回执为准 |

“停用后不再分配”需要后续 Task5 分配器锁内检查 disabled；本入口本身不证明调度已接入。页面 `webui/static/onboarding.html` 仍是离线控制面，没有本批邮箱按钮。

## 输入与返回

- mailbox_id：小写规范 UUID 字符串；foreign / 不存在统一 FORBIDDEN，不泄露所属者或当前版本。
- expected_version：精确 int，1..9223372036854775807，bool 不合法。
- changes：精确非空 dict，仅 group_ref 和/或 disabled；未知字段直接拒绝，不悄悄忽略。
- group_ref：精确 str，最多128字符，合法 UTF-8，无 Unicode Cc/Cf，允许空字符串。
- disabled：精确 bool，0/1/字符串不替代布尔值。
- request_key：精确 str，`[A-Za-z0-9._:-]{1,128}`。
- 返回和终态回执摘要均仅 `{mailbox_id, version, request_key}`；不返回原始邮箱行或自由分组内容。
- 当前版本 bigint 上限时新命令 VERSION_CONFLICT，避免 SQL 溢出；已成功旧命令仍可先按回执重放。

摘要使用 `mailbox.update.v1` 域，规范 JSON 包含 `v=1,schema,instance_marker,mailbox_id,expected_version,changes`；owner 由 RequestMac 帧绑定。request_key 只用于定位回执，不混入内容摘要。先确认精确 dict 后立即复制，再验证同一副本；该已验证快照用于摘要和 SQL，避免调用方 dict 在校验期间或后续步骤变化绕过校验、导致两者不一致。

## 事务、并发与回执

```text
strict inputs + isolated policy + stable MAC
  -> BEGIN
  -> operator SHARE -> session UPDATE -> mailbox FOR UPDATE
  -> revalidate live identity after lock wait
  -> receipt exists? -- yes --> validate fixed receipt -> original result
  | no
  -> expected_version CAS -> candidate metadata UPDATE
  -> terminal receipt INSERT -> version-only audit
  -> final policy + stable MAC + live identity
  -> COMMIT -> return
```

先查回执、后比较当前版本；否则成功后重放旧 expected_version 会误报冲突。回执固定 task_id=NULL、本人 scope、action=mailbox.update、resource_revision=pool-admin:本人、generation=1、fence=0、phase=SUCCEEDED。存储元数据/摘要严格校验，损坏数据固定 DEPENDENCY_UNAVAILABLE，不回显 JSON。

同邮箱同键并发由邮箱行锁串行化；同邮箱不同键同版本只有一个 CAS 胜出；不同邮箱共用同键由管理回执唯一索引仲裁，输家包括 updated_at 在内全部回滚。不存在临时占坑回执、假 Google 任务或覆盖式 UPSERT。已做候选更新后出现同 hash 回执冲突是不应出现的情况，保守失败回滚，不提交无审计改动。

每次成功新命令写一个 mailbox.update 审计，object_ref=邮箱 UUID，correlation_id=回执 UUID；before/after 仅版本。审计失败、实际 SQL 错误、最终隔离/密钥/权限/TTL 核验失败均回滚。等待可能发生在行锁、回执或审计写入，因此必须末次重新校验。统一短事务 timeout/fsync/JIT 配置不调整。

## 表与字段

**无新增表、字段、索引、触发器、迁移或权限。** 沿用既有 002：

| 表 / 来源 | 本切片触及字段 | 影响 |
|---|---|---|
| mailbox_registry | 读取 id、owner_operator_id、version；写 group_ref、disabled、version、updated_at | 仅两项元数据及并发版本/时间 |
| operation_receipts | 读取/插入 id、task_id、scope_operator_id、action、resource_revision、generation、fence、phase、idempotency_key、request_hash、result_summary；默认时间列由库生成 | 新增 mailbox.update 终态管理回执，不改旧回执 |
| audit_events | 插入 actor_id、task_id、action、object_ref、outcome_code、correlation_id、before_summary、after_summary；默认 id/created_at 由库生成 | 白名单新增固定动作；summary 白名单原样 |
| operators / operator_sessions | 原有鉴权、身份版本、权限、禁用、撤销和实时期限列 | 原有锁顺序与不续期规则不变 |
| 系统目录 / schema_migrations | 原有 policy 读取 | 核验隔离目标与 schema，不修改 |

不改 mailbox 的 email_norm/source_type/credential_ref/credential_version/source_fingerprint/health/ever_registration_attempted/sale_eligibility/pool_status/last_used_at/created_at；不改 mailbox_platform_states、secret_objects、onboarding_tasks、resource_leases。原有历史单调触发器保留，不能因恢复启用把曾使用状态洗白。

## 回退与限制

只撤销本批 metadata 模块、门面末尾导出、audit 固定动作、runner 注册、对应测试和文档。不 reset 整工作树，不动已有导入/列表模块，不清空审计/回执/密钥，不降级 001/002。保留已经成功的管理记录，不能靠删除回执“允许再执行一次”。当前没有生产路由；未来上线与回退仍需独立授权。

服务级合成测试不替代真实页面/Google/Billing/Vertex/Sub2API 集成。会话最终检查仍有正常提交前后的极短时间边界，不承诺事务提交后还持续有效；稳定 MAC 首末检查也不证明抵御同 OS 权限攻击者 A→B→A 替换。宿主磁盘同步超时仍需独立解决，旧失败记录不得被本批通过覆盖。
