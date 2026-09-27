# P1c Task4b-1：邮箱列表与签名分页

## 当前入口

内部服务 `mailboxes.list_page(settings, actor, *, platform=None, search='', group_ref=None, health=None, occupied=None, cursor=None, limit=50, policy, mac)`；返回 `Page(items, next_cursor)`。没有 HTTP、页面、元数据修改、历史查询、凭据读取或真实 Google 操作。本轮导入服务原 216 行保持，末尾仅显式导出读取入口。

只接精确 `Settings` / `SyntheticPoolPolicy` / `RequestMac` / `Actor`，复用私有 manifest 隔离 schema2，读取要求本人实时 `onboarding:read`。不因 Actor 快照自带权限就放行；不延长会话期限。认证仍取得原有 operator/session 锁，不是无锁数据库读取。

`Settings` 没有 schema→keyring 目录登记，MAC 目录来自受信本机装配，不是 HTTP 参数。不得声称 settings 相等即证明 MAC 目录归属。列表不实例化 Vault/Keyring，不解密、不需要 active AES key；但 RequestMac 仍按原规则读取目录中存在的 AES 候选以拒绝复用密钥。

## 操作 → 结果

| 内部调用 | 结果 |
|---|---|
| 默认列表 | 仅本人，每邮箱一条，现有平台摘要固定顺序；不是按平台拆库 |
| 指定 google 等单个平台 | 只筛该平台状态存在的邮箱，返回该平台摘要；不暗筛 UNUSED，不创建/补写状态 |
| 平台状态缺失 | all 返回实际已有集合，可为空；指定该平台时不包含这条邮箱，不伪造 UNKNOWN |
| 搜索邮箱 | 大小写不敏感的字面子串；`%`、`_` 不作通配符，空格不自动删除 |
| 分组/健康/占用筛选 | 精确筛选；`group_ref=None` 不筛，`''` 是空分组；occupied 必须 bool 或 None |
| 下一页 | DESC `(created_at,id)`；签名绑定本人、用途、环境、目录、全部规范筛选、固定顺序和 limit；改变这些必须重新第一页 |
| 查询期间身份过期、密钥/环境变化 | 末次复验拒绝，不能把已读到的 Page 返回 |

platform 为 all 或七个平台；search≤320 原始字符，group≤128，拒控制/格式字符和非法 UTF-8；limit 为精确 int 1..100，拒 bool。Unicode lower 可扩张，因此按原始输入校验一次，再使 SQL 与 MAC 使用相同规范语义。

每页一个数据 SELECT，取 limit+1；有下一条时用最后一条已返回记录作为游标。READ COMMITTED keyset **不是跨请求冻结快照**：数据和筛选字段变更、迟提交可能改变后续页集合。现有索引未为生产容量重新优化，不承诺大规模性能。

## 数据与固定投影

没有新增/修改表、字段、索引或业务写入。

- `mailbox_registry` 只读 `id, owner_operator_id, email_norm, source_type, group_ref, health, disabled, ever_registration_attempted, sale_eligibility, pool_status, version, created_at, updated_at, last_used_at`。owner 仅用于过滤，不返回；email_norm 投影为 email。
- `mailbox_platform_states` 只读 `mailbox_id, platform, identity_status, usage_status, version, checked_at`；关联 ID 不返回，平台 DTO 只含后五项。
- `resource_leases` 只读 `resource_kind, resource_id, task_id` 判断占用；不返回 lease/task/owner 详情。
- `onboarding_tasks` 只读 `execution_scope, mailbox_id, status` 作保守占用补充；不返回任务详情。
- `operators` / `operator_sessions` 与系统目录/迁移账本由原权限、期限和环境检查读取；认证锁规则不变。
- 不查询 `secret_objects`，不返回 credential_ref、credential_version、source_fingerprint、evidence_ref、last_task_id、PIN/密码/RT/API key/TOTP 或原始数据库行。

Mailbox DTO 精确包含：`id,email,source_type,group_ref,health,disabled,ever_registration_attempted,sale_eligibility,pool_status,occupied,version,created_at,updated_at,last_used_at,platforms`。UUID、枚举、bool、正 bigint、时间、长度及控制字符二次校验；坏数据固定 `DEPENDENCY_UNAVAILABLE`，不回显原值。邮箱和分组是本人管理数据，不宣称已匿名化。

## 占用与安全边界

```text
canonical mailbox UUID lease with task_id != NULL
                       OR
pool task status NOT IN (SUCCEEDED, FAILED_CONFIRMED, CANCELLED_SAFE)
                       |
                       v
                 occupied = true
```

lease 过期、owner_id 为空或其 task 已终态但 lease 未释放，都不能自动显示空闲。lease.task_id=NULL 不算占用。不同平台共享这一邮箱占用观察；它不是任务领取许可，真正 claim 仍需后续锁内核验。当前没有 pool task/lease writer，测试构造合法记录只证明读取规则。

游标只有版本、filter MAC、UTC 六位时间、UUID 和签名；总长≤512，不含原始搜索、分组、owner、实例标识或路径。严格拒非规范 base64/JSON、重复 key、NaN、额外字段、bool 版本及错误时间/UUID；MAC 文件错误保留 `SECRET_UNAVAILABLE`，不伪装成用户参数错。首末 key probe 不是生产密钥轮换，也不证明抗同 OS 攻击者 A→B→A 替换。

所有结果在 UoW COMMIT 之后返回；`COMMIT_UNKNOWN` 不吞、不自动重试。列表没有业务写入，但未知提交仍遵守统一存储合同。

## 回退与剩余工作

只撤销本批读取模块、末尾门面、对应测试和 runner 注册；保留此前导入、Vault、SQL、用户改动、密钥和审计。不清空库、不降级 002、不 reset 整个工作树。无生产入口需要停用，未来部署仍需独立授权。

仍缺 metadata CAS、真实历史、平台凭据明确绑定/更新、任务租约与消费、卡池、完整页面和真实外部集成。Task4a 的 531 项 / 2 次迁移初始化错误仍是未通过的环境验收记录，本批测试不得覆盖它；本批新鲜验证见对应 verification 文档。
