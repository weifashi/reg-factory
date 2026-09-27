# P1c Task5a 全局 Pool 配置验收记录

## 结论：实现和独立双审完成，受影响回归未通过

**不是上线交付，不是Task5完整验收。** 最终root v2：16模块255唯一测试，253通过、1失败、1错误、0跳过，exit1；测试耗时243.505秒，总计243.580秒。2026-09-24 12:00:00.363900—12:04:03.949710 UTC。不把第一轮通过项、作者定向通过或旧179项拼成“全绿”。不进行第三轮原样重跑求绿。

本批9文件：新pool_config服务、新核心/race测试2文件、旧repository及测试2文件、runner及集合测试2文件、operations/verification文档2文件。7个源文件冻结01，root两轮前后与双审SHA一致；security/audit/request_mac/pool_vault/001002六项保护源与本批前基线一致。无提交、推送、生产迁移、部署；无DDL、权限扩大、HTTP、产品页面或worker改变。

## 最终两项未通过必须分别处理

| 项 | 证据 | 当前判断 |
|---|---|---|
| 跨管理员不同key、同旧配置的竞争 | pool_config_races.test_different_operators_different_keys_same_expected_one_winner。独立backend3475210/3475211、独立session、两者到达guard前同步；结果OK和DEPENDENCY_UNAVAILABLE，预期OK和VERSION_CONFLICT | **业务测试FAIL，不能写并发验收通过**。候选仅1、配置/receipt/audit各只+1，不代表预期错误合同已验证。私有PG在12:00:54.684记录PID3475211 lock timeout；未记录该条SQL，具体锁和持锁延迟原因仍未定位，不全归咎磁盘 |
| 旧审批撤销测试 | approvals.test_revoke_prevents_consumption_and_is_audited，在001迁移setup失败；PID3477675；native SQLSTATE57014 statement_timeout | **setup ERROR，业务体未运行**。001调用6.242013秒，DDL事件6.238436秒；118个采样为IO/DataFileImmediateSync。确认等待现象，不证明宿主机硬件/QoS根因，也不能视为审批断言通过 |

同owner同key竞争为两OK同结果，同owner不同key为一OK一VERSION；新service的session/config/audit三个真实跨TTL锁测试和RETURNING用例在最终v2执行通过。它们不能消除上表跨管理员FAIL。

## 第一轮root监测故障及修复

第一轮255项/205.923秒：254通过、1 ERROR、0失败/跳过，exit1。失败为既有migration_sequence.test_002_failure_rolls_back_only_002_and_preserves_001，非setup或已证实磁盘错误：root的ConnectionProbe二次包装测试FailAfterPoolDDL，Composable.as_string取到中间context.connection，缺pgconn，AttributeError；未到002注入点。

只修改私有诊断工具：精确psycopg.Connection才加probe，已存在的故障上下文原样保留，不吞异常、不改其execute/transaction。初版纯RED为身份断言失败；改为Composed才同栈复现AttributeError。之后两项纯GREEN；独立补审也两项纯通过且PG连接尝试0。v2保持完全相同16模块255 ID，O_EXCL独立输出，不改产品源码或迁移断言，是已证实监测bug修复后的新鲜验证，不是无改动重跑。

最终v2原002故障测试已通过：真实001提交保留、002 DDL后注入恰1次并回滚。其两次调用sql_event_instrumented=false，逐SQL事件缺席不表示没有执行DDL或没有故障；不能捏造native日志。原v1日志/footer全部保留。

## 本批原始RED/GREEN与作者错误

- 入口RED：真实PoolCase完成setup后服务缺失断言失败；纯入口也失败。旧001过滤用例到业务INVALID_INPUT，后续明确成断言。不将setup失败当业务RED。
- 输入RED另有001 setup ERROR：11:40:50.053 UTC、PID3459440 statement timeout。原日志保留。
- root runner精确模块集合1项RED→1项GREEN；仅注册验证。
- 作者集中验证44项/69.494秒：43通过、1 session TTL setup ERROR，11:43:44.781 UTC、PID3461223 statement timeout。业务体未执行；与最终v2中该TTL通过分开记录。
- root审查发现配置INSERT未校验RETURNING时间：定向1项RED（ServiceError未抛）→最小修复→4项GREEN/17.330秒。实际INSERT后仅cursor.fetchone注入(None,)，确认触达与全表快照回滚；不是触发器或物理数据库损坏。最终使用SQL旧头+1微秒与clock取大，RETURNING再验有限aware/UTC和严格递增。溢出由既有UoW固定依赖错误回滚，无storage修改。

## 独立审查和范围核验

- SPEC PASS（静态）：逐项合同、完整新源和4处既有文件diff，7源+6保护基线匹配。未跑PG。
- QUALITY PASS（Task5a代码范围）：独立8纯测试通过、PG连接尝试0；无代码阻断发现。不替代root回归，更不抵消后续FAIL。
- OBSERVER FIX PASS：保留原故障钩子/异常传播，16模块及255 ID未减，另2纯测试通过。
- 原预备13模块228项仅发现检查，经repository.lock_task真实调用链增加approvals/downloads/b1_faults至16/255；未把发现数字当执行通过。
- root最终源码与freeze和双审清单完全相同。报告归档覆盖本批9文件全部变动行，不把前序大量dirty/untracked算成本批。git diff --check另通过，但不会用tracked空diff冒充新文件审查。

## 迁移观测与数据库最终只读核对

v2记录243次迁移调用：001共207次、002共36次；204 APPLIED、33 UNCHANGED、6返回异常。6次中5次是迁移负测的预期拒绝/故障，1次是上表审批setup超时；**不能把6次均说生产迁移失败或均说通过**。原始native57014已记录。观察线程诊断错误0、绑定还原；两次故障wrapper明确没有逐SQL遥测。

最终只读：永久rf_onboarding仍14表141列，仅001且checksum保持3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782，其他13业务表0行；临时schema0、临时key fixture0。JIT on/100000未变。002文件checksum b763180ee4d782e6e88f2a78ac3bbf34df026425133e8c05ca99143eb56ec764保持。没有放宽2秒锁/5秒语句超时、关闭fsync/JIT、使用tmpfs或重启其他实例。

## 仍待完成

1. 定位跨管理员竞争锁超时的持锁阶段/耗时原因，保持VERSION与timeout的正确区别，不能把断言改成“接受任意错误”求绿。
2. 处理隔离数据库持续同步I/O/迁移超时风险；旧531/2、91/1以及本批作者错误仍保留，未证明环境修复。必要真实目标/替代环境资料已提问，不能臆造。
3. 完成Task5任务/双凭据pin/租约/原子N、其余池管理、全部API与页面；当前配置八键仅非秘密合成字典，数值没有worker执行，真实target注册未接。
4. 真实Google资格/支付验证、Vertex/Sub2API及经批准费用的Gemini调用仍未验收。不能自动绕过官方核验、虚构资格或费用授权。
5. 本批HTML是失败状态进度报告，展示真实旧保护页面与未接线内部结果，不是产品E2E。报告渲染、四视口与公网一致性证据单独归档，不将其当功能通过。
