# P1c Task4b-1：邮箱列表验证记录

## 结论

2026-09-24：只读列表和签名分页已实现，但**主线程受影响回归为 91 项、1 项迁移初始化 ERROR，未通过阶段验收**。不要用作者 28 项通过、纯测试通过或独立代码审查替代这个结果。此前 Task4a 的 531 项 / 2 ERROR 也仍然保留，未修复宿主存储稳定性。

本次没有邮箱池页面、HTTP、新表、字段、解密或真实外部调用。仅内部合成环境服务；无提交、推送、生产迁移、部署或支付。详细行为、读表字段与回退见 [运维说明](../operations/onboarding-p1c-mailbox-reads.md)。

## 基线与完整变更

工作树 `/workspace/reg-factory/.worktrees/gcloud-p1a`，分支 `feature/gcloud-p1a`，HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`。本批基线是私有 `task4b-read/baseline/`，不将全部 untracked 误算成新改动。

| 文件 | 完整本批行范围 / 业务变化 |
|---|---|
| `onboarding/mailbox_read.py` | 新增 291 行。1–39 类型/常量/输入文字边界；40–89 filters/规范编码/UUID/时间；90–142 filters MAC 与严格 cursor；143–174 一条 owner 过滤的数据 SELECT；175–246 固定字段 DTO 严格校验；247–291 实时鉴权、读取、末次环境/MAC/TTL 复验及 COMMIT 后返回。原来没有此服务。 |
| `onboarding/mailboxes.py` | 仅新增末尾 217–219 三行，显式导出 Page/list_page；原 216 行字节完全不变，导入、MAC、幂等和历史规则不变。 |
| `tests/test_onboarding_mailbox_cursor.py` | 新增 176 行/11 组，纯 helper/DTO 验证，无 PG；没有业务入口变化。 |
| `tests/test_onboarding_mailbox_reads.py` | 新增 286 行/17 组真实 PG 测试，安全列表/分页/保守占用/事务边界；合成 task seed 不是已实现 Task5 writer。 |
| `tools/onboarding_test_runner.py` | OFFLINE 增 mailbox_cursor、POOLS 增 mailbox_reads；旧组、no-skip 和 legacy 环境保护不删。 |
| `tests/test_onboarding_test_runner.py` | 同步精确模块集合；完整 inventory/唯一性断言保留。 |
| `docs/operations/onboarding-p1c-mailbox-reads.md` | 操作、所有读取字段、隐私/占用/游标及回退边界；无运行操作。 |
| `docs/verification/onboarding-p1c-mailbox-reads.md` | 本记录；不改变业务规则。 |

共 8 个仓库文件。无 DDL；安全底座、audit、RequestMac、001/002 原文保持。六份代码/测试 SHA 在 `root-prefinal-source-sha.json`；主线程回归终态逐一核对相同。规格审逐行覆盖六文件全部变更和真实底层依赖，见 `spec-review.md` 与 `spec-review-source-sha.json`。

## 用户操作与规则影响

当前 `webui/static/onboarding.html` 未修改。页面仍只有原保护模式诊断/合成任务功能，没有能调用这次列表的邮箱池按钮。

```text
internal list -> live owner/read permission -> one data SELECT
              -> fixed mailbox/platform DTO -> final policy/MAC/TTL -> COMMIT -> Page
same filters  -> signed cursor -> next keyset page
other owner / changed filters / invalid cursor -> reject
expired lease with logical task hold          -> occupied, not free
```

将来页面选平台只过滤既有状态，不按平台拆库，不默认只查 UNUSED，不伪造缺失状态。搜索 `%`/`_` 是字面字符；分组空字符串表示精确空组。只有本人邮箱可见。Page 顶层 frozen 不等于内部 dict 全部深度不可变，也不等于跨页数据库快照。

变更影响只限未来邮箱管理读取调用方与新测试。旧注册、付款、代理、导出、模型调用均未接入；没有新增 HTTP 请求/响应契约或 i18n 文案。真实认证仍有 operator FOR SHARE 与 session FOR UPDATE 锁，不宣传无锁读取。

## RED/GREEN 与中间失败

所有证据在 `/workspace/gcloud/.local/rf-p1c/task4b-read/`，不同次执行分别保留：

1. `cursor-red-01.log`：8 组缺 module/helper 的断言 RED；`cursor-green-01.log` 为 8 PASS。
2. `list-red-01.log`：PoolCase 初始化成功后，明确缺少 list_page 的 1 项业务入口断言失败；不是用环境错误冒充 RED。
3. `list-green-01.log`：15 项中 14 通过，1 项 TTL 测试自身错误（Transaction context 中显式 commit）。`list-ttl-fix-01.log` 又未证实 real Lock wait 而失败；作者解释为跨角色 pg_stat_activity 等待信息不可见，原日志本身未独立证明该原因。两份失败不删除；后续改用精确 pg_locks 观察，未改变产品超时/权限。
4. `list-unicode-red-01.log`：320 个合法原始 `İ` lower 后扩张，规范化后的值再次被原始长度限制拒绝；是真实产品反例。改为 MAC 入口接原始输入、各自单次规范化，保持 SQL 与 MAC 语义一致。
5. `list-targeted-green-02.log`：上述 Unicode 修复和真实表锁 TTL 用例 2 PASS。
6. 作者最终 `affected-green-01.log`：28 项 / 17.987 秒 / OK（11 纯 cursor/DTO、17 PG list）。追加 DTO 负测是覆盖补充，不声称每条都有独立 RED。
7. root runner 注册 `runner-red.txt` 真实缺模块集合 FAIL，`runner-green.txt` 同项 1 PASS；`root-pure-01.log` 独立纯测试 17 PASS / 0.841 秒。既有 FastAPI 弃用和文件 ResourceWarning 保留，不称零告警。

TTL 验证用真实 mailbox_registry ACCESS EXCLUSIVE 锁；`pg_locks` 明确关联 app backend PID、该 relation、NOT granted，再跨 900ms idle TTL 释放锁，结果为 UNAUTHENTICATED。不是只用 sleep 猜测正在阻塞；finally 回滚 blocker 并 join worker。

## 主线程新鲜受影响验证（未通过）

```text
.venv-onboarding/bin/python \
  /workspace/gcloud/.local/rf-p1c/task4b-read/root-affected-suite.py
```

一次执行既有 parser、RequestMac、secret types、runner 及 cursor、reads、import、import races 共八模块，不是反复重跑旧 531 项求绿。所有测试 IDs 唯一，无过滤/跳过。包装器只在迁移调用边界观测原始 SQLSTATE/等待和固定取消原因，不改变 SQL、kwargs、事务、超时、fsync 或 JIT。

| 指标 | 实测 |
|---|---|
| 终态 UTC | 2026-09-24T10:59:31.445769+00:00 |
| 数量 | 91 个唯一 tests；90 通过 |
| 失败 / 错误 / 跳过 | 0 / 1 / 0 |
| unittest 耗时 / 包装器总耗时 | 76.571 / 76.635715 秒 |
| exit / success | 1 / false |
| 迁移观测 | 132 次：87 APPLIED、44 UNCHANGED、1 MIGRATION_FAILURE |
| 版本分布 | 88 次 001、44 次 002 |
| 观察器错误 / 函数绑定恢复 | 0 / true |

错误是 `MailboxReadTests.test_empty_page_frozen_no_writes_or_session_refresh` 的 setUp。此次完整观测明确定位 **002 SQL 批次**：QueryCanceled、SQLSTATE 57014，原生白名单原因 `statement_timeout`，8.222836 秒；158 次采样中 157 次 `IO/DataFileImmediateSync` 且进程 D，采样阻塞 PID 列表为空。**空页测试体本次未执行**。不同于旧 Task4a 未观测的 apply_all 错误，这次可确认版本和取消原因；仍不能反推旧错误，也未定位底层设备/宿主负载根因。

本轮 22 组 import 与 5 组真实 spawn race 均执行通过，但不能据此把 91 项整体失败改写成通过。未对空页用例再循环求绿，未降低检查或增加超时。

## 数据复核与未验证项

主线程最终只读检查：永久 schema 14 表 / 141 列，仅 001 账本 1 行，其余 13 表各 0；临时 schema 0、测试密钥目录 0；JIT on / above_cost 100000。永久库未升级 002。原始证据 `final-database-check.json`。

- COMMIT_UNKNOWN 当前列表用例是在 UoW body 中注入 ServiceError，证明传播和不重试，**不是 COMMIT 成功后丢 ACK**；不借 Task4a 的另一测试冒称本例做了真实 lost-ack。
- occupied 测试覆盖过期 lease、终态 task、owner NULL、task NULL、非终态 pool task fallback 与 CANCELLED_SAFE；未逐项穷举所有非安全状态/hold_reason。SQL 不依赖 TTL 或 hold_reason，不据枚举简化改变事实。
- 末次 policy 检查在代码路径上存在，底层 policy 有既有检查；本模块尚未定点注入 data SELECT 后的环境失效。MAC 中途变化和真实等待跨 idle TTL 已有本模块实测；绝对期限复验依赖共用 security。
- 代码独立审查无已确认阻断项不等于测试、环境或生产通过。独立质量审查报告 `quality-review.md` 无 critical/important 阻断项，另跑纯测试 17 PASS / exit 0；源 SHA 在 `quality-review-source-sha.json`。不替代 root 终态。
- 无生产容量/分页快照/密钥轮换/OS 隔离/真实数据和联网验证；无 metadata、history、平台改密、Task5 claim、卡池或页面。

## 回退与后续

仅回退本批八文件的精确增量（其中三份是新增源码/测试文件），保留前期导入和全部用户工作；不做 reset/clean/清空数据库，不删除凭据密钥。无本批 DDL 回滚 SQL；清理临时数据必须经过 manifest 归属校验。

先保留全部失败和磁盘同步观测，解决稳定测试运行环境的门槛；可继续独立离线研发，但不报阶段验收完成或整体上线交付。HTML 是本批变更说明，实际应用界面并未新增功能，渲染检查不能算池页面 E2E。
