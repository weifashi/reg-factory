# B1 验收记录

日期：2026-09-24。状态：B1 本次验收暂不通过；上一轮技术复验通过，但本次串行复验再次发生初始化迁移超时。B2 保持暂停。

## 基线与授权

- 仓库 `/workspace/reg-factory`；隔离 worktree `.worktrees/gcloud-p1a` / `feature/gcloud-p1a`。
- HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`；用户授权 B1 持久任务/租约/回执，不含提交、推送、部署或真实外部调用。
- 执行前 P1a+B0 共 29 个已有改动文件快照：`/workspace/gcloud/.local/rf-b1/baseline-manifest.json`。
- B0 迁移文件不修改，复用既有 14 表。全部测试只在专用实例临时 schema 内造合成数据。

## 分工与证据

- root：repository/audit/test support，实际 PG 故障补测，最终串行复验与报告。
- b1_leases：lease 与 spawn 竞争/死锁；交叉审查 repository/approvals/receipts。
- b1_receipts：approval/receipt 原子消费；交叉审查 leases/coordinator。
- b0_spec_review：本轮为 coordinator 实现者（不是自审批准人），闭合执行器与一次许可、命令和故障注入。
- 独立审查发现问题反馈原作者修复；最终依靠 root 真实命令输出，不用口头“绿”代替。

所有原始日志：`/workspace/gcloud/.local/rf-b1/`。RED 包含接口缺失与断言失败；仅由共享 stub 导致的 NotImplementedError 不计业务行为 RED。新增故障验证针对既有 B0 UoW，不假造已有逻辑的 RED。

## 实施边界差异（明确记录）

1. repositories 独立为 `onboarding/repository.py`，而不是增大 B0 storage.py；既有安全连接/UoW 不变。
2. B1 尚无 SecretStore，故 `secret_refs` 比完整 P1b 合同更严格：必须空；B2 再实现经过授权的引用。
3. B1 `FixtureActor` 是内部合成身份，不是 B2 Actor/会话，不对外挂 HTTP。
4. `FixtureStopEvidence` 仅用于合成执行者交接，不提供真实进程/浏览器停止验证。
5. 本地一次消费与不重发不等于远端恰好执行一次；INTENT 可能代表尚未发送，也可能代表结果没写回。
6. 本轮未接通新 UI/API、登录、保险库、真实费用审批或下载；旧业务全套回归仍不能冒称完成。

## 最终验证

root 最终串行验证（2026-09-24 06:45 UTC）：

- `run-verification.py` 明确载入 `test_onboarding_*.py`：185 tests / 57.457s，failures=0、errors=0、skipped=0，退出 0。基线 90 + 本批新增 95。
- 模块：repository/audit 12；leases 25；approvals 9；receipts 22；coordinator 25；额外真实 PG fault 2。其它 90 项原 P1a/B0 回归均通过。
- `root-final-tests.txt` 为完整原始输出，`root-final-result.json` 含逐项 test id，不能用测试发现数量替代实际执行结果。
- `compileall`、`git diff --check` 通过；专用 venv 未装 pip，`python -m pip check` 不可用，没有假报通过；改用现有 `uv pip check --python .venv-onboarding/bin/python` 验证依赖。
- `final-database-check.json`：14 表 / 141 字段；001 checksum 与文件一致；schema_migrations 1 行；13 张业务表 0 行；临时测试 schema 0 个。
- 29 个既有文件 SHA-256 不变；主仓库 main tracked 工作区未改；本轮新增 15 个文件，准确行数与哈希见 `manifest.json`。
- Task 2/4 独立 spec+quality 复审：`review-repository-receipts.md`；Task 3/5：`review-leases-coordinator.md`。发现的问题修复后再复核，非作者自审代替独立审查。

### 已复现并修复

缺失 expected_version 绕过 CAS；审批/回执/派发等锁后 lease 失效；确认结果被 UNKNOWN 降级；另一回执成功掩盖 CONFLICT/UNKNOWN；多资源完成后残留 hold；恢复后新 owner 无法核验旧步骤；别资源同 fence 或别动作串步骤；许可未绑定 PID/当前资源代次；审计回滚测试未确认注入点被命中。对应 RED 与 GREEN 保存在证据目录。

### 必须保留的异常记录

分模块回归曾有一次协调器测试 `setUp` 迁移失败，业务测试体尚未进入，原日志已命名 `coordinator-final-attempt-migration-error.txt`，不能称该轮 25 项通过。PG 日志对应 06:44:05 UTC `canceling statement due to statement timeout`。最终无代理并发整套测试全部通过；短时观察未捕获慢活动等待。超时的性能根因没有充分定位，不声称已修复，也没有调大超时或新增自动重试掩盖问题；大规模并行测试稳定性未验收。

协调器 CAS 用例因下层 validator 已由其它代理修复，首次即绿，记录为 `coordinator-cas-upstream-already-green.txt`，不冒充 RED。重启用例是清空本地许可表模拟状态丢失，不是实机重启；COMMIT 回包丢失是提交后注入错误，不是实际网络断链。

详细交付页面：`https://8008--main--wfs--weifashi.coder.tbc.5ok.co/gcloud_onboarding/b1-delivery.html?v=1`。


## 用户验收复验 — 2026-09-24 06:52 UTC

本次执行一次完整串行复验，不改产品代码、不提高超时、不自动重跑掩盖失败。

- 185 项 / 88.433s：184 通过、1 error、0 assertion failures、0 skipped；退出 1。
- 错误用例 `test_unknown_holds_and_never_returns_approval` 在 `tests/onboarding_b1_support.py:22` 的 setUp 调用 `onboarding/migrate.py:65` 失败，尚未进入业务断言。
- PostgreSQL 06:52:42 UTC 对应 `canceling statement due to statement timeout`。与上次不同，本次完整串行仍复现，不能仅归因于多代理并行负载；性能根因尚未定位。
- 原始证据独立保存在 `/workspace/gcloud/.local/rf-b1/acceptance-xPMRCAsY/`（tests.txt / result.json / database-check.json），未覆盖上一轮通过日志。
- 复验后 14 张永久表仍在，迁移版本 1 行，13 张业务表仍空，临时 schema 0 个。没有真实外部调用、提交或部署。
- 验收前后产品源码与测试未改；本次只追加验收记录并更新报告状态。旧的 185 全通过结论只适用于 06:45 那一轮，不能替代本轮失败结果。
- 当前结论：验收暂不通过，B2 不启动。后续应定位具体慢 SQL / 锁等待 / I/O 或 CPU 调度来源，修复或明确环境约束后重新验收；不得仅反复运行直到变绿。

最新验收报告：`https://8008--main--wfs--weifashi.coder.tbc.5ok.co/gcloud_onboarding/b1-delivery.html?v=2`。
