# Pool 邮箱逻辑占用验证记录

2026-09-24 UTC。**当前验收仍 FAILED，未达到上线标准。最新196项回归只运行40项：39 PASS、1 setup ERROR，156未运行；新增pool_leases 25项全部通过。** 新入口尚未接页面或真实执行器；下面均为隔离测试，不代表 Google、支付、Vertex 或 Sub2API 验证成功。

## 范围

工作树 `/workspace/reg-factory/.worktrees/gcloud-p1a`，分支 `feature/gcloud-p1a`，HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`。新增 `pool_leases.py`、对应测试及操作/验证文档；测试 runner 登记新模块。无新增表列、HTTP、消费器、释放或恢复功能，无提交、推送、部署、永久迁移。

私有证据根为 `/workspace/gcloud/.local/rf-p1c/task5c-leases/`。产品 SHA 为 `8be3134a53d1947ba61574bb9e9e9cd458dbccfe467389818aabe30209ecdaaf`，当前测试 SHA 为 `1f9a760a1edfa4f4a47fc215b3143ea3479e7f1c504c8e510543b99f1c84e4b1`；root-final-01旧测试SHA为 `63e6abecb60a1e60c93743e37e313ad84e7792d50b14e8b46cded5ade80bffa6`，旧证据保留。

## 已有验证，不互相替代

| 检查 | 实际结果 | 证据 |
|---|---|---|
| 作者真实临时库后的缺模块 RED | 1 FAIL、0 ERROR | author-red/ |
| 作者旧尝试 | 发现20项、实际11项：10 PASS、1 setup ERROR，9未跑 | author-final-01/ |
| 测试修订后静态审查 | SPEC、QUALITY静态通过，不代表PG运行通过 | spec-review.md、quality-review.md |
| 根代理纯测试 | 3 PASS；runner登记2 RED→2 PASS；setup-stop helper另1 PASS | root-pure-fixes、runner-red/green、root-setup-stop-pure 日志 |
| 根代理第一轮受影响回归 | **发现195，运行94：91 PASS、0 FAIL、3 ERROR、0 SKIP，101未运行；exit 1** | root-final-01/raw.log、result.json、run.exit |
| 根代理源码一致性 | 85个代码/依赖 SHA 匹配 | root-final-01/approved-source-sha.json、root-statistics.json |
| 根代理只读残留核验 | 14表141列，仅001原校验和；其余13表0行，临时schema/key fixture均0 | root-final-01/database-check.json、database-check.exit |

根代理第一轮测试耗时239.935秒，runner耗时240.028888秒，session58243终态exit1；入口为私有 `root-final-01/run.py`，外层600秒限时、一次性输出门禁。11个模块：新pool_leases24、pool_contracts12、pool_repository11、旧leases25、receipts22、coordinator25、repository15、security30、pool_vault21、contracts4、test_runner6。新模块24项均运行，**22 PASS、2正文 ERROR**；不能与旧10 PASS或纯测试拼成全绿。

## 第一轮三个报错必须分别保留

1. `PoolLeaseIntegrationTests.test_real_lease_and_secret_deadlines_after_wait`：测试正文→`wait_across_deadline`→locker UoW，顶层 `DEPENDENCY_UNAVAILABLE`。
2. `PoolLeaseIntegrationTests.test_real_task_mailbox_state_lease_secret_audit_waits_expire_session`：同样为正文helper/UoW错误。原日志没有保存底层异常链；UoW也会把断言转换成服务错误，**尚不能证明是SQL故障、期限提前耗尽或产品缺陷**。7组无目标标签的WAIT输出不能证明后续断言/提交成功。后续诊断必须新增证据，不能原样重跑求绿。
3. `ReceiptTests.test_unknown_holds_and_never_returns_approval`：旧测试的001初始化报 `QueryCanceled / SQLSTATE 57014 / statement_timeout`，正文未运行。PID3534949；001批次11.542656秒，222采样中203为`IO/DataFileImmediateSync`。这是该次同步IO等待证据，不证明宿主根因，也不意味着配置超时已改变。精确setup-stop终止后续101项。

136次迁移观测的monitor均结束、替换绑定恢复、diagnostic_errors为空。未修改2秒锁/5秒语句/10秒空闲事务限制，未更改JIT/fsync/WAL或重启共享服务。旧Task5a竞争失败及其他setup失败继续保留。

## 交付边界

可确认：上述实际执行结果、源码一致性与残留核验。不可确认：本批全通过、环境问题已解决、真实任务能运行或已经上线。单次新诊断即便未复现也不能自动关闭原ERROR。页面和用户按钮当前无变化；字段写入及业务规则详见 `docs/operations/onboarding-p1c-pool-leases.md`。


## 最小修复与第二轮：新模块通过，整体仍失败

单次 `wait-diagnostic-01`（session97668、exit1）发现2项、只运行1项：正文ERROR原始链为 `InsufficientPrivilege / SQLSTATE 42501`，位置为helper锁SQL。源码/权限合同结合定位到audit共享表锁：APP对审计表仅SELECT/INSERT，不足以持SHARE锁。这不是赠予APP修改审计表权限的理由。PG16官方规则见 [LOCK Notes](https://www.postgresql.org/docs/16/sql-lock.html)。该次trace又在已关闭连接PID观察路径附近报OperationalError并停止；因此诊断完整性为false、第二项未执行，不能冒称完整九目标诊断。

只改测试：audit阻塞连接改用已有manifest测试schema owner、显式事务与READ COMMITTED/2s/5s/10s；非audit、worker、observer仍APP。新增独立权限负测证明APP真实42501、无UPDATE/DELETE/TRUNCATE，三个独立后端角色和超时正确。产品、DDL、GRANT及650ms期限不变。私有observer另存副本，闭连接PID元信息property seam有1 FAIL RED→11项离线PASS；不是实际断网/断连测试。根代理另跑3个closed-PID与3个API纯测试，6 PASS、0.015秒。

最小差异经 `wait-lock-fix-spec-review.md`、`wait-lock-fix-code-review.md` 静态通过后，根代理执行 `root-final-02/run.py`。只继承脱敏异常链，不安装旧窄trace。真实结果：**196发现、40运行、39 PASS、0 FAIL、1 ERROR、0 SKIP、156未跑，199.429秒，session32111终态exit1**。新pool_leases25项全部通过，包括两项真实等待测试（九组阻塞PID）、新权限负测与两spawn争用；源码85SHA匹配，只有获批测试文件与上一轮不同。

唯一最新ERROR：`test_onboarding_pool_repository.PoolRepositoryTests.test_entry_after_real_pool_setup`，在**002_resource_pools_batch**初始化而非正文报 `QueryCanceled / 57014 / statement_timeout`。PID3551628，批次5.086534秒；98采样中97为IO/DataFileImmediateSync、1为ClientRead。不误写001、不据此断言宿主根因。72次迁移47 APPLIED、24 UNCHANGED、1异常，全部monitor结束、bindings恢复、diagnostic_errors空；setup-stop停止后156项。证据 `root-final-02/raw.log/result.json/run.exit/root-statistics.json`。

最新后置只读检查 `root-final-02/database-check.*` exit0：永久14表141列，001原checksum一行、其余13表0行，临时schema/key fixture均0，JIT仍on/100000。没有原样循环重跑求绿。**可以确认本次25项新模块通过和测试权限问题已修复；不能宣称196项通过、环境稳定、旧Task5a问题关闭或整体上线。**
