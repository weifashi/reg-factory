# Task5e 原子批次验证记录

2026-09-24，feature/gcloud-p1a，HEAD b0758484c2401ea38792e18711ff38ef8a24105c。
**新模块 35 项全部通过；受影响回归整体失败，尚不可上线。** 未提交、推送、部署、永久迁移或操作真实账号/支付/provider。

## 冻结实现与独立检查

- `onboarding/pool_batches.py`：479 行，SHA256 `1559523b3a729626a4870bccca062b9886e739f5bd0141ba1e001334498d420e`。
- `tests/test_onboarding_pool_batches.py`：874 行，SHA256 `117fde9367606e1df3d4ea9af39dba5f5cdedd7b1ee991082f69a01386eff5d4`；35 个方法，3 pure、32 PG。
- 仅为隔离合成 Google 任务整批受理；没有 HTTP、页面、worker、真实注册或支付。无 DDL。写入的六张表及原有规则影响见 `../operations/onboarding-p1c-pool-batches.md`。
- root-red：真实 setup 后缺模块，1 FAIL / 0 ERROR；不是连接失败冒充 RED。
- spec-review-01 指出 TTL 测试在准备完成前计时；只修测试，保留旧版本证据。连接/租约/线程 ready 后才置短期限，并观察真实 blocker 与 DB clock；秘密行边界先提交期限再锁行。
- spec-review-02、quality-review-01 均 STATIC PASS。质量审查有两项非阻断覆盖建议：历史任务 fetchall 损坏分支、自动选择资格矩阵更完整镜像；不代表这些分支已单独实测。
- root-runner：集合测试 2 RED→2 GREEN；完整 runner 6 PASS；独立 pure3 PASS。旧 ResourceWarning/deprecation 保留。

## 单次真实数据库回归

证据：`/workspace/gcloud/.local/rf-p1c/task5e-batches/root-affected-01/`。
worktree 命令：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests timeout 600s .venv-onboarding/bin/python /workspace/gcloud/.local/rf-p1c/task5e-batches/root-affected-01/run.py`。
O_EXCL 防重跑、89 源 SHA 门禁；真实 setup ERROR 后停止，不把未跑当跳过或通过。

| 指标 | 结果 |
|---|---|
| 发现 / 运行 / 未运行 | 263 / 49 / 214 |
| PASS / FAIL / ERROR / SKIP | 48 / 0 / 1 / 0 |
| 新 batch 模块 | 35 / 35 PASS |
| unittest 时间 / shell exit | 247.802 秒 / 1 |
| 批次六处真实等待 | config、lease、receipt、state 会话到期拒绝；secret、audit 秘密到期拒绝；线程结束、整批快照回滚 |
| 并发与提交 | 四种双 spawn 竞争、旧 task/session 锁反转、NOWAIT 不补位、真实 COMMIT 回执丢失后原 key 回放均通过 |

唯一 ERROR 位于旧 `pool_commands.test_marker_conflict_path_rereads_and_preserves_actual_existing_row` 的 **setUp 002_resource_pools_batch**，其测试正文未运行。原生 QueryCanceled / SQLSTATE 57014 / statement_timeout；PID3595977；DDL 6.409205 秒、迁移整体 6.412769 秒。124 个采样中 110 个 IO/DataFileImmediateSync、14 个无 wait event。不能凭此确认宿主存储根因或宣称剩余代码正确。

135 次迁移观察：89 APPLIED、45 UNCHANGED、1 MIGRATION_FAILURE；所有 monitor 结束、绑定恢复、diagnostic_errors 为空。运行后 89 源 SHA 全一致。没有放宽数据库超时/权限、修改 fsync/JIT/WAL 或重跑筛绿。

## 清理核验与未完成项

root 新建只读检查 exit0：永久库14表141列，仅001迁移记录且 checksum 原样，其余13表各0；临时 schema0、key fixture0；JIT on / 100000。不据此声称所有数据库连接均不存在。

Task5a 历史失败、数据库初始化稳定性与本次214项未运行均未关闭。下一步接入真实受保护邮箱页面，不以静态原型或合成链替代最终 Google/Vertex/Sub2API 授权验收；全目标仍进行中。
