# 真实执行适配层：2026-09-29 实施记录（不是上线验收）

## 状态

本切片已经编写浏览器、Google 控制平面、Sub2API 停用导入及持久操作许可组件；尚未将 8799 原页面切换到真实执行。不能把本地 HTML / MockTransport / 隔离 PostgreSQL 通过当成 Google 开通成功。

```text
原页面运行按钮
    |  [尚缺 live 配置、真实资料导入及 worker 组合]
    v
任务当前步骤 / 账号归属 / 配置 / 资源版本 / lease fence
    |
    +-- 持久 INTENT -- 确认提交 --> 单次进程内许可
    |                                  |
    |                                  +-- 再核验权限、取消、步骤、租约
    |                                  +-- 摘要匹配固定目标计划
    |                                  +-- 浏览器 / Google API / Sub2API
    |                                                     |
    +<----------- 保留结果/核对，不自动重发 <--------------+
```

## 文件与职责

- `common/browser.py`：新增显式 `automation_mode="normal"`，跳过本模块 stealth、指纹参数和 context 流量过滤，隐藏 CDP 地址；旧调用默认行为保持。normal 拒绝 extreme 启动节流。
- `onboarding/google_browser.py`：普通登录表单与联系人字段填写；每次填表必须严格通过 guard，登录提交须持久意图 callback。暂停不关闭现场。验证码/未知页不绕过。未实现联系人保存、支付、试用激活或验证码渠道。
- `onboarding/google_cloud.py`：固定项目、billing、Vertex API、SA、IAM 和密钥接口；只读核对。没有模型请求、提权或组织策略写入。
- `onboarding/sub2api_import.py`：仅批准的本机 8800、分组 2 与补丁制品合同；`create_disabled=true` 原子导入，读回 inactive、schedulable=false、身份与分组。官方 DTO 不返回私钥，只核验凭据存在性标记。不会发送模型测试或启用调度。
- `onboarding/live_effects.py`：session-bound 的持久记录与单次许可；不是脱离登录会话的 worker 授权。
- `onboarding/migrations/003_live_effects.sql`：新增显式模式与操作记录，不修改 001/002。
- `migration_catalog.py` / `migrate.py`：登记 003 checksum，允许显式 target 3；默认 target 保持不变。应用角色仅 SELECT/INSERT/UPDATE journal，无 DELETE/TRUNCATE。
- `tools/onboarding_test_runner.py`：纳入新的单元、数据库、跨适配器测试；单独 browser suite 使用本地拦截 HTML，不登录真实账号。

## 数据字段与原规则影响

| 表 / 字段 | 变化与业务含义 |
|---|---|
| onboarding_tasks.execution_mode | 新增，默认 synthetic；历史任务不会因进程标志改变而变成真实操作 |
| task_mode_identity / live_task_pool_scope | 复合唯一键与 CHECK；live 任务仍属于原 pool 范围 |
| live_effect_journal.task_id/task_mode | 复合 FK 只关联 live task |
| step_key/action | 必须与当前任务步骤对应；白名单不能当成跨步骤授权 |
| generation/config_revision/resource_revision | 任务与资源修订绑定，旧版本不能继续执行 |
| effect_digest | 固定非敏感目标计划摘要，绑定项目、billing、SA、地区、下游目标等；不以秘密原文做审计摘要 |
| request_key/request_hash | 幂等查询；换请求键不能绕过同一步骤或未知结果 |
| fence_bindings | 记录 mailbox/card 当前租约 owner/fence/revision；过期不等于能释放 |
| phase/result_code/evidence_ref | INTENT、UNKNOWN、SUCCEEDED、FAILED_CONFIRMED、CONFLICT；只保存固定结果码和不透明证据引用 |
| created_at/observed_at/updated_at/version | 数据库时间及版本，不依赖浏览器宣称完成 |

一次步骤成功只改变 journal，不会把整任务置成功或自动释放邮箱。所有关联操作按 CONFLICT > UNKNOWN > INTENT > HELD 聚合占用状态，迟到结果不能把另一步的未知占用降级。发生旧步骤冲突后，下一步已签发但尚未消费的许可也会被阻断。

## 已修复的交叉审查问题

1. `POST :getIamPolicy` 是只读请求；403/404/412 不能证明以前的 IAM 写入失败，核对保持 UNKNOWN。
2. 密钥生成后保存失败保留已验证的资源名，以便核对孤立 key；不重复生成、不自动删除。
3. Google key 输出与 Sub2API 输入统一 40 位 key ID、RSA 私钥及官方 token URI。
4. 许可同时绑定动作与目标摘要；导入的 Idempotency-Key 必须等于 journal receipt ID。
5. 确认 COMMIT 后才签发许可；对象复制、恢复读取、COMMIT_UNKNOWN 都不能重新发送。
6. 填写 Google 密码前必须核对页面当前账号与目标邮箱精确一致；guard/intent 等待后再次检查，切号后不点击或重发。不把普通账号列表当当前账号，不推断 Gmail 别名。
7. normal 浏览器路径自己的 provider/CDP 异常及重试日志仅给固定码，legacy 行为不变；取消/退出仍保留语义并清理本次 profile。
8. SQL 静态保护器区分精确字面量 GRANT 权限声明与 UPDATE DML；动态 f-string/拼接和夹带第二语句仍不豁免。

## 显式迁移超时调整

统一回归中，未触及的旧 B1 用例在 001 迁移 setup 出现 PostgreSQL statement timeout，单例也复现。迁移 DDL 的 statement_timeout 从 5 秒单独改为有界 30 秒；普通 storage.unit_of_work 仍为 5 秒，lock 2 秒、idle 10 秒、单事务、校验和与 COMMIT_UNKNOWN 语义均不变。不自动重试迁移。

这增加显式维护操作可能的持锁时间，不放宽业务、权限或计费边界。整段 DDL 4.618 秒的观察值不是“某条 SQL 超过 5 秒”的单独证据；保留 PG 超时和原单例失败日志。30 秒若仍超时应继续诊断，不继续扩大限制或跳过验收。

## 尚未完成的上线门槛

- 真实池资料导入、加密存储、task-bound 解密消费。原 synthetic 类型不能直接放宽。
- live 配置和启动策略、原 UI/API 路由组合；没有把生产入口接成可执行状态。
- 后台授权快照、batch→worker 占用交接、心跳、进程/浏览器停止证明、跨进程恢复。
- 真实 Google 页面 DOM、当前浏览器账号身份核验、联系人保存、绑卡与验证码渠道。
- 官方试用资格及赠金证据核验；billingEnabled 不能代表赠金到账。
- 全流程步骤推进、外部引用/密钥引用持久化、迟到结果专用接收与取消闭环。
- 单独的只读恢复授权：当前集成测试的 `_dispatch_guard` 只允许 INTENT，不适合 UNKNOWN/PENDING 后的核对。恢复 guard 必须核对当前 owner/fence/固定目标，但不能重新签发发送许可；暂停或取消后的只读核对与继续外部写入须分开。
- 真实 token_provider、secret_sink 和服务器制品 guard 接入。现在都是必须显式提供的内部合同，不是已连好的依赖。
- 单账号真实开通至停用导入的端到端验收；收费模型调用依然未获单独批准，不发送。

## 变更范围和恢复

本轮没有对 Google、银行卡发起真实写入，没有重启 8799、没有把 preview schema 升级到 003，没有迁移历史邮箱/卡数据。003 只在可清理的测试 schema 中验证。恢复代码应只撤销本轮列出的新增组件/精确增量，保留此前用户修改；不能用整个工作树 reset。不可通过删除 journal 来“重试”未知外部操作。

## 后续接通顺序（不把缺代码归因于用户资料）

1. 完成真实运行配置和同库认证；`settings.py` / `storage.py` / `webui/onboarding_auth.py` / `webui/server.py` 目前只接受测试边界。原 preview 会话不能跨库移植。
2. 原资源管理页接真实资料加密入口、任务绑定取用和 Cloud key 安全 sink；不放宽现有 synthetic 类型，不向任务 DTO 返回明文。
3. 原 `pool_batches` 服务由可信服务端模式创建 live task；不能由客户端布尔字段将历史记录升级为可执行任务。
4. 将 `batch:<batch_id>` 已占用邮箱原子交给 worker 并提升 fence。现有 RESERVED 邮箱不能再用面向空闲邮箱的 `claim()`；增加心跳、停止证明及 started-task 取消核对。
5. worker 逐步固定计划、提交 journal、执行、保存外部引用、核对结果，再推进下一步；补真实 token、Sub2API 进程/制品 guard、独立只读恢复授权。
6. 接原运行按钮、后台调度与右侧持久日志。Google 联系人保存、试用激活、绑卡和授权 OTP 通道仍须实际实现和真实页面验证，不能靠隐藏提示或补用户资料冒充已实现。

现有 Google 身份且已有可用 billing 的单账号路径可以先贯通云资源至停用导入；此验收不覆盖首次注册、首次结算、试用赠金申请。全部业务完成之前，原页面不宣称“真实开通服务已接通”。
