# P1c C0 操作边界与数据规则（2026-09-24）

## 1. 当前阶段实现了什么

当前阶段实现是**隔离合成环境中的数据库结构、版本迁移底座及离线邮箱解析**，不是邮箱/卡池业务上线：002 六表、既有四表向前演进、五个触发器、两个形状校验函数、精确应用角色权限及受审版本序列。

```text
已实现                     尚未实现 / 尚未验收
001 -> 002 显式升级         邮箱导入/列表/修改实际业务服务
六表与计数约束             PoolVault / 合成输入硬门禁 / 真实秘密provider
schema 类型与引用形状      session+owner+用途+lease 的池业务授权
隔离测试 app 角色可写       池 API / 三Tab页面 / 批次领取 / consumer切换
历史不可逆/卡身份不可变     真实邮箱、卡、Google、支付、Sub2API、部署
```

S1纯解析/脱敏预览与私有serializer已另经14组测试与独立审查；root离线+旧回归96项通过，不能用SQL测试代替其结果。当前旧 B1/B2 fixture-only 限制未删除；数据库允许新的 kind 不表示旧 SecretStore 已允许真实资料。C0 没有新增页面动作：页面上不能因此导入真卡、领取邮箱或启动 Google 任务。旧受保护资产路由仍不解禁。

本文是长期运维证据，不是生产执行批准；面向用户的 HTML 汇报由 root 汇总实际页面与各切片状态，不能画一个未实现的池页面冒充已交付。

## 2. 版本与执行入口

| 版本 | 固定文件 | SHA256 |
|---|---|---|
| 001 | onboarding/migrations/001_core.sql | 3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782 |
| 002 | onboarding/migrations/002_pools.sql | b763180ee4d782e6e88f2a78ac3bbf34df026425133e8c05ca99143eb56ec764 |

001 原文未改。catalog 只接受受审连续版本；缺版本、乱序、重复、未知、bool 假整数、任意 checksum 漂移拒绝，不自行排序/猜测“最新”。新目录中的 SQL 文件不会自动成为 known version。

- `apply_migration(conn,schema,*,script=None,target_version=1)`：默认仍只要求001；完整已知002前缀下是 no-op，不降级。`script`仅本机可信001故障注入，无HTTP入口，002不能用它覆盖。
- `apply_all(conn,schema,*,target_version=2)`：每个版本独立事务，返回是否至少应用了一步；001提交后002失败，001保留、002整笔回滚。无自动重试。
- CLI `--target-version 1|2` 默认1；Web启动不自动迁移。只允许已验证本机测试目标/专用角色/manifest schema，未新增生产目标、远程连接、角色提升。
- 每事务仍 `lock_timeout=2s / statement_timeout=5s / idle_in_transaction_session_timeout=10s`；沿用同schema advisory锁，不为通过测试放大超时。
- DDL、版本行与该版本GRANT同事务；故障中不能留下“版本已成功但权限没应用”。提交回包不明归 `COMMIT_UNKNOWN`，先查实际已装版本/校验和，不反复发送DDL。

**本次实际执行**仅测试工具创建的 `rf_p1b_test_<token>` manifest 临时schema，非已有 `rf_onboarding` 或真实邮箱数据库。没有运行生产迁移命令。

## 3. 六张新表：全部字段与业务含义

独立临时schema实查：20表/239列；相对001为+6表/+98列。这里只是已实现迁移的结构，不是永久库已升级。

通用列在下表逐表列全：`id`为UUID主键；`version`正bigint默认1；`created_at/updated_at`为UTC timestamptz/数据库时钟。FK均RESTRICT，不因删除父对象静默丢历史。密码/PAN/持卡姓名/详细地址不建明文字段。

### 3.1 mailbox_registry（001原来没有统一邮箱表；SQL L4–22）

| 字段 | 类型/作用 |
|---|---|
| id | UUID 主身份 |
| owner_operator_id | operators FK，归属操作者；跨owner权限仍需未来服务核验 |
| email_norm | text 3..320，全局唯一、小写、无控制字符；服务将做casefold且保留plus/dot |
| source_type | outlook / icloud / other |
| group_ref | text，默认空，最长128 |
| credential_ref / credential_version | 邮箱秘密对象FK / 正版本；不是Google平台口令 |
| health / disabled | UNKNOWN/HEALTHY/NEEDS_REVIEW/DISABLED；独立禁用bool |
| ever_registration_attempted | 曾尝试注册，默认false，一旦true不回退 |
| sale_eligibility | UNVERIFIED/ELIGIBLE/INELIGIBLE；曾注册永久INELIGIBLE |
| pool_status | AVAILABLE/EXPORTED/QUARANTINED |
| source_fingerprint / last_used_at | 可空char64来源指纹 / 最后使用时间；指纹计算方式由未来受审服务确定，不在此用裸密码hash |
| version / created_at / updated_at | CAS版本与时间 |

索引 `mailboxes_owner_filters(owner_operator_id,disabled,health,group_ref,id)`；email唯一索引防重复身份。DDL不是“邮箱健康/纯净”证明；默认UNKNOWN/UNVERIFIED。

### 3.2 mailbox_platform_states（L23–36）

| 字段 | 类型/作用 |
|---|---|
| id / mailbox_id | UUID / mailbox_registry FK |
| platform | google/claude/chatgpt/grok/kiro/github/k12 |
| identity_status | UNKNOWN/EXISTING/NEW_CONFIRMED |
| usage_status | UNUSED/RESERVED/SUCCEEDED/FAILED_CONFIRMED/UNKNOWN/CONFLICT/HISTORY_UNRECONCILED |
| credential_ref / credential_version | 可空独立平台口令FK / 正版本；不能用邮箱改密覆盖Google实际口令 |
| last_task_id | 可空onboarding_tasks FK |
| evidence_ref / checked_at | 可空≤256证据引用 / 检查时间，禁止把秘密正文当证据 |
| version / created_at / updated_at | CAS版本与时间 |

`UNIQUE(mailbox_id,platform)`、`platform_filter(platform,usage_status,mailbox_id)`。跨平台可复用不等于重新变成“从未注册可售卖”。观测真实性尚需后续受信服务。

### 3.3 billing_identities（L37–46）

`id`；`owner_operator_id`FK；`holder_secret_ref`与`address_secret_ref`两个秘密FK；`country`两位大写；`validation_status`为UNVERIFIED/USER_ATTESTED/NEEDS_REVIEW；`revision`资料正版本；`version`行正版本；`created_at/updated_at`。

应用角色只有SELECT/INSERT：资料修订应新建身份对象再换引用，不覆盖旧预留对应姓名/地址。USER_ATTESTED只是用户声明，不是银行/Google验证通过。

### 3.4 payment_cards（L47–67）

| 字段 | 类型/作用 |
|---|---|
| id / owner_operator_id | 卡UUID / operators FK |
| alias / brand / last4 | 1..80别名 / visa/mastercard/amex/other / 四位数字尾号 |
| expiry_ref / pan_secret_ref / billing_identity_ref | 到期秘密、PAN秘密、付款身份FK |
| pan_fingerprint | 64位hex，全局唯一；未来服务必须用稳定全局HMAC，不能按owner拆分同张卡 |
| enabled / account_limit | 启用bool / 1..10000上限，不是余额证明 |
| sms_channel_ref | 可空≤128通道引用，不存OTP |
| linked_count / reserved_count | 非负成功关联数 / 未决名额预留数，触发器维护投影 |
| reconciliation_required / last_assigned_at | 冲突冻结bool / 最后分配时间 |
| revision / version / created_at / updated_at | 资料版本 / 行版本 / 时间 |

`cards_candidates(owner_operator_id,enabled,reconciliation_required,linked_count,last_assigned_at,id)`。非冻结卡必须`linked+reserved<=account_limit`；冻结时允许记下已发生的超额事实，**不表示可以新分配**。PAN引用/指纹/owner不可原位换，换PAN应新卡ID。

### 3.5 card_reservations（L68–88）

`id`；`card_id`FK；`task_id`FK；`account_ref`平台身份FK；`phase`为NOT_SENT/INTENT/UNKNOWN/SUCCEEDED/FAILED_CONFIRMED/CANCELLED_SAFE/CONFLICT；`conflict_detected`独立迟到冲突bool；`operation_id`唯一回执FK；`card_revision/billing_revision`历史正版本；`pan_secret_ref/expiry_secret_ref/billing_identity_ref/holder_secret_ref/address_secret_ref`五个固定历史引用FK；`evidence_ref`可空≤256；`version/created_at/updated_at`。

`UNIQUE(operation_id)`、`UNIQUE(id,card_id,account_ref)`；`card_one_open_binding(card_id)`只约束NOT_SENT/INTENT/UNKNOWN/CONFLICT；`reservations_task(task_id,created_at,id)`。同卡同一时刻最多一个未决绑卡。预留不是成功关联，UNKNOWN仍占名额。

身份、task、account、receipt、两revision及五refs不可修改，防挪卡后计数错账。phase/conflict/evidence/行版本/时间可由后续服务受控修改；schema本身不是外部成功证据。

### 3.6 card_account_links（L89–101）

`id/card_id/account_ref`；`reservation_id`唯一且与card/account组成复合FK；`billing_resource_ref/evidence_ref`均1..256；`confirmed_at`；`version/created_at/updated_at`。`UNIQUE(card_id,account_ref)`防重复成功加次数；`links_account(account_ref,confirmed_at,id)`用于历史。应用只SELECT/INSERT，不可改/删已发生的关联事实。

## 4. 原有四表演进（没有改001文件）

| 表 | 本次002改变 | 对旧业务的影响 |
|---|---|---|
| global_configs | 新scope fixture/pool，默认fixture | 旧配置保持fixture；未来池查询必须显式scope，不误拿最新fixture配置 |
| onboarding_tasks | 新execution_scope默认fixture；mailbox_id FK；platform含7单平台+combined；platform_plan数组；credential_version邮箱版本；mailbox_credential_ref FK；platform_credential_pins对象 | 旧task无需补值仍合法。pool task必须邮箱身份与双pin；不能只带一个version混淆邮箱密码和Google口令 |
| operation_receipts | task_id允许NULL；新scope_operator_id FK/result_summary对象；exact XOR范围约束；admin只SUCCEEDED/FAILED_CONFIRMED；新owner/action/key部分唯一索引 | 不再为管理导入伪造Google任务；旧task回执原语义保留。result_summary虽SQL只验object，未来服务仍须严格安全字段投影 |
| secret_objects | kind CHECK新增mailbox_credential/platform_credential/billing_holder/billing_address/card_expiry | 原fixture/google_credential/pan/service_account_json枚举保留；CVV/OTP仍拒绝。**未放宽SecretStore/use/download权限** |

task部分非唯一索引 `mailbox_active_tasks(mailbox_id,id)`用于查活动任务；刻意不建活动task部分UNIQUE，避免INSERT隐式等待旧task终态提交与operator/session锁形成环。后续批次必须mailbox NOWAIT+重验hold，当前C0没有实现该分配服务。

## 5. 函数、触发器、精确权限

| SQL行 | 对象 | 规则 |
|---|---|---|
| 130–147 | valid_platform_plan(text,jsonb) | 单平台恰好自身；combined仅2..6个去重非Google真实平台；拒JSON null/非数组/未知/bool |
| 149–181 | valid_platform_credential_pins(jsonb,jsonb) | key集合与plan对应，每pin恰好state_id/secret_ref/revision/identity_status；UUID形式、正bigint整数、固定身份态；拒bool/NULL结构/extra |
| 183–195 | mailbox_history_monotonic | ever注册与INELIGIBLE不可回退；首次ever=true强制INELIGIBLE |
| 197–208 | card_identity_immutable | PAN秘密引用、PAN指纹、owner不可修改 |
| 210–224 | reservation_identity_immutable | 预留主体、任务、回执、历史版本及五refs不可挪动 |
| 226–247 | reservation_count_projection | INSERT/phase UPDATE/DELETE按open集合差量改reserved_count/version；锁card行；DELETE只迁移者可用 |
| 249–259 | link_count_projection | 去重后的新link使linked_count/version+1；同事务失败回滚 |

两helper是IMMUTABLE纯形状判断；七函数均invoker，无SECURITY DEFINER、无动态SQL、无跨schema访问。PUBLIC全部撤权；app仅能直接EXECUTE两helper，不能直接调用五trigger函数，但真实app写表触发器运行已验证。

| app权限 | 对象 |
|---|---|
| SELECT/INSERT/UPDATE，无DELETE/DDL | mailbox_registry/mailbox_platform_states/payment_cards/card_reservations |
| SELECT/INSERT，无UPDATE/DELETE | billing_identities/card_account_links |
| EXECUTE | 仅两个CHECK helper |
| 保留原权限 | audit/config append-only；schema_migrations只读；001旧表固定权限 |

app数据库角色属于受信后端，不是终端用户隔离边界。owner匹配、secret kind/purpose、task/lease当前fence、卡冻结时禁新分配、完整状态机/审计/COMMIT后披露仍必须由后续服务实现；不能通过这份DDL宣称已证明这些业务授权。

## 6. 故障与回退

- 同成功重复link：唯一约束拒绝第二份，成功计数不增长。
- UNKNOWN预留：仍reserved；清理UI/超时不能自动释放。当前无释放业务API。
- 已失败后迟到成功：先冻结卡，保留原失败+conflict_detected，再记录真实link；不抢删另一预留，允许超额事实留账；C0仅测试结构能容纳这一流程，不宣称已接外部观测。
- 002 SQL/GRANT故障：整版回滚且已提交001仍可用；不要手动插version行骗过检查。
- COMMIT_UNKNOWN：保留证据，核对安装前缀和hash；不按失败假设自动重放。
- 本期没有down.sql/删表回滚。已应用002的隔离schema保留已知前缀；旧core调用可no-op。若只有manifest临时测试数据，用原安全cleanup核对instance/token/owner后删除**该**schema；禁止泛删、删除schema_migrations、降checksum、覆盖001。
- 真实生产切换另审：独立OS/生产密钥provider/备份恢复/完整服务与所有consumer切换/无人值守身份/外部接口证据未闭合前，不能接真实邮箱/PAN/付款信息，更不能启动官方账户/付款操作。

## 7. 后续独立插入点（不是完成声明）

`INTEGRATION_DIAGNOSTICS`：诊断现在接受已核验的完整001/002前缀，返回实际版本；不启用任何真实流程。未知/漂移/缺前缀仍失败关闭；routes24项通过，详见验证记录。

`INTEGRATION_RUNNER`：专用runner已加offline/pools分组；all必须与所有test_onboarding模块一一对应。runner6项通过；此前长批量I/O边界不因小批验证通过而自动关闭。

`S1_PARSER`：纯离线解析/安全预览/私有JSON序列化已通过14组测试与独立审查；无API/DB写入能力。preview不返回凭据；serializer会返回秘密，只准私有内存兼容调用，禁止接日志或公开API。
