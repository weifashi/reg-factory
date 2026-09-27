# Task5d Pool commands 验证记录

2026-09-24，feature/gcloud-p1a，基线 HEAD b0758484c2401ea38792e18711ff38ef8a24105c。**新模块 24 项通过；受影响回归整体失败，不能交付上线。** 无提交、推送、部署、永久迁移或真实账号/支付/provider 操作。

## 最终冻结实现

- onboarding/pool_commands.py：233 行，SHA256 `5dab7f5fffeb3fbadde81e4b5ec92b185b928b87ee899a5d868fb1f5ab3cee98`。
- tests/test_onboarding_pool_commands.py：904 行，SHA256 `05e436b6562927ae948b2761002a38b92314eea299091336f57a9f8f1ccefe96`，24 个唯一方法。
- 统一 runner 的 pools/all 加入本模块；显式集合测试同步加入。85 个旧文件只允许这两项 runner 文件变化，其余 83 项保持基线 SHA。
- 无新增表/列/权限、HTTP 或页面。暂停/取消只是受理请求；核验只建 marker，不是 worker、停止证明或真实 Google 集成。

## RED、审查与历史失败

1. root-red：真实 PoolCase setup 后缺模块断言，1 FAIL、0 ERROR，exit1。原始记录保留。
2. author-final-01：23 个发现，3 个方法启动；纯输入 1 PASS，等待方法 4 subtest FAIL，下一方法 001 setup ERROR，20 未跑。不能把 subtest 数当方法数。
3. 独立 spec 找到缺新 key 不同 body 首次竞争、缺旧代未决回执覆盖、TTL 早于准备和失败清理缺陷。只修测试，不改产品/数据库超时/权限。
4. author-fixes-02：补齐测试契约及 finally 隔离、等待证据；AST、24 方法发现、pure1 PASS。修订不证明原 4 FAIL 的实际根因。
5. spec-review-02 STATIC PASS；quality-review-01 STATIC PASS、0 blocking、1 low：旧 spawn 测试的启动循环在 try 外，启动异常时父级清理可改进。此建议不代表本次实际启动失败，也未改变冻结源。
6. root-runner-02：集合测试 2 RED→2 GREEN；offline 62 PASS；完整 runner 6 PASS。旧 server.py:833 ResourceWarning 和 FastAPI deprecation 如实保留。

## Root 单次真实 PostgreSQL 结果

证据私有目录：`/workspace/gcloud/.local/rf-p1c/task5d-commands/root-final-02/`。
命令在上述 worktree 执行：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests timeout 600s .venv-onboarding/bin/python /workspace/gcloud/.local/rf-p1c/task5d-commands/root-final-02/run.py`。
运行目录以 O_EXCL 防重跑；87 源 SHA 门禁；发生真实 setup ERROR 立即停止后续测试，不用旧通过数拼成新验收。

| 指标 | 真实结果 |
|---|---|
| 发现 / 运行 | 227 / 46 |
| PASS / FAIL / ERROR / SKIP | 45 / 0 / 1 / 0 |
| 未运行 | 181 |
| 新 commands 模块 | 24 / 24 PASS |
| unittest 时间 / shell exit | 246.482 秒 / 1 |
| 新模块等待目标 | task、mailbox、platform state、receipt、marker、audit 六处均真实阻塞；首次观察早于期限，释放前 DB clock 已过期；均 UNAUTHENTICATED、线程结束、完整快照不变 |
| 新 key / 旧代未决 | 两独立 spawn 有序竞争及同 key 回放通过；当前代2/未决代1三命令逐行不变通过 |

唯一 ERROR：旧 `pool_leases.test_current_pin_drift_and_wrong_binding` 的 **setUp 002_resource_pools_batch**，测试正文未执行。原生 QueryCanceled / SQLSTATE 57014 / statement_timeout；PID3575790；DDL 批次 7.378079 秒，迁移整体 7.380964 秒。142 个采样中 141 个 IO/DataFileImmediateSync、1 个 ClientRead。只能说明同步 I/O 等待相关证据，不能证明宿主硬件根因或认定产品全部正确。

123 个迁移观察：81 APPLIED、41 UNCHANGED、1 MIGRATION_FAILURE；所有 monitor 结束、绑定恢复、diagnostic_errors 为空。运行后 87 源 SHA 全一致。没有提高 2s/5s/10s 或 650ms，没有改变 fsync/JIT/WAL，没有重跑筛绿。

## 只读清理核验与交付边界

root-database-check.py exit0：永久库 14 表 / 141 列，仅 schema_migrations 一行 001 原 checksum，其余 13 表各0；临时 schema0、key fixture0；JIT on / 100000 原样。root 单次执行进程已结束，未遗留本轮 spawn 子进程。此清理结果不补足 181 项未跑验证。

Task5a 历史失败、旧轮 setup ERROR 和本次整体验收失败均保持开放。下一步可继续独立原子批次/页面纵切开发，但仍需解决隔离数据库验证稳定性、补完回归及真实授权集成；不能以当前合成模块 PASS 替代生产验收。
