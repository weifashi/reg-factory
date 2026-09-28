# "可证明未提交"信号运维说明

## 含义
5 条写路由的 409 VERSION_CONFLICT 若带 `not_committed: true`，表示服务端已证明原请求没有提交、以后也不会提交：
- 批次创建、pool 任务命令、邮箱修改、配置保存：在与原请求同一把串行锁内确认该请求键没有回执，且导致拒绝的前提是单调的（版本只增；被取代的配置不会复位）。
- 邮箱导入：没有串行锁，依据是预览摘要链。页面只在重新预览的摘要与未决导入完全一致时才用原键重放；服务端发现同名邮箱已存有不同的凭据指纹，说明该邮箱由另一个请求写入。服务端还会在同一事务内重读回执，作为纵深防御。

页面只凭这个信号、经操作者确认后解除"提交结果待核对"。完整规格不在本仓库，位于另一个工作区：`/workspace/gcloud/docs/superpowers/specs/2026-09-26-reg-factory-not-committed-signal-design.md`；下表是其第 4 节的摘要。

| 位置 | 函数 | 串行化 | 带信号的条件 | 不带信号 |
|---|---|---|---|---|
| 1 | `pool_batches.create` | 按操作者+请求键的事务咨询锁，锁后读回执 | 当前 pool 配置存在且 revision 不等于期望 | 配置不存在；资源不足或占用 |
| 2 | `pool_commands._command` | 任务行锁，锁后读回执 | 当前版本大于期望，或已达最大值 | 当前版本小于期望；任务已终态（RECONCILIATION_REQUIRED）；CAS 更新未命中的防御性冲突。fixture 任务在路由层就分派给 coordinator，不经过本函数 |
| 3 | `mailbox_update.update` | 邮箱行锁，锁后读回执 | 当前版本大于期望，或已达最大值 | 当前版本小于期望；邮箱不属于本人；CAS 更新未命中的防御性冲突 |
| 4 | `pool_config.replace` | 配置独占锁，锁后再次读回执 | 当前配置存在且 revision 不等于期望（期望为空亦然） | 无配置而期望非空 |
| 5 | `mailboxes.import_text` | 无串行锁，依赖预览摘要链 | 同名邮箱为本人所有、指纹格式正确但不同，且重读仍无回执 | 指纹格式异常；邮箱属于他人；重读发现回执 |

每处带信号前，都会在同一事务内重跑成功路径末尾的全部复核（迁移与目标校验、MAC 稳定性、会话复核）；任一复核失败，就抛复核自己的错误，不带信号。

## 前提（任一被破坏，此前的解除结论作废，须人工按请求键核对）
- 不发生丢失已提交事务的数据库回退（异步复制故障切换、按时间点恢复、克隆替换）。
- 滚动发布期间新旧代码对 5 处执行相同的前提判断。
- 下列数据写入后只增不改、不删。产品代码与迁移由静态守卫 `tests/test_onboarding_not_committed_guard.py` 约束；人工 SQL 不受约束，禁止人工修改：
  - `operation_receipts`：不得删除、归档、按保留期清理或改写；操作者、动作、任务、请求键与请求摘要写入后不改。回执缺失会让已提交的原请求被误判为未提交，导致重复执行。
  - `mailbox_registry`：不删除；`email_norm`（唯一）、所有者、凭据指纹写入后不改；`version` 只做 +1。
  - `onboarding_tasks`：不删除；`execution_scope` 不改；`version` 只做 +1。
  - `global_configs`：只追加，不修改、不删除；`revision` 唯一；新配置的 `created_at` 严格大于现有最大值（由 `pool_config.replace` 保证，人工插入配置行会破坏此前提）。
- 应用角色 `rf_onboarding_app` 永不授予上述 4 张表的 DELETE/TRUNCATE，也不授予 `global_configs` 的 UPDATE（包括列级 UPDATE）；也不得把它加入迁移角色 `rf_onboarding_migrator` 或任何持有这些权限的角色（以 NOINHERIT 方式加入再 SET ROLE 时，`has_table_privilege` 查不出来）。
- `tests/test_onboarding_pool_schema.py` 中的 `test_app_cannot_delete_or_truncate_not_committed_proof_tables` 只核对迁移代码在测试用临时 schema 里授出的权限，不代表生产库。生产库由 DBA 在上线前及每次变更权限后自查，期望全部为 false、成员关系查询无结果：

  ```sql
  SET search_path TO <生产 schema>;   -- 替换为实际部署的 schema
  SELECT t, p, has_table_privilege('rf_onboarding_app', t, p)
    FROM unnest(ARRAY['global_configs','mailbox_registry','operation_receipts','onboarding_tasks']) AS t,
         unnest(ARRAY['DELETE','TRUNCATE']) AS p;
  SELECT has_any_column_privilege('rf_onboarding_app', 'global_configs', 'UPDATE');
  SELECT roleid::regrole FROM pg_auth_members WHERE member = 'rf_onboarding_app'::regrole;
  ```
- 保留期清理、数据修复、归档任务不得触碰上述 4 张表；确需变更时，先停用解除功能（回滚后端 5 处信号），再重新审查本设计。

## 不会带信号的情况
迁移或数据库环境异常、锁超时、请求键冲突、资源占用、STALE_FENCE、APPROVAL_INVALID、RECONCILIATION_REQUIRED、fixture 诊断任务命令、所有 422/401/403/429/5xx。页面保持冻结并提示"原请求仍待核对"。

## 回滚
去掉后端 5 处信号即可；前端只认信号，没有信号时回到"任何拒绝都冻结"。
