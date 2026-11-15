# 真实接入调查与上线阻塞（2026-09-29）

本记录基于 `fix/onboarding-minors` 当前实现和 Sub2API 官方 `v0.2.9` 源码。它不是上线通过记录，不含密码、token、JSON 私钥或真实邮箱资料。

## 当前事实

- 应用仍在 `/workspace/reg-factory` 的原工作树内建设，不另起独立产品。
- 原池的数据库、金库、配置和任务执行都存在显式 synthetic 合同，不能只删除页面提示或 fixture 检查就开放真实流程。
- 本机临时 Sub2API 已部署，但它本身可登录不等于 reg-factory 已接入。初始调查时管理员合规确认尚未取得；2026-09-29 用户明确确认后已通过官方接口提交，未通过数据库绕过。
- 本轮新增的管理连接核验只准备真实下游联调，不授予费用批准、上游调度或 Google 操作权限。

## 必须保留的既有业务规则

| 要求 | 当前事实与接入约束 |
|---|---|
| 同一邮箱不并发 | 新 DB mailbox lease 不约束旧文件消费者；旧 `common/emails.py` 的锁按 platform 区分。不能同时对同一数据源启用两套领取机制 |
| 跨平台独立状态 | 复用邮箱 registry 与 platform state 的分层；不能把邮箱密码自动当成所有平台密码 |
| 导入不自动取得执行资格 | 未核清历史仍是 HISTORY_UNRECONCILED，不能迁移时改成 UNUSED |
| 失败才释放占用 | 网络超时、进程失联和 TTL 到期不是明确失败；原占用及 fence 必须保留 |
| 全局配置固定 | 任务应固定配置版本、凭据版本和目标身份，不能执行途中切换地址或密钥 |
| 禁调度导入 | 必须在外部账号首次可见前禁用，不允许先启用再关闭的竞态 |
| 收费单独批准 | 无批准不调用 Gemini；预算提醒不是硬费用上限 |
| 缺权限暂停 | 不自动提升组织权限、弱化密钥策略或重试不明操作 |

## Sub2API 0.2.9 的真实合同冲突

已独立核对官方 tag：

1. `backend/internal/handler/admin/account_handler.go:121-138` 的创建请求没有 `status` / `schedulable`。
2. `backend/internal/service/admin_account.go:424-435` 创建时固定 `StatusActive` 和 `Schedulable=true`。
3. 同文件 `495-508` 省略或传空分组会尝试绑定平台默认分组，不等于隔离。
4. 同文件 `535-543` 先创建再绑组；绑组失败可能已留下账号。未知结果不得自动重试 POST，即便使用同一 Idempotency-Key。
5. `backend/internal/handler/admin/account_data.go:682-698` 的备份导入类型列表不接受 service_account；且该导出接口会返回原文凭据，不用于探测。
6. 复制已有账号存在原子 `schedulable=false` 路径，但不是任意新 SA 导入。未经模板和凭据替换安全验证，不把复制当现成解决方案。

因此不能向原 API 虚构 `schedulable:false` 字段然后宣称安全，也不能自动导入真实 SA 后再关闭。后续须实现并验收原子禁用的兼容路径，或使用明确支持该合同的版本；当前关闭真实写入。

来源：
- [创建服务](https://github.com/Wei-Shaw/sub2api/blob/v0.2.9/backend/internal/service/admin_account.go)
- [请求 DTO](https://github.com/Wei-Shaw/sub2api/blob/v0.2.9/backend/internal/handler/admin/account_handler.go)
- [备份导入导出](https://github.com/Wei-Shaw/sub2api/blob/v0.2.9/backend/internal/handler/admin/account_data.go)
- [幂等协调器](https://github.com/Wei-Shaw/sub2api/blob/v0.2.9/backend/internal/service/idempotency.go)

## 真实存储的关键缺口

- `onboarding/settings.py`：目标固定测试库、专用 socket 和 schema。
- `onboarding/pool_vault.py`：仅 SyntheticPoolPolicy；PoolVault 没有真实秘密消费接口。
- `onboarding/pool_secret_types.py`：只接受 fixture 邮箱、秘密和固定合成卡。
- `onboarding/pool_config.py`：内置 fixture 模型、地区、目标与分组，不是实例注册表。
- `onboarding/pool_repository.py`：历史管理视图不是 worker 执行许可。
- `onboarding/secret_store.py` / `downloads.py`：fixture 消费和下载机制不能直接用于真实 SA。
- `webui/onboarding_auth.py`：只组装 off/synthetic，不存在已部署的 live 组合。

新 live 组合必须明确专用存储身份与密钥来源，复用现有业务规则但不全面放宽旧 validator。执行前校验 task、owner、current/pinned secret revision、lease/fence、撤销/过期和 purpose；持久化 INTENT 后才发出外部动作。数据库提交结果未知时不得 dispatch。

旧消费者切换必须覆盖 `common/emails.py`、显式邮箱入口、`register_three_platforms.py` 同邮箱并行、`run_full_flow.py` 文件快照、K12、mailbox broker 和恢复 RT 回写。HTTP 保护不会停止已运行的 CLI。迁移必须保留历史尝试、销售限制、凭据版本和未核对占用；不能重新清零。

### 2026-09-29 复核：沿用原邮箱池密码，不另建密码池

原项目确有可复用来源：`webui/server.py` 的 `EMAILS_FILE` / `POST /api/mailpool` 使用受信 `REG_FACTORY_DATA_DIR`（未配置则项目根目录）的 `emails.txt`，通过 `common.account_records` 解析并写入。`common/asset_store.py` 的 `_mailboxes` / `_mailbox_map` 读取同一格式的邮箱密码。不能因此把任意文件记录自动归给当前操作人；旧文件没有 owner 字段，也不能使用会推进领取游标的 `get_email` 充当只读凭据来源。

当前受保护邮箱导入仍只接受 fixture 类型，既有 Google token 来源不读取邮箱池密码，浏览器 driver 也尚未绑定受控密码消费者。这三点是**尚未完成的程序工作**，不是“用户再提供密码就能全自动运行”。

只读检查正在运行的本机 preview 进程，其实际 data-root 下 `emails.txt` 尚不存在；未读取其他账号或输出任何密码。此结果只描述该进程的当前路径，不代表用户其他机器或其他目录没有已有邮箱资料，也不能凭空推断密码错误。

下一阶段复用顺序：

```text
原邮箱池的明确授权记录
  -> 精确邮箱 / 来源版本 / 操作人归属核验
  -> 同一邮箱凭据的加密版本与引用（不另建 Google 密码配置）
  -> live 来源与 fixture 来源可区分，旧数据不改标签
  -> 固定全局配置版本 + 可信新建 live 任务（不是 UPDATE 旧任务模式）
  -> 同邮箱 gcloud 身份或受控浏览器登录消费者
  -> 原页面运行入口 + 后台 worker + 真实验证
```

真实全局配置还须接入 billing、Google 授权 profile、已审核 Sub2API target/group、SA 命名及角色；HTTP 不得任意指定 SDK 路径、秘密文件或目标 URL。当前执行器固定单任务 / 180 秒，不能仅保存更大的并发或超时参数便声称已生效。首次开通付款、赠金核验、费用批准及真实 DOM 验收仍另有缺口。

## Google 官方接入限制

- `billingAccounts.create` 官方文档说明创建的是 billing subaccount，不能拿它冒充普通用户首次绑卡/申请试用 API。
- `ProjectBillingInfo` 的 billingEnabled 不能证明赠金已到账、适用于某模型或余额足够。
- SA 私钥只在创建响应中提供；超时不能盲目再次创建。需要核对远端 key 状态和本地加密落盘，不把“列到了 key”当作本地已持有私钥。
- 只使用真实授权和真实账单资料，不编造地址、不绕过本人验证、不重复申领不符合资格的赠金。

来源：[Billing 创建](https://docs.cloud.google.com/billing/docs/reference/rest/v1/billingAccounts/create)、[ProjectBillingInfo](https://docs.cloud.google.com/billing/docs/reference/rest/v1/ProjectBillingInfo)、[SA keys](https://docs.cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts.keys)。

## 已准备的本机工具

Google Cloud SDK 586.0.0 安装到 `/workspace/reg-factory/.local/toolchains/gcloud-586.0.0/google-cloud-sdk`，不改系统 PATH、不运行注册或资源创建。

校验使用官方安装页对应的 rolling Linux x86_64 包，SHA-256 为 `c1cd1823624a33f2341d0d384aafe2d7b24c1b77c9b131087d772ee0791ff2be`，包内 VERSION 已核验 586.0.0。最先下载的 versioned URL 包与该表校验不一致，未执行；没有跳过校验。

独立 `CLOUDSDK_CONFIG=/workspace/reg-factory/.local/google-auth-test`，关闭匿名使用报告和自动版本检查；`gcloud version --format=json` 成功，`gcloud auth list --format=json` 返回 `[]`。这表示工具可用但尚无 Google 授权，不代表云资源已接通。

## 待用户输入与无需再问的事项

已取得：用户官方 Sub2API 承诺确认、临时实例修改授权、Google 测试账号官方登录授权。仍需：可用结算及赠金状态证据；模型测试时单独批准费用。

无需反复确认：继续在原项目开发、沿用当前界面、同机优先、一次一个授权账号联调、不动正式 Sub2API、不隐式启用调度、不提交推送及生产迁移。这些沿用既有决定。

## 后续进展（非整体上线）

原官方0.2.9合同冲突已在本机临时实例通过create_disabled兼容补丁解决：首次inactive/false、单/批量API及DB读回验收通过，测试记录已逻辑删除；正式实例未改。详见onboarding-sub2api-disabled-import.md。Google授权已核验：可见7个项目全部未绑定billing，当前可见结算账号0；不能据此推断从未试用或仍有试用资格。真实worker及其他存储/协调缺口仍存在。
