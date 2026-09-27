# P1c Task4a：邮箱导入阶段验证记录

## 结论与范围

2026-09-24：**实现和双重独立审查完成，但本轮统一回归未通过，不能标记阶段验收或上线通过。** 工作树为 `feature/gcloud-p1a`，基线 HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`；保留此前全部未提交改动。无提交、推送、生产迁移、权限变更、支付或外部调用。

本次只有内部合成邮箱导入服务，不新增 HTTP、页面、真实账号输入、平台凭据绑定、凭据读取或调度。字段和全部默认值见 [运维边界](../operations/onboarding-p1c-mailboxes.md)。完整 Task4 的列表、元数据/凭据更新、历史以及后续任务租约尚待实现。

## 主线程唯一统一回归

工作树根执行：

```text
.venv-onboarding/bin/python \
  /workspace/gcloud/.local/rf-p1c/task4a/observe_full_suite.py \
  --suite all --legacy --require-no-skips \
  --json-result /workspace/gcloud/.local/rf-p1c/task4a/root-final-result.json
```

观察包装器仅包裹 `onboarding_b1_support.apply_migration` 的测试 setUp 调用；透传参数至未修改的 runner，不重试，不改变 SQL、超时、fsync 或 JIT。

| 指标 | 实测 |
|---|---|
| 终态 UTC | 2026-09-24T10:42:28.969318+00:00 |
| 总数 | 531，且 531 个唯一 test IDs |
| onboarding / legacy | 458 / 73 |
| 失败 / 错误 / 跳过 | 0 / 2 / 0 |
| 成功数 | 529；不是 531 全绿 |
| 耗时 / 退出码 | 289.115 秒 / 1 |
| B1 setUp 迁移观察 | 209 次；208 APPLIED、1 未预期迁移错误、0 观察器错误 |

### 两项错误，不隐藏或按历史原因猜测

1. `test_onboarding_pool_vault.PoolVaultTests.test_existing_card_and_billing_owners_are_checked_without_billing_update_grant`：`setUp` 的 schema2 `apply_all` 报 `DEPENDENCY_UNAVAILABLE`。该次 apply_all 不在窄观察器范围；它先再核对 001 再执行 002，因此目前不能区分在哪个版本步骤出错，也未捕获原生 SQLSTATE/等待事件；**具体版本及底层原因未知**。不能因另一项 I/O 证据就断言两项原因相同。
2. `test_onboarding_mailbox_import_races.MailboxImportRaceTests.test_same_key_disjoint_payload_rolls_back_loser_resources_and_secret_audit`：B1 `setUp` 的原版 001 SQL 批次报 `QueryCanceled / 57014`，再由服务映射成 `DEPENDENCY_UNAVAILABLE`。原批次 9.843 秒；190 个采样中 141 次 `IO / DataFileImmediateSync`，189 次进程状态 D，采样 blocker PID 列表为空。说明该次建表确有磁盘同步等待，**尚未定位或修复宿主存储根因**，也不声称没有任何未采到的瞬时锁。

两项均在业务测试体前失败，不能当成业务断言已执行。没有反复全量求绿、调大超时、关 fsync、关 JIT 或转 tmpfs。本次失败记录将一直保留。

### 一次补充诊断，不替换失败的验收结果

随后只运行上述 PoolVault 用例一次，显式观测初次 001、apply_all 内部的 001 no-op 与 002：分别 APPLIED / UNCHANGED / APPLIED；1 test / 1.844 秒 / exit 0，0 失败、错误、跳过。观察绑定在 finally 恢复，未改 SQL、超时或业务断言。只说明此次未复现且业务体确实执行，不证明最初错误的具体版本或原因，也不把 531/2 ERROR 改写为通过。原始记录为 `diagnose-poolvault-once.txt` 和 `migration-once.jsonl`；独立分析为 `failure-audit.md` / `failure-summary.json`。

补充诊断后的主线程只读复核 `post-diagnostic-database-check.json` 与前述清理结果相同：永久 14 表/141 列/001 账本一行、其余为空，临时 schema/密钥目录为零，JIT 不变。故障后磁盘容量和 inode 检查未见耗尽；I/O 压力只提供相关证据，未定位底层存储或其他负载根因。

## TDD、定向测试与覆盖边界

原始记录位于私有目录 `/workspace/gcloud/.local/rf-p1c/task4a/`；这些是不同次执行，不能拼接成“一次全绿”：

- `import-red.txt`：初始 16 组因缺少服务模块而 RED，不夸大为 16 个独立业务断言失败；`import-green-attempt1.txt` 为 16 PASS / 26.361 秒。
- 独立审查 F1 找到真实缺陷：同批两次写入之间覆盖 AES 文件，原实现没有固定整批密钥材料，可能把同批密文写到不同密钥世代。`encryption-batch-red.txt` 确实出现预期 ServiceError 未抛出的失败。修复为捕获初始版本/材料、末次恒时比较；`import-green-attempt2.txt` 为 18 PASS / 16.023 秒。
- `import-green-attempt3.txt` 为 21 PASS / 20.539 秒，另 `commit-ack-test.txt` 为 1 PASS / 1.913 秒。最终源码有 22 组 import 测试，本轮统一执行这 22 组均通过。
- 原提交错误测试是在实际 COMMIT 前注入分类错误，只证明传播/回滚。新增 ACK 测试先完成真实 PostgreSQL COMMIT，再由测试专用 transaction 包装器抛 OperationalError，证明返回 `COMMIT_UNKNOWN`、数据和回执确已持久、显式同键重放原结果、不重复写入；**不是实际物理网络断连实验**。
- `races-tests-first.txt` 为 5 tests / 1 setUp ERROR / 20.335 秒，其余四项通过。错误是 001 初始化失败但当次未采原生原因；`races-disjoint-targeted.txt` 仅补执行此前没执行的业务场景，1 PASS / 1.351 秒。本轮统一执行相同场景再次初始化失败，不能据旧定向结果把本次统一结果改写为通过。
- 五组 race 使用真实 `multiprocessing` spawn、两个不同真实数据库会话、首个实际 Vault 写前 Barrier，验证逆序重叠邮箱、同键同摘要、同键异分组、同键不相交资料、跨 owner。未 mock SQL 或业务成功结果；合成输入会跨进程，不传真实秘密、连接或密钥。
- 锁等待跨 session TTL 用 `pg_blocking_pids` 证明实际资源/审计阻塞；SQL、审计故障注入有确达断言；回滚检查秘密、邮箱、平台状态、回执和审计。
- root 共享动作/runner 的 RED→GREEN 留存；`shared-final-green.txt` 为 9 PASS / 2.082 秒。独立质量审查另执行纯测试 9 PASS / 1.139 秒。保留既有 FastAPI 弃用告警及旧文件 ResourceWarning，不称零告警。

## 全部文件与逐行规则影响

本批基线是 `task4a/baseline/` 与 `baseline.json`，不是仅 git diff（多数前期文件仍 untracked）。本批共 10 个仓库文件：4 新源码/测试、4 既有文件修改、2 新文档；不把之前阶段重复计入。

| 文件 | 本次范围 / 原规则到新规则 |
|---|---|
| `onboarding/mailboxes.py` | 新增全部 216 行；原无此服务。1–28 固定定义和安全结果；31–39 exact 依赖/身份/初始密钥；42–72 解析、临时平台口令拒绝、规范排序和分层 MAC；75–106 末次复验和只读预览；109–133 严格回执投影；136–145 owner/指纹判断；148–158 原子邮箱和七平台新建；161–193 校验、重放、排序与唯一约束 savepoint；194–216 终态回执竞争、审计、提交后返回。 |
| `onboarding/audit.py` | 仅新增固定 `mailbox.import` 动作；摘要白名单和 PII 拒绝保持。 |
| `tools/onboarding_test_runner.py` | pools/all 增两组 import/races；不减旧组、不降低 no-skip。 |
| `tests/onboarding_pool_support.py` | 新增 24 行，仅隔离 manifest schema2 与有明确权限的测试操作员、独立测试 MAC key；不是生产自动授权。 |
| `tests/test_onboarding_mailbox_import.py` | 新增 374 行/22 组；覆盖输入、未知状态、保留历史、事务、权限、密钥和提交未知，无直接生产规则变更。 |
| `tests/test_onboarding_mailbox_import_races.py` | 新增 202 行/5 组真实多进程竞争；不以 thread 代替 spawn；无生产规则变更。 |
| `tests/test_onboarding_repository.py` | 新增 13–21 行：动作白名单允许，摘要含 PII/ID/hash 仍拒绝；旧测试不删。 |
| `tests/test_onboarding_test_runner.py` | 38–39 行同步新增模块集合；完整 inventory/唯一性保持。 |
| `docs/operations/onboarding-p1c-mailboxes.md` | 接口、五表全部字段/默认值、保守历史、回执/密钥及回退；不执行操作。 |
| `docs/verification/onboarding-p1c-mailboxes.md` | 本记录；不改变业务行为。 |

两名独立审查者完整读取源码与测试，规格审查 F1 修复后无已确认阻断代码项，质量审查覆盖 8 文件 1247 行与 4 baseline 完整 diff。审查通过不抵消上述统一测试失败。原始审查在 `spec-review.md`、`quality-review.md`，代码 SHA 在 `pre-final-source-sha256.json`；主线程终态再核验 8 个源码/测试 SHA 全部不变。

### 页面 → 操作 → 结果

当前 `webui/static/onboarding.html` 没有本轮导入按钮。点“刷新诊断”仍只核对本地底座；查询仍只查本人合成任务，不导入邮箱、不创建 Google 账号。将来接入邮箱池时才能由页面调用本轮内部服务，不能提前把原型当已上线功能。

```text
internal preview -> validate + authorize -> safe preview / batch MAC
internal import  -> sort + lock          -> create / same-value skip
same key + hash  -> revalidate + receipt -> original IDs
any write error  -> rollback             -> no success result
lost COMMIT ACK  -> COMMIT_UNKNOWN       -> explicit same-key replay
```

新增只服务未来本人邮箱池管理员；现旧注册、资产导出、支付、Google、Vertex 与 Sub2API 路径均未调用它。没有新增 HTTP 请求/响应字段、i18n key 或前端文案。

## 数据与清理复核

本次无 DDL、更改字段类型/默认值/索引，也未改 001/002 原文。写入已有五表 `secret_objects`、`mailbox_registry`、`mailbox_platform_states`、`operation_receipts`、`audit_events`；具体显式列和默认列见运维文档。不改已有邮箱的分组、密码、历史、平台凭据或占用。

终态只读核对 `final-database-check.json`：永久隔离 schema 仍 14 表 / 141 列，只有 001 迁移账本 1 行，另 13 表各 0 行；临时 schema 0、测试密钥目录 0；JIT on / above_cost 100000。001 SHA `3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782`，002 SHA `b763180ee4d782e6e88f2a78ac3bbf34df026425133e8c05ca99143eb56ec764`。同时核对 Vault/types/MAC/parser 四基础文件与基线相同。永久 schema 未应用 002。

## 剩余门槛与回退

- 当前 `account_password` 非空明确报 `PLATFORM_BINDING_REQUIRED` 是临时切片，不是永久删需求；明确平台归属、平台密码与邮箱密码独立更新仍须完成。
- 只支持 fixture 策略。生产 key provider、稳定密钥世代/轮换、OS 隔离、无人值守执行身份、备份恢复和真实凭据接入没有完成。前后检查不等于抵抗同 OS 攻击者的 A→B→A 替换。
- 列表/平台筛选/历史/更新 CAS、租约/凭据快照、卡池、旧消费者、页面与真实 Google/Billing/Vertex/Sub2API、费用批准后单次 Gemini 调用、独立调度启用仍待交付。
- PostgreSQL 建表稳定性是未通过的验收门槛。保留本轮及历史失败；排查宿主存储，不放宽测试来隐藏问题。另有可独立推进的离线研发，不宣称整体被完全阻断。
- 回退仅停用本批内部服务并撤销精确 10 文件增量及两个 runner 注册/一个审计动作。保留前期和用户改动，不做整仓 reset/clean，不删密钥/历史回执、不降级永久库。临时数据只由 manifest 归属校验的清理器处理。

HTML 阶段记录（非应用 E2E）另用最新模板生成并单独核对四视口；结果留在私有 `report-layout.json`。本 Markdown 不把未运行的渲染或未来回归提前写成通过。
