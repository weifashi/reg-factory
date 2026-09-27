# P1c Task5a：全局 Pool 配置（内部服务）

## 范围与身份

为后续原子批次提供不可变配置快照，不是实际开通、任务执行或完整Task5。本批不增加HTTP/页面按钮，不提供模型调用/凭据解密，不写真实Google/Sub2API。当前`webui/static/onboarding.html`仍是离线保护模式控制面；旧配置编辑页也未接入新pool配置。验收状态见对应verification，不以本文代替测试。

`pool_config.get_current(settings,actor,*,policy)`读取；`replace(settings,actor,expected_revision,fields,request_key,*,policy,mac)`追加新版本。精确Settings/真实Actor/SyntheticPoolPolicy，write另精确RequestMac；settings必须与policy相同。不接FixtureActor，不信任Actor.permissions缓存，不续会话。读取需要`onboarding:read`，写入需要`config:manage`，后者不额外要求read。

配置是**同一个隔离schema全局一份当前头**，不是每账号、每批次或每operator一份。changed_by只表示修改人，不充当owner过滤；获读权限者可见全局非秘密设置，获管理权限者竞争同一版本。未来新任务pin配置ID，旧任务仍引用旧版本；本批不冒称任务快照创建和调度已经接通。

不构造Vault/Keyring，不要求active AES；MAC原有AES候选独立性扫描仍保留。MAC目录来自受信本机装配，不是HTTP输入，Settings没有目录归属注册表。

## 字段合同与“未接通”边界

fields是exact dict，先copy再验证，副本同时用于MAC/SQL。exact八键，不忽略extra、不补默认：

| 键 | 当前允许值/类型 | 真实含义 |
|---|---|---|
| model / region / project_prefix | exact str，`fixture-[a-z0-9][a-z0-9-]{0,63}` | 合成模型/区域/前缀，不是官方可用性或真实Cloud项目ID验证 |
| instance_ref | `fixture:sub2api` | 内置合成目标标签，非已连接实例 |
| group_ref | `fixture:group` 或 `fixture:group-alt` | 内置合成分组标签，非已确认存在的实际分组 |
| timeout_seconds | exact int，30..86400 | 存储合同/资源边界，当前无worker执行此超时 |
| concurrency | exact int，1..32 | 存储合同，未接入调度限流 |
| retention_days | exact int，1..365 | 存储合同，未执行任何清理/删除 |

int拒绝bool；测试提交的1800/1/30不是隐式默认。内置合成目标字典是隔离测试前置，不是生产目标注册表。真实实例/分组注册、provider校验、全局认证secret与worker/保留策略仍属整体上线必做。不能因为这些标签语法合格就显示Google/Vertex/Sub2API可用。

不接URL/自由secret_refs/token/PAN/银行卡验证码。secret_refs本期严格空对象；公开`secrets_configured=False`仅说该配置行没有秘密引用，不代表其他系统全未配置或业务可调用。

## 操作 → 结果

| 内部动作 / 未来页面操作 | 服务结果 |
|---|---|
| 读取且尚无pool配置 | 返回None，不创建默认，不错读fixture配置 |
| 首次保存（expected_revision=None） | 只有当前头为空才追加新版本 |
| 基于所见revision保存 | 只有仍是当前头才成功；否则VERSION_CONFLICT |
| 不同管理员同时按同旧revision保存 | 全局CAS只有一个成功，不按operator分成多份配置 |
| 同字段但新request_key | 新管理命令，追加新ID/revision，不覆盖旧行 |
| 原key、原expected、原字段显式重试 | 返回原成功config_id/revision/key，即便全局头已更新；零额外写 |
| 原key更改字段或expected_revision | IDEMPOTENCY_CONFLICT，不偷偷覆盖旧成功 |
| 配置guard/审计等待期间会话过期 | 拒绝并回滚，不返回已失效权限下的结果 |
| COMMIT确认丢失 | COMMIT_UNKNOWN，不重发；重新认证后原key核验已落库回执 |

expected_revision只有None或`pool-<canonical UUID>`；key为`[A-Za-z0-9._:-]{1,128}`。config_id与revision中的UUID只需各自规范、引用匹配，不要求二者相等或不同。

读取DTO仅`id,revision,scope,nonsecret_config,secrets_configured,created_at`；scope固定pool、created_at规范UTC微秒时间。不下发changed_by/secret_refs或原始DB行。replace与回执结果仅`config_id,revision,request_key`。

## 数据库、头选择与锁

没有新增表、字段、索引、迁移或权限。app对global_configs仍只有SELECT/INSERT，不能用FOR UPDATE/FOR SHARE，不授予UPDATE来方便CAS。

```text
get_current: live read -> SELECT current pool head -> final policy/auth -> COMMIT
replace:     live manage -> old receipt? -> original result
                         -> exclusive config guard -> live auth -> receipt recheck
                         -> head + expected CAS -> new config/receipt/audit
                         -> final policy/MAC/auth -> COMMIT -> return
future batch: live manage -> shared config guard -> mailbox NOWAIT -> ...
old task:     pinned config_id (no current-head lock / no hot replacement)
```

内部guard使用双int事务advisory域`hashtext(current_schema()),428701`；exclusive与shared同键，事务结束自动释放。它本身不是独立授权接口，调用者先做policy/liveauth；原语要求INTRANS、shared精确bool。不与旧单参数迁移锁或security421906混用。hash碰撞最多额外序列化，不改变owner权限。仍用原2秒锁超时，超时不自动重试，不调成无限等候。

头定义为`scope=pool ORDER BY created_at DESC,id DESC`。在exclusive guard内，新created_at取数据库时钟与旧头时间+1微秒的较大值，防时钟回退把旧头重新选成当前。先严格校验存储时间、配置形状与UUID；不修复脏行，日期溢出失败回滚。这个排序规则不是实际外部操作发生时间证明。

| 表 | 本次读取/写入字段 | 行为 |
|---|---|---|
| global_configs | id,revision,nonsecret_config,secret_refs,changed_by,created_at,scope | pool头/历史读取，只INSERT新版本；原配置、task/config pins不UPDATE |
| operation_receipts | id,task_id,scope_operator_id,action,resource_revision,generation,fence,phase,idempotency_key,request_hash,result_summary及默认时间/版本 | 固定pool.config.replace终态管理回执；不写INTENT，不造假task |
| audit_events | actor_id,task_id,action,object_ref,outcome_code,correlation_id,before_summary,after_summary及默认id/created_at | config.create；仅版本摘要，不记录8字段正文 |
| operators / operator_sessions | 原有身份/权限/禁用/epoch/撤销/绝对及idle期限 | 真实鉴权与锁，不续期、不改权限 |
| onboarding_tasks + batches + configs（旧fixture路径） | 原task锁查询及最新fixture config读取 | 只改隔离判定，不改任务状态/身份/快照 |
| 系统目录/schema_migrations | 原policy只读验证 | 001/002字节和权限原样 |

## 幂等、损坏存储与提交

MAC `pool.config.replace.v1` 对规范`{v,schema,instance_marker,expected_revision,fields}`签名；owner由MAC帧绑定，key仅定位回执。首末key probe防执行期间材料变化；不等于生产密钥轮换，也不保证抵御同OS攻击者A→B→A。

回执固定task=NULL、scope=当前actor、action=pool.config.replace、resource_revision=pool-admin:actor、generation1/fence0/SUCCEEDED。先验固定元数据，再恒时比较摘要；不同body返回IDEMPOTENCY_CONFLICT，相同才验最小summary与该历史配置的id/revision/fields/scope/changed_by。坏存储固定DEPENDENCY_UNAVAILABLE，不回显JSON或原始SQL异常。当前头变化不否认旧成功。

写者等待guard后必须再次查receipt，否则两个同key请求会误把后到者当旧version冲突。配置/receipt/audit同事务；若防御性的ON CONFLICT撞到异常已有回执，候选配置/时间全部回滚，同hash但已经产生候选不提交无审计改动。config.create审计object是配置UUID、correlation是receiptUUID，outcome OK、before空、after.version=1；这里1是新不可变记录初始版本，不是全局revision序号。

任何审计/SQL/最终隔离、密钥、liveauth失败整笔回滚；结果只在UoW成功COMMIT后返回。内部read同样不吞COMMIT_UNKNOWN。底层schema checksum漂移仍沿用VERSION_CONFLICT，不因新服务把所有依赖错误混成一种。

## 旧fixture兼容与回退

旧create_fixture_task查询最新配置时仅考虑`revision LIKE 'fixture-%'`，仍拒调用者指定旧fixture版本；不在001-only schema引用scope字段。旧lock_task额外要求`task.get('execution_scope','fixture') == 'fixture'`，防schema2的pool任务伪装成fixture样式混入；所有原fixtureActor/mailbox/config五字段限制保留。

回退只能撤本批新入口/注册/测试及两处兼容改动，先确保不再有新pool调用者；不删除已经提交的配置/回执/审计，不解除future holds，不reset工作树、不重写001002。未来真实部署仍需独立授权。本批成功不能替代任务/租约/N批次、真实target注册、页面和外部E2E，旧磁盘同步问题也未因局部通过而修复。
