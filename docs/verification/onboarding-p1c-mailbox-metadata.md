# P1c Task4c：邮箱元数据验证记录

## 状态：本批受影响回归通过，整体上线验收未完成

2026-09-24：内部邮箱分组/停用服务及审查发现的输入快照竞态修复完成。修复后独立规格/质量复审无未解决阻断；root一次新鲜受影响回归179项全部通过、0FAIL/0ERROR/0SKIP、exit0。作者旧版24项2个迁移初始化ERROR保留，不能因此宣称历史环境故障已修复或整个项目已达到上线标准。

仅合成隔离环境；没有 HTTP/页面、生产数据、联网 Google/Billing/Vertex/Sub2API、真实模型费用或部署。此前 Task4a 的 531 项 / 2 ERROR、Task4b 的 91 项 / 1 ERROR 仍然保留，不能被本批通过覆盖。用法、字段和回退见 [运维说明](../operations/onboarding-p1c-mailbox-metadata.md)。

## 基线与逐文件范围

工作树 `/workspace/reg-factory/.worktrees/gcloud-p1a`，分支 `feature/gcloud-p1a`，HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`。真实本批基线为私有 `task4c-metadata/baseline/` / `baseline.json`，不是把所有 untracked 误算成本批新文件。

| 文件 | 本批完整范围 / 业务影响 |
|---|---|
| onboarding/mailbox_update.py | 新增150行：1–24 模块/依赖/常量；25–51 精确dict先复制、严格校验同一快照；52–60 首末MAC；61–90 回执定位/固定字段/摘要验证；91–105 入口与隔离/MAC；106–118 实时认证/行锁/先重放后CAS；119–146 候选UPDATE、终态回执、冲突回滚及最小审计；147–150 最终复验、COMMIT后返回。 |
| onboarding/mailboxes.py | 仅新增220–222三行显式update导出，原219行字节保持，导入/列表语义不改。 |
| onboarding/audit.py | 固定动作白名单仅增加mailbox.update，不扩大summary字段。 |
| tests/test_onboarding_mailbox_update.py | 新增487行/21项核心PG合同与故障测试 + 3项纯输入快照竞态测试；无直接产品规则。 |
| tests/test_onboarding_mailbox_update_races.py | 新增137行/3项spawn并发，两真实独立session/backend；无直接产品规则。 |
| tools/onboarding_test_runner.py | POOLS注册update/update_races，不删旧分组、不放宽skip判定。 |
| tests/test_onboarding_repository.py | 新动作允许/自由输入摘要拒绝的纯负测。 |
| tests/test_onboarding_test_runner.py | 同步精确POOLS集合，旧inventory和legacy保护保留。 |
| docs/operations/onboarding-p1c-mailbox-metadata.md | 内部操作、完整字段/安全合同与最小回退，无运行副作用。 |
| docs/verification/onboarding-p1c-mailbox-metadata.md | 本记录，无产品规则变化。 |

初版8份代码/测试冻结在 `source-freeze-01.json` / `frozen-01/`；审修后当前版在 `source-freeze-02.json` / `frozen-02/`，保留初版证据，不覆盖。原 read、pool_vault、request_mac、security、001/002 SHA 和 mailboxes 219行前缀已由root检查不变。无新表/字段/索引/迁移；无凭据、历史、平台、任务或租约写入。详细逐行分类见最终HTML底账；独立审查不能替代新鲜测试。

## 真实页面与业务结果

当前页面仍是 `webui/static/onboarding.html` 的保护模式离线控制面：诊断、本人合成任务查询、暂停/只读核验/取消后续步骤。未新增邮箱分组、停用按钮。报告中邮箱操作帧必须注明“内部服务结果示意，页面未接线”，不能以它证明浏览器端到端完成。

```text
new metadata command -> real owner/manage -> mailbox row lock
                     -> old receipt? replay : expected_version CAS
                     -> terminal receipt + version-only audit
                     -> final policy/MAC/auth -> COMMIT -> minimal result
same key / same body -> original result; no second update
new key / stale ver  -> VERSION_CONFLICT; no partial write
occupied + disabled  -> hold and in-flight task stay intact
```

## RED/GREEN 与完整中间失败

证据均在 `/workspace/gcloud/.local/rf-p1c/task4c-metadata/`，0600私有文件；不把每轮通过数相加成最终唯一测试数。

1. `core-red-entry.log`：真实PoolCase初始化/seed成功，1项因缺update入口FAIL；这是业务RED，不是环境初始化报错。
2. `core-green-attempt1.log`：15项、3FAIL/2ERROR。测试修正：默认None与显式None混用；保留字段tuple索引错误；损坏JSON/过期会话seed先被既有数据库CHECK拒绝，未达到目标分支。shell没有python导致原定修正脚本未执行，后改用项目venv。修正限测试，不拓宽服务或库约束。
3. `core-races-check2.log`：21项、20通过/1FAIL。仅新增schema校验和测试预期写错；既有policy对校验和漂移返回VERSION_CONFLICT，修正预期，不改底层错误合同。
4. `core-races-final.log`：最终24项、22通过/2ERROR、80.808秒。两个setUp失败：final_policy_search_path_and_key_replacement_rollback、same_value_new_key_increments_replay_old_version_and_body_conflicts。均是PoolCase.setUp→apply_all(target_version=2)→apply_migration→DEPENDENCY_UNAVAILABLE，业务体未进入。该日志没有native SQLSTATE/准确失败版本证据，不补写“已定位002/磁盘超时”；apply_all还可能执行001检查。三组spawn全部通过。本轮失败不得用前两轮同项通过替代。
5. `shared-red.txt`：2项均按预期FAIL（缺审计动作、runner未注册）；最小修改后 `shared-green.txt` 2通过。
6. `root-pure-01.log`：root新鲜10项纯接口/runner验证通过，0.679秒；不连接PG，不证明业务数据库或完整验收通过。

## 故障与并发证据的真实含义

作者测试包括真实资源行锁及审计表锁跨TTL、SQL CHECK确达故障后的整笔回滚、末次权限撤销、MAC替换/与AES候选复用、真实search_path修改后最终policy拒绝、损坏回执投影和不应出现的同摘要候选冲突。两个最终setUp错误中的业务体未进入，不能说最终24项覆盖了其分支。

COMMIT_UNKNOWN用例先让真实COMMIT成功，再由测试专用连接wrapper抛一次OperationalError模拟确认丢失；确认资源/回执已持久化，显式同键再请求零额外更新/审计。这不是断网、kill后端或物理网络故障实验。

三组spawn使用不同真实会话/后端，在真正尝试邮箱锁前同步：同邮箱同键只一次更新、同邮箱异键同版本一方CAS失败、异邮箱同键一方幂等冲突。异邮箱组在候选UPDATE之后/回执INSERT之前再次同步，证明双方产生候选写而输家version/updated_at/audit全部回滚；不靠同session锁伪装竞争。

## 存储观察与未完成门禁

`host-io-audit.md` 是独立被动观察：180样本，11:07:14.367882—11:10:13.370192 UTC。已确认可见 `/`、home、workspace 都在 dm-0→sda3→sda；无现成不同持久化挂载。看到当前窗口I/O压力/队列/私有PG内核等待，但与旧失败不同窗，没有历史因果证据，未证明根因或修复。sdb未挂载、用途未知，未触碰；不将tmpfs当持久替代盘。

没有提高timeout、关闭fsync/JIT、重启其他PG、kill其他服务、读取其他业务日志或选择未授权远端。最终受影响回归、独立双审、永久库/临时schema残留检查及源SHA已补证，见下节。HTML报告的布局/公网证据另存私有目录，不等于产品页面端到端。完整生产provider/OS隔离/真实数据/外部集成/上线授权仍未完成。

## 独立审查发现与修复（P2）

首次规格静态审查未发现阻断，质量审随后独立复现 `_inputs` 的校验/复制时序漏洞：先校验调用方可变字典、最后才copy，其他线程可在期间把已验证分组改成控制字符并注入未知字段，结果带入未校验数据。原002只有分组长度检查，不能依赖数据库兜住这个错误。纯helper复现只控制真实线程调度，没有替换业务返回，也没有执行PG。

最小修复：确认exact dict后立即copy，只对这一副本执行非空、字段白名单、精确值类型和控制字符校验，并返回同一副本。复本之后caller修改不能改变SQL/MAC使用的内容。只改metadata模块和其测试，既有数据库/安全底层不变。

`input-snapshot-red.log`：3项中1项按真实竞态FAIL；`input-snapshot-green.log`：3通过。root随后新鲜 `root-pure-02.log` 合计13项全部通过（3快照+4接口+6runner），未连接PG。首次规格审遗漏此项的记录保留；修复后spec/quality当前SHA复审及root唯一新鲜PG已补齐，见下节。

## 修复后根线程最终验证

唯一新鲜受影响命令：

```sh
PYTHONPATH=.:tests .venv-onboarding/bin/python /workspace/gcloud/.local/rf-p1c/task4c-metadata/root-affected-suite.py
```

UTC 2026-09-24 11:21:21.101991—11:24:09.040301；**179项、179唯一test ID、0FAIL/0ERROR/0SKIP、167.862秒、exit0**。`root-affected-tests.txt`为终态输出，`root-affected-result.json`为唯一ID与终态，`root-final-statistics.json`为分模块汇总。这是13个受影响模块，不是项目全量或旧531项重跑：parser14、request_mac10、pool_secret_types6、runner6、cursor11、reads17、import22、import_races5、update24（21PG+3纯）、update_races3、repository14、security30、storage17。

测试期只读迁移观察覆盖两个实际调用绑定，不改SQL/参数/事务/超时，不自动重试；结束恢复原绑定。`root-affected-migrations.jsonl`：233次调用，165 APPLIED、68 UNCHANGED、0异常；版本001165次、00268次；最长4.797534秒，观察器诊断错误0。所有代码SHA在执行前后均匹配 `root-prefinal-source-sha.json`（即frozen-02）。源漂移检查不是用旧结果替代验证。

`spec-review.md`与`spec-review-source-sha-v2.json`明确首审遗漏及修复后逐行复审；`quality-review.md`与`quality-review-source-sha.json`记录初版P2、复现、最小修复、本人新鲜纯测试及当前8源匹配。无托管orchestrator信封，不编造run/stage成功状态；这是本地独立审查。

随后只读 `check-final-db.py`：永久库仍14表/141列，schema_migrations只有001原SHA（3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782），其余13表0行；临时schema=0、剩余fixture key目录=0；JIT=on、阈值100000。002只应用于测试工厂授权临时schema，未升级永久库或生产。无提交、推送、部署、支付或真实账号操作。

同窗被动I/O采样使用独立 `host-io-regression/`，与首次180样本分开，不为得到绿色重复旧全量。root在测试结束后创建STOP正常收尾；详细终态/UTC/PID对应关系保存该目录。**这次没有复现迁移错误，不能据此证明原磁盘问题已解决**，也不能用它替代宿主根因诊断、全量稳定性和真实集成验收。
