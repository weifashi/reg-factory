# Pool 历史任务入口验证记录

日期：2026-09-24 UTC。**本批受影响回归未全通过，整体未达到上线标准。** 新增历史入口的11项通过不替代整批或真实集成验收。

## 范围与证据

工作树 `/workspace/reg-factory/.worktrees/gcloud-p1a`，分支 `feature/gcloud-p1a`，基线 HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`。未提交、推送、部署或永久迁移。

私有证据目录 `/workspace/gcloud/.local/rf-p1c/task5b-history/`，不包含于公共API：

| 检查 | 实际结果 | 证据 |
|---|---|---|
| 作者初次真实 PoolCase 后缺模块 RED | 1 FAIL、0 ERROR | repository-author/red.log |
| 作者尝试1 | 11项、5 ERROR；测试时间约束和cursor seam问题，后续修正；保留失败 | repository-author/green-attempt-1.log |
| 作者尝试2 | failfast跑7项：6 PASS、1 setup ERROR；后4项未跑，不能称全绿 | repository-author/green-attempt-2.log |
| config key精确类型缺陷 RED | 真实临时PG setup后，1 FAIL、0 ERROR；是decoded-row seam，不是物理数据损坏 | repository-diagnostic-red/run.log |
| 最终静态规格/质量审查 | SPEC补核02通过；质量无新增阻断缺陷，3项纯测试通过，未替代PG验收 | repository-spec-review.md、repository-quality-review.md |
| 根代理最终受影响检查 | **132项：131 PASS、0 FAIL、1 ERROR、0 SKIP；147.961秒，exit 1** | repository-root-final-01/run.log、result.json、run.exit |
| 最终源码一致性 | 14个实现/测试/关键依赖 SHA 一致，无漂移 | repository-root-final-01/approved-source-sha.json、root-statistics.json |
| 最终只读数据库核对 | 14表141列，仅001原校验和；其他13表均0行，临时schema和key fixture均0 | repository-root-final-01/database-check.json、database-check.exit |

最终执行入口为 `.venv-onboarding/bin/python /workspace/gcloud/.local/rf-p1c/task5b-history/repository-root-final-01/run.py`，外层600秒限时；session84505已终态exit1。入口含一次性排他输出和源SHA门禁，不应原样重复执行覆盖证据。

9个模块：pool_repository(11)、pool_contracts(12)、pool_secret_types(6)、repository(15)、security(30)、pool_vault(21)、pool_config(27)、test_runner(6)、contracts(4)。独立发现清单132个唯一ID，最终实际运行132项。新增历史入口11项均通过，包含坏decoded-row修复、历史漂移、fixture/owner隔离、实时认证、真实后端等待、锁顺序、事务回滚及配置历史引用。未测试任何真实Google/支付/Vertex/Sub2API动作。

## 唯一最终报错，不得省略

`test_onboarding_pool_config.PoolConfigTests.test_strict_service_inputs_and_no_side_effects` 在 `setUp → 001_core_batch` 报错；测试正文未执行。PID3508792，UTC12:47:23.054575开始的001批量DDL，6.054644秒后原生 `QueryCanceled / SQLSTATE 57014 / statement_timeout`，上层为 `DEPENDENCY_UNAVAILABLE`。116个采样中114个为 `IO/DataFileImmediateSync`，对应进程D状态；采样未见未授予锁。

这证明该次初始化的同步IO等待与超时，不足以证明宿主硬件、QoS或其他根因，也不能用采样未见锁断言整段没有任何锁竞争。190次迁移观测：136 APPLIED、53 UNCHANGED、1异常；137次001、53次002，监测均结束、替换绑定已恢复、diagnostic_errors为空。

本次没有扩大超时、改变fsync/JIT、杀共享进程、跳过失败、循环重跑。历史Task5a的255项中1 FAIL/1 ERROR、其后单次竞争诊断未复现，以及作者未确定迁移阶段的setup错误都继续保留，不因本轮新增11项通过而关闭。

## 验收边界

可以确认本次11个历史入口测试的结果和静态审查；不能宣称本批整体通过、数据库环境已修复、任务命令/租约/消费器完成或已上线。下一步允许推进独立开发和合同细化；上线前仍需解决测试环境稳定性、未闭合竞争失败，并完成全链路真实集成与授权部署验收。

当前页面无变化；没有新增数据库字段。28个字段是已有历史关联图的内部投影，不得直接作为HTTP响应或执行许可。操作与回退说明见 `docs/operations/onboarding-p1c-pool-history.md`。
