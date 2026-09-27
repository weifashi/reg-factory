# "可证明未提交"信号运维说明

## 含义
5 条写路由的 409 VERSION_CONFLICT 若带 `not_committed: true`，表示服务端已证明原请求没有提交、以后也不会提交：
- 批次创建、pool 任务命令、邮箱修改、配置保存：在与原请求同一把串行锁内确认该请求键没有回执，且导致拒绝的前提是单调的（版本只增；被取代的配置不会复位）。
- 邮箱导入：没有串行锁，依据是预览摘要链。页面只在重新预览的摘要与未决导入完全一致时才用原键重放；服务端发现同名邮箱已存有不同的凭据指纹，说明该邮箱由另一个请求写入。服务端还会在同一事务内重读回执，作为纵深防御。

页面只凭这个信号、经操作者确认后解除"提交结果待核对"。规格：gcloud/docs/superpowers/specs/2026-09-26-reg-factory-not-committed-signal-design.md。

## 前提（任一被破坏，此前的解除结论作废，须人工按请求键核对）
- 不发生丢失已提交事务的数据库回退（异步复制故障切换、按时间点恢复、克隆替换）。
- 滚动发布期间新旧代码对 5 处执行相同的前提判断。
- 下列数据写入后只增不改、不删。产品代码与迁移由静态守卫 `tests/test_onboarding_not_committed_guard.py` 约束；人工 SQL 不受约束，禁止人工修改：
  - `operation_receipts`：不得删除、归档、按保留期清理或改写；操作者、动作、任务、请求键与请求摘要写入后不改。回执缺失会让已提交的原请求被误判为未提交，导致重复执行。
  - `mailbox_registry`：不删除；`email_norm`（唯一）、所有者、凭据指纹写入后不改；`version` 只做 +1。
  - `onboarding_tasks`：不删除；`execution_scope` 不改；`version` 只做 +1。
  - `global_configs`：只追加，不修改、不删除；`revision` 唯一；新配置的 `created_at` 严格大于现有最大值（由 `pool_config.replace` 保证，人工插入配置行会破坏此前提）。
- 应用角色 `rf_onboarding_app` 永不授予上述 4 张表的 DELETE/TRUNCATE，也不授予 `global_configs` 的 UPDATE（`tests/test_onboarding_pool_schema.py` 中 `test_app_cannot_delete_or_truncate_not_committed_proof_tables` 核对）。
- 保留期清理、数据修复、归档任务不得触碰上述 4 张表；确需变更时，先停用解除功能（回滚后端 5 处信号），再重新审查本设计。

## 不会带信号的情况
迁移或数据库环境异常、锁超时、请求键冲突、资源占用、fixture 诊断任务命令、所有 422/401/403/429/5xx。页面保持冻结并提示"原请求仍待核对"。

## 回滚
去掉后端 5 处信号即可；前端只认信号，没有信号时回到"任何拒绝都冻结"。
