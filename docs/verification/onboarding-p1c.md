# P1c C0 验证记录（2026-09-24）

## 范围与证据纪律

当前完成C0 catalog/002结构/显式分版本迁移与权限验证，以及S1纯离线parser；完整池服务/API/页面、生产provider及真实外部流程均未完成。本记录不能取代B0–B3最终总验收，也不将小批测试GREEN当历史全量I/O问题已解决。

执行树 `/workspace/reg-factory/.worktrees/gcloud-p1a`；原始日志 `/workspace/gcloud/.local/rf-p1c/`。schema测试只使用SchemaFixture manifest临时schema，未读真实.env/秘密、未外部账户操作、未提交/推送/部署。001字节固定不变。

另一次临时manifest元数据核对得到升级后20表/239列（不计_test_marker），相对001增加6表/98列：新六表共88列、既有表增加10列。逐列类型/nullable证据在schema-metadata.json；该schema随后清理，永久库没有升级。

## 已实际读取的原始结果

| 日志 | 命令/范围 | 实际结果 |
|---|---|---|
| schema-interface-red.txt | unittest test_onboarding_pool_schema.PoolSchemaInterfaceTests | 1 FAIL：002不存在；不是timeout/skip |
| schema-behavior-red.txt | 私有一次性脚本：manifest fixture应用001后断言六张池表 | AssertionError，六表均缺失；finally清理fixture；真实旧schema状态RED |
| schema-interface-green.txt | 同interface test，002落盘后 | 1 PASS，001hash未变/六表/无definer/无CREATE SCHEMA |
| root-sequence-first.txt | `PYTHONPATH=tests .venv-onboarding/bin/python -m unittest test_onboarding_migration_catalog test_onboarding_migration_sequence test_onboarding_migrations -v` | **25 tests PASS，4.456s，零skip** |
| root-schema-first.txt | `PYTHONPATH=tests .venv-onboarding/bin/python -m unittest test_onboarding_pool_schema -v` | **16 tests PASS，1.401s，零skip** |

最后两批为root独占PG执行，本文编写者实际读了完整日志，不仅引用口头通知。25与16是两个独立目标批次，不把重复interface GREEN额外累加；也不宣称整仓/全部onboarding已经重跑。未调大迁移超时、未删旧断言。

## Schema 16项具体证明

1. 002文件显式存在；001固定SHA；准确新增六表。
2. 原表集合外正好六表，安全索引齐；无易引隐式唯一等待的mailbox_one_active_task。
3. email全局唯一与小写约束；ever注册导致INELIGIBLE并不可撤销。
4. mailbox+platform唯一、平台枚举、FK拒绝不存在身份。
5. 同PAN指纹跨owner重复拒绝；PAN/owner不能原位改。
6. 单卡一个open预留；UNKNOWN仍reserved；成功先释放reserve再加linked；超容量拒绝。
7. 同成功唯一去重且复合FK不能把link挂在另一张卡/身份。
8. 旧失败迟到成功可冻结后保留新预留及真实link，计数允许超额事实；不能直接清冻结绕过容量。
9. reservation card/task/account/receipt/revisions/五秘密refs不可变；card后来换expiry/identity不改历史refs。
10. migrator删除预留会修正投影，app无DELETE。
11. 旧fixture task默认合法；pool必须邮箱pin/平台pin；空pin/JSONnull/bool/0/负数/小数/字符串revision拒绝。
12. 单/组合平台计划严格、去重、bounded，combined不能Google。
13. admin回执只能操作者范围且terminal；同scope/action/key重复拒绝；task与operator必须二选一。
14. 新kind受枚举限制，cvv/otp/password_dump拒绝；这不代表秘密服务已放开。
15. 实际app角色在事务内执行reservation INSERT、pool双pin CHECK、mailbox永久历史、phase UPDATE、link INSERT成功；没有五trigger函数直接EXECUTE权限仍正常。应用事务force_rollback后计数回到零。
16. app各表SELECT/INSERT/UPDATE逐项权限核验；历史表无UPDATE/DELETE；DDL/DELETE真实拒绝；helper仅精确两函数授权。

schema测试采用一次setUpClass迁移共享manifest、每case独立事务回滚，减少DDL/清理I/O。第15项仅基础合成refs先提交供另app连接可见，真正业务写事务回滚。它主要证明SQL约束/权限，不冒充部署后持久恢复；持久升级/故障回滚由独立sequence测试证明。

## Sequence/catalog额外证明

- 已审001/002完整前缀；bool/未知/缺中间/重复/乱序/hash漂移拒绝，源文件缺失固定依赖错误。
- catalog纯读文件不连接DB，文件内容与pinned SHA匹配才返回SQL。
- 空schema001→002、已有001→002、重复no-op、core默认001在已知002上no-op。
- 001原数据/默认fixture值在升级后保留。
- 002注入失败真正命中计数器后回滚，仅001仍持久存在；不是未到注入点的假阳性。
- 未知已提交版本即使core消费者也拒绝；target2不能越过001。
- 原迁移权限、audit append、checks/FKs、错误连接/schema拒绝继续回归。

## 逐行规则影响地图

| 文件/行段 | 新规则 / 保留边界 |
|---|---|
| 002_pools.sql 4–36 | 唯一邮箱+独立平台身份；不再将平台密码等同邮箱口令 |
| 37–67 | 付款身份append-only资料容器；全局PAN身份与容量投影 |
| 68–101 | reservation不同于success；固定历史refs；唯一成功关联 |
| 103–126 | 原四表向前演进；旧fixture默认；管理回执不造假task；新增secret枚举不改变SecretStore |
| 130–181 | 两helper只验shape，不借JSON参数决定owner/权限 |
| 183–224 | 注册历史不可逆、PAN与reservation主体不可偷换 |
| 226–259 | 预留delta/成功增量在同事务维护，失败回滚 |
| 263–269 | PUBLIC撤权；精确app helper授权由migrate管理 |
| migration_catalog.py | 已审文件/hash/连续版本单真源，不能扫描目录自动接受新SQL |
| migrate.py | 默认001兼容、显式目标逐版事务、同schema advisory锁、权限跟DDL提交、不自动迁移/重放 |
| test_onboarding_pool_schema.py | 真PG负约束+应用角色正反向测试，无thread mock代替SQL |

全字段/索引/权限说明见 `../operations/onboarding-p1c.md`。没有改common.emails、asset_store或旧注册算法；当前没有新池页面/操作，所以不会将此C0底座描述为“点击导入已可用”。

## 独立审查与冻结身份

reviewer `/root/b0_spec_review` 已逐行读002与测试，静态未见阻止隔离验证的SQL安全/spec缺口，并要求补实际app触发器正向；已补且root16项GREEN包含该测试。计划review修复双凭据pin、隐式唯一等待、PAN全局身份、合成存储门禁及历史refs五项；**生产门禁与池服务仍是后续实现要求**，不是C0自动拥有。

| 文件 | SHA256 |
|---|---|
| onboarding/migrations/001_core.sql | 3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782 |
| onboarding/migrations/002_pools.sql | b763180ee4d782e6e88f2a78ac3bbf34df026425133e8c05ca99143eb56ec764 |
| tests/test_onboarding_pool_schema.py | 5982554feb326404238404edaa7c924fea82afb915cd8e85178169611a00c51d |

## 后续结果插入点 / 未验项

- `INTEGRATION_DIAGNOSTICS`：已完成。`diagnostics-red.txt`真实失败后，`diagnostics-green.txt`整个routes 24项PASS/17.563s。接受已知完整002前缀仍标real_flows_connected=false；未知版本、漂移或单独缺001均拒绝；升级后旧fixture暂停操作仍可用。
- `INTEGRATION_RUNNER`：已加入offline/pools分组与全部test_onboarding文件库存相等校验，防新增测试漏入all。runner-red.txt真实RED，runner-green.txt 6项PASS/0.756s；完整新增后统一验收结果另记，暂不预写。
- `S1_PARSER`：14组离线测试通过，已独立逐行审查。root最终offline+legacy共96项PASS/1.085s（root-offline-final-*）；parser14、catalog8、依赖1、旧回归73，无重复计数。JSON/iCloud邻接吞行、session cookie吞行、非法Unicode均有实际RED→GREEN。新模块不接HTTP、DB或Vault。
- PoolVault用途授权/生产key provider、owner隔离服务、邮箱/卡CRUD、批次/lease、两spawn进程竞争、旧消费者全面接入、池API/页面仍未验收。
- 未做真实资料迁移/卡验证/Google资格/支付/模型/Sub2API/部署。测试卡和合成观测不代表官方成功或可上线。

## S1 原有规则变化与影响

- 新`legacy_mailboxes`仅复用旧识别逻辑，旧`common.account_records`无修改。现有WebUI/CLI导入规则没有被暗改。
- 支持原邮箱分隔格式、iCloud多行、JSON/JSONL；严格拒重复JSON键、未知字段、非字符串秘密、冲突别名，以及session/access/OAuth记录。
- JSON邮箱密码与account_password保留原字符且分别存内存，不自动fallback；新serializer同时保留API key/URL/two_factor与两种密码。
- 邮箱规范化只strip/casefold，不删plus/dot；相同规范身份相同凭据报重复、不同报冲突，不覆盖首次。
- UTF8最多262144字节/1000逻辑候选，错误只line/code；预览仅邮箱/来源/分组/统计，不返回秘密或提交批准。
- 复核发现并修复旧行为继承缺口：iCloud二行后下一条JSON/分隔邮箱/已识别session cookie不可吞成取码凭据。普通不明确opaque凭据不胡乱推断为另一个账户。
- 无表字段或API变化；新增不可变内存DTO及私有serializer。完整行段与14组测试见私有s1-delivery.md/s1-review.md。

新锁依赖检查另见`onboarding-dependencies.md`。批次/卡分配/秘密存储与页面并未因此自动实现。

## 最终统一结果（本批仍未整体通过）

2026-09-24 10:04:15 UTC，最终代码一次完整带观测回归：**465项 / 0fail / 1error / 0skip，222.250秒，exit1**。392项onboarding（含新增全部模块）加73项旧回归。此轮因实际新增迁移/诊断/解析/依赖改动而重验，不是反复重跑至绿。

唯一错误仍在旧repository用例`test_audit_summary_rejects_secret_fields_and_free_text`的setUp，尚未进入业务断言。私有观测捕获001_core_batch原生QueryCanceled/57014，5.0746秒；98采样中97为IO/DataFileImmediateSync，未发现blocking PID。与此前类似的数据文件同步等待再次发生，但宿主底层原因仍未根治，未更改原2s/5s/10s超时、JIT、fsync或SQL来掩盖。

证据`root-final-tests.txt`、`root-final-result.json`、`full-suite-migration-observations.jsonl`。失败记录完整保留；不能将464项已执行通过描述为整个465通过，也不能将一次setup错误误称业务断言失败。下一批继续实现不依赖环境修复的工作，上线前必须关闭该整体验收门禁。

同一最终代码真实HTTPS/Chromium回归：12项行为检查、20截图、4个实际视口全部通过（browser-result.json）。无生产账号/网络操作；详情沿B3相同fixture脚本，输出另存P1c，不覆盖历史。

结束后永久schema核对仍14表/141列、仅schema_migrations1行其余0；临时schema及fixture key目录均0；001 SHA不变、JIT仍on（final-database-check.json）。002只用于已清理的manifest临时schema。
