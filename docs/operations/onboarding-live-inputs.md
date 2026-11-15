# 原邮箱与统一配置：真实输入服务接入

日期：2026-09-29。目录：`/workspace/reg-factory/.worktrees/onboarding-minors`，属于原 `reg-factory` 工作树。没有创建独立产品，没有重启 8799，没有迁移其正在使用的数据，没有访问 Google、银行或模型服务。

## 当前到底能做什么

```text
原项目 emails.txt  --只读/精确授权邮箱-->  来源快照 + 带密钥摘要
                                               |
                       当前操作者/文件/密钥复核  |
                                               v
原 secret_objects <--- AES-GCM 加密 <--- 同一邮箱原密码
原 mailbox_registry <- 凭据引用 + 版本
原平台状态            UNKNOWN / HISTORY_UNRECONCILED
                                               |
                       不提升资格、不创建任务 --+

真实全局字段 -- CAS/同一配置锁 --> 原 global_configs(scope=pool)
                                       |
                         v1 历史继续读；v2 严格按版本使用
```

这是内部真实输入服务，不是浏览器开通完成。现有“运行任务”按钮仍未调用新建 live 批次/后台启动链。没有把保存配置或导入密码描述为完成 Google 注册、绑卡、领取赠金。

## 页面 → 操作 → 结果

| 原页面/入口 | 操作 | 本次实际结果 |
|---|---|---|
| 邮箱池原导入页 | 原格式导入 | 原 synthetic 导入规则未放宽；新可信源桥接尚未挂到该按钮 |
| 邮箱池内部可信源服务 | 预览、按源摘要确认导入 | 精确本人邮箱入原表；不领取原文件、不推进游标、不覆盖已有不同密码 |
| 系统配置原页面 | 读取旧 v1 配置 | 行为保持；旧页面不能把 v2 配置作为 v1 保存 |
| 配置内部 v2 服务 | 保存真实结算账号和固定引用 | 追加到同一全局头；旧任务固定旧版本，不接受批次覆盖 |
| Google 任务页 | 点击原运行按钮 | 仍需后续 live 批次/启动接线；本次不假装运行外部开通 |

## 逐文件与业务规则

| 文件 | 规则与影响 |
|---|---|
| `onboarding/live_mailbox_source.py` | 只允许服务端组合的 owner、数据目录、首批精确授权账号。逐级无符号链接打开、文件 0600/单链接、整文件上限、读前后及路径身份核对。复用原解析器，拒绝重复、坏行、平台密码歧义。不会调用原邮箱领取器。 |
| `onboarding/live_input_vault.py` | 新 typed payload、固定数据库策略、同表 AES-GCM 密文。源摘要跨本人登录会话稳定；短期 snapshot 仍绑定原会话。导入、回执、审计同一事务，末尾重新核验来源/权限/AES/MAC。相同 key 重放只读；并发不同 key 同邮箱仅一份密文；不同原密码不覆盖。 |
| `onboarding/live_config.py` | v2 字段精确白名单；已有结算账号云资源 workflow、us-central1、最小固定角色、服务端 Google/Sub2API 引用、180 秒/单并发。费用测试与调度固定关闭。项目/服务账号名由完整任务 UUID 稳定派生，不接受任意 URL 或本机秘密路径。 |
| `onboarding/pool_config.py` | 同一配置头增加版本化解码，v1 写校验保留；另提供完整 v1 guard，不能把“能读 v2”当作“旧任务可以使用 v2”。 |
| `onboarding/pool_batches.py` | 原预检、新建、历史重放均执行 v1 guard；切为 v2 后，旧 v1 历史批次仍按原固定配置重放。 |
| `onboarding/pool_repository.py` | 原任务锁定后读取固定配置时检查 v1，阻止历史任务被改标签指向真实配置。 |
| `webui/onboarding_pool_routes.py` | 旧配置 HTTP 投影应拒绝 v2，不用 v1 字段强行投影导致 KeyError；不通过异常向页面泄露账单/profile。 |
| `tools/onboarding_test_runner.py` 与测试 | 新模块加入明确套件及全文件覆盖断言；并发、跨 owner、源文件变化、授权失效、密钥变化、回执/秘密版本漂移、真实 COMMIT 丢回包均有回归。 |

## 表和字段

没有新增表、列或迁移文件。复用以下数据：

- `mailbox_registry`：新建 `id/owner_operator_id/email_norm/source_type/group_ref/credential_ref/source_fingerprint`；`credential_version` 为 1。Gmail 来源为 `other`，不冒充 Outlook。不同版本秘密不隐式替换。
- `secret_objects`：新建 `kind=mailbox_credential`、`key_version/nonce/ciphertext`，新策略标识 `pool-live-v1:<owner>:mailbox:<id>:v1`。旧密文不改标签；重放同时核对 secret `version/revision`、吊销/过期字段。
- `mailbox_platform_states`：七个平台初始化 `identity_status=UNKNOWN`、`usage_status=HISTORY_UNRECONCILED`，不填第二份 Google 密码引用、不宣称 UNUSED。
- `global_configs`：仍为 `scope=pool`，增加 **JSON 内** `schema_version=2` 与真实全局字段；无 SQL 列改变。
- `operation_receipts`：新增 action `live.mailbox.import` 与 `pool.live_config.replace`；固定回执字段和独立 MAC 域，重放验证代次/栅栏/外部引用。
- `audit_events`：沿用 `secret.put/mailbox.import/config.create` 白名单；只记录固定标识/版本，不记录原文密码或卡号。
- `onboarding_tasks/resource_leases`：本切片导入操作不创建任务或占用，不改变旧任务的 execution_mode。

## 边界与恢复

1. 未核清历史仍不能进入正式开通；没有从文件语法正确推断账号身份/赠金资格。
2. 相同 key、相同内容重试只查询已提交结果；COMMIT_UNKNOWN 不被当作“未提交”。不自动改 key 重发。
3. 写入成功仍不能绕过旧领取器并发协调；后续执行入口必须补齐统一领取证明。
4. 原 source 文件完全不修改。遇到源变动、权限/密钥失效，本地事务回滚；已提交来源变更不自动覆盖已有秘密。
5. v2 配置保存不调用云；撤回需追加明确的新全局配置版本，不能删除历史配置或直接改旧任务。未获授权不对 preview 数据执行回滚/迁移。
6. 根目录检查与快照可识别读取过程的替换；不承诺抵抗同 UID 恶意进程或最终复核后文件变动。

## 验证记录

测试只用自造密码、临时源文件、manifest-scoped PostgreSQL 测试 schema。原始输出保存在 `/workspace/gcloud/.local/live-entry-evidence`。未通过/主动中止的日志保留，不用文件名或代理口述代替实际结果。

### 本次实际结果

- `entry-offline-final.json`：根代理显式 offline runner **339 / 339，通过，0 failure/error/skip**；44.191 秒测试时间。
- `input-mode-pg-third.log`：根代理新增输入/模式 PostgreSQL **17 / 17，通过**；122.923 秒。包含同/异 key 并发、原子回滚、真实提交后丢回包、无隐式覆盖/资格升级及旧任务固定 v1 历史。
- `entry-pg-regression-final.log/json`：根代理 14 个相关模块组合共 **280 项，266 通过、14 error、0 failure/skip**，1306.390 秒。这一次组合本身**不是成功**。14 项均为 multiprocessing spawn 从 `<stdin>` 重新载入主模块失败，随后父进程队列超时；原始日志含明确 `FileNotFoundError .../<stdin>`。
- `entry-spawn-recheck.log`：改用标准 `python -m unittest`，不改生产代码或测试，精确补跑上述 **14 项，全部通过，52.018 秒**。不是删除或跳过并发测试。
- `entry-verification-reconciliation.json`：核对 14 个原错误 test ID 与 14 个补跑通过 ID 完全一致，且 8 个相关生产/runner 文件 SHA-256 未变。因而 280 项均取得同一源代码版本的通过结果；**不能把原组合日志改写成一次性全绿**。
- SPEC 与 code-quality 两条独立审查完成；已修复并发邮箱唯一冲突、回执元数据、秘密 revision、新旧配置混用、总编码大小以及旧 HTTP v2 投影问题。质量代理最终 101 项离线结果仅为辅助证据，不替代上述根代理 PG 结果。
- `input-mode-pg-red.log`、`input-review-red.log`、`input-size-red.log`、`config-http-red.log` 等保留先红证据。第一次 input PG 有 4 个测试 SQL `%` 转义错误，已修测试并重跑；第二次因命令环境无 `python` 导致预定修正未执行而主动中止，不能当作验收。
- `git diff --check` 通过；新增文件无尾部空白，证据文件限制为 0600。

本次没有重跑整个产品全量 `all` 套件，也没有真实首次开通、绑卡、赠金或模型端到端验收。历史全量失败记录未被此相关回归覆盖。**当前不能宣布达到上线标准。**

当前 preview 仍用旧 v1 HTTP/运行入口。不要直接给它保存 v2 全局头来强行启用；旧入口会明确拒绝不支持的配置。下一阶段须一并完成 live 任务创建、同凭据绑定、原领取器排他、两提交启动以及页面/读控接线，而不是只撤掉本次保护检查。
