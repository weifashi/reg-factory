# B2 依赖修复后复验：本次全量通过

2026-09-24；最终时间 `2026-09-24T08:11:46.983992+00:00`；用户已授权修复隔离 PostgreSQL 依赖并重新验收。

- **267 tests / 0 failures / 0 errors / 0 skipped，140.002 秒，exit 0。**代码最后变更后仅做本次一次全量串行复验；旧265项/7错误的失败记录原样保留，不用绿色覆盖历史。
- 补齐 Ubuntu libllvm17t64 1:17.0.6-9ubuntu1 的单个 LLVM17 常规库，仅在专用 BASE/runtime-lib 内；不装宿主包、不更改共享pgdist，不用LLVM20假冒。
- 工具新增固定专用库目录安全加载；2个新增离线测试覆盖缺目录兼容、安全前置、mode/owner/普通文件/符号链接/悬空链接拒绝。14项离线子集RED→GREEN；全量由265增至267。
- 小查询JIT探针：修前58P01 UndefinedFile，修后jit=on、Functions=5；仅探针事务临时降低触发阈值，全局JIT开关/三项阈值不变，没有调大超时。
- 7个遗留schema全部经过原manifest/marker/owner/锁/依赖闭包保护清理，确认不存在后才移除对应manifest。没有force或通配DROP。
- 共享pgdist的1575个常规文件SHA完全一致，另一postmaster PID及启动时刻未变；授权实例由原工具单独重启。实例配置、两个角色凭据及数据配置哈希一致，无重新生成凭据。
- 最终永久14表/141列，schema_migrations=1，其余13表为空；临时schema=0，临时key fixture=0；应用角色、私有socket与无TCP边界不变。无业务表DDL或迁移修改。
- compileall、git diff --check及21包依赖兼容检查通过；独立审查另核对工具逐行差异、来源/哈希与清理流程。

证据：`.local/rf-b2-recheck/root-final-tests.txt`、`root-final-result.json`、`package-manifest.json`、`jit-red.txt`/`jit-green.txt`、`cleanup-results.json`、`environment-after.json`、`final-database-check.json`、`review.md`。原始失败证据仍在 `.local/rf-b2/`。

**未关闭的历史问题：**B1旧迁移statement_timeout本次未复现，但根因仍未定位；不能将缺库修复说成历史超时根因修复。B3/真实云端/支付/API页面仍不在本轮；未提交、推送或部署。

**日志安全：**首次新增负测试mock失败诊断意外带出继承环境，原日志已0600、所在证据目录0700且不公开；测试改为只断言调用布尔值，避免再输出参数环境。公开日志只用已检查的成功输出/脱敏证据。若原始诊断曾被转发，应轮换相关运行会话凭据；本轮没有自动更改平台凭据。

---

## 以下为修复前历史失败记录（保留，不代表上方最新验收结果）

# B2 验证记录：代码已实现，整套验收未通过

日期：2026-09-24；worktree `/workspace/reg-factory/.worktrees/gcloud-p1a`；分支 `feature/gcloud-p1a`；HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`。未提交/推送/部署。

## 最终统一验收（不能用分批绿色替代）

主代理在所有 B2 源码冻结后，以独占数据库窗口串行运行：

```bash
.venv-onboarding/bin/python /workspace/gcloud/.local/rf-b2/run-verification.py
# runner discovers tests/test_onboarding_*.py and fails on any skip
```

**265 项，258 通过，7 errors，0 failures，0 skipped；157.348 秒，exit 1。整套验收未通过。**

其中 B2 新增的 80 项在本次同一全量日志中全部通过：认证29、Keyring9、SecretStore23、下载16、桥接3。这个子集结果不能把全量失败改成通过。

七项错误均在 B1 ReceiptTests 的 addCleanup → SchemaFixture.close → cleanup_schema → _guard_cascade_scope：

`psycopg.errors.UndefinedFile: llvmjit.so: libLLVM-17.so.1: cannot open shared object file`

涉及 confirmed_observation、confirmed_resources、conflicting_terminal、consume/revoke/generation、expired/revoked/paused、inconclusive_observation、lease_expiry 七个测试的清理。没有删除这些错误、skip清理或反复重跑刷绿。没有跳过安全依赖闭包检查、强行 DROP CASCADE、改库 JIT/超时或装包。

原始证据：
- `.local/rf-b2/root-final-tests.txt`：完整 stdout/stderr。
- `.local/rf-b2/root-final-result.json`：265项ID、成功标记false、错误位置及子集统计。
- `.local/rf-b2/final-database-check.json`：最终永久表、schema与key fixture状态。
- `.local/rf-b2/final-environment-diagnosis.md`：独立只读确认实际postmaster动态链接路径缺LLVM17；不能用宿主LLVM20替代，不猜测本次planner触发变化。

## 最终状态及残留

- 永久 `rf_onboarding`：14张表、141列；schema_migrations=1，其他13张业务表计数全0。
- migration checksum 与开始时完全相同：`3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782`。
- **保留7个临时 rf_p1b_test_* schema及对应manifest，等待依赖修复后按原安全清理流程处理。**没有把清理失败写成已清空。
- 临时key fixture目录剩余0；无真实凭据或外部调用。
- 只读查询确认 JIT=on、jit_above_cost=100000；app无权限读jit_provider，首次诊断因此拒绝，随后只查询app允许的设置。没有提权或更改配置。

## 模块与安全证据

- 原始 RED/GREEN 分别保存于同目录 security-*.txt / keyring-*.txt / secrets-*.txt / downloads-*.txt / bridge-*.txt / audit-*.txt。
- 六项真实发现已经修复：Actor epoch类型、getpass回显fallback、SecretStore审计跨TTL、require审计跨TTL、暂停时保护性撤销、None store依赖。每项有真实RED及修后GREEN，不把接口缺失RED冒充最终行为证明。
- 下载两个spawn进程竞争同grant，仅1个DELIVERED；grant行锁后过期拒绝；audit失败消费回滚；模拟COMMIT成功但回包丢失时不给字节、不恢复grant。
- SecretStore真实审计锁/secret行锁跨session或object期限时零consumer调用；AES篡改/AAD错绑/缺key/权限/撤销/过期拒绝；SQL/审计快照无合成canary。
- Linux目录0700/文件0600、owner、nofollow逐层描述符、硬链接/FIFO/路径逃逸已测；Windows ACL未验。
- 独立规格/安全逐行审查无待修代码阻塞；审查者另跑25项纯测试通过。完整行级/表字段/契约/残余风险见 `.local/rf-b2/review-auth-secrets-downloads.md` 与 `review-data-callers-contracts.md`。此审查结论不覆盖后来发现的环境全量失败。
- 最后源码变化后 py_compile/compileall、依赖兼容检查21包、git diff --check通过；新增文件另按全文审查，不能仅以git diff --check证明质量。

## B1 旧问题不能混为一谈

B1 用户验收 185项中1项在迁移setUp失败；服务器历史日志确认 statement_timeout。本轮最多6次受控原样迁移探针均通过（97–112ms），但没有定位或修复历史超时。
本次最终7项是清理SQL加载JIT缺共享库，**不能仅凭同属数据库就断言与历史迁移超时同因**。两条风险都保留。

## 未验证与下一步

- B3 HTTP/全路由保护/旧执行阻断/Cookie/真实Origin和可信IP/API-SSE端到端脱敏尚未接入；旧控制面不能因此对外开放。
- 密钥轮换只是单对象CAS原语，不是全库扫描、跨进程切换与生产备份恢复工具。
- 下一步先修复隔离PostgreSQL运行依赖，安全清理7个有manifest临时schema，重新统一验收；同时保留B1历史迁移稳定性问题，不直接开始生产接入。
- 不改变现有功能断言，不调低密码成本，不扩大超时、不skip清理来制造绿色。
