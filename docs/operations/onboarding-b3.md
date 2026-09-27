# B3：受保护控制面与页面接入

## 适用边界与上线状态

这是 P1b **隔离合成底座**，不是完整 Google Cloud 自动开通产品，不应作为生产系统对外发布。只沿用 B0 专用测试数据库和 B2 认证，不接真实 Google、银行、赠金、Vertex 或 Sub2API；没有创建真实资源、执行收费测试或打开调度。

2026-09-24 用户已将整体目标扩展为“完成全部、达到上线标准再交付”。这个目标不改变上述当前事实；B3 验收只是后续 P1c–P5 的前置条件，不把本文件当完整产品上线批准。

## 启动配置与运行模式

- `ONBOARDING_MODE` **仅在启动前读取进程环境**，默认 `off`，只接受精确 `off` / `protected`；其它值拒绝启动。不能通过普通 `.env` 编辑接口切换。
- `off` 保留旧页面和原执行体系，不加载新数据库设置、不注册新 API、不建立新的保护层。**off 不等于公网安全。**新增安全前缀配置不能通过旧 Web 编辑器写入。
- `protected` 还要求启动环境 `ONBOARDING_ORIGIN` 为固定 HTTPS origin，以及 `ONBOARDING_SETTINGS_FILE` 指向既有测试范围内的应用角色私有配置文件。路径/目标由 B0 Settings 再验证；不接受远端 DSN、任意 schema 或迁移角色。
- `.env.example` 只添加上述变量的注释说明，没有真实密码、密钥或可直接运行的生产配置。
- 配置非法返回 503，不退回匿名。数据库断开时需要数据库的请求返回固定错误；登录静态页可能仍可读，不能据此认定系统就绪。
- 保护模式只提供本地新底座生命周期：不运行旧 K12/Plus 自动启动、旧浏览器清理、导入期代理初始化或导入期 Git 子进程。
- 真实反向代理/TLS/服务进程配置尚未部署验收；不能直接相信 `X-Forwarded-*`。测试使用真实回环 HTTPS、临时证书、`proxy_headers=False`，不是生产证书或代理信任链的证明。

## 页面 → 操作 → 结果

```text
/login
  | bootstrap + credentials (POST; no browser storage)
  v
opaque HttpOnly session cookie
  | GET session -> permissions + CSRF in page memory
  v
/ or /onboarding
  +--> diagnostics ---------> DB/schema ready; never cloud-ready
  +--> task UUID -----------> own synthetic snapshot only
  +--> pause/cancel/recheck -> CAS + durable receipt + audit -> 202
  +--> protected env -------> redact / keep / replace / clear
  +--> logout -------------> DB revocation + delete cookies

unknown path/method --------> 403
expired session -----------> 401 (known HTML home navigation -> login)
missing permission --------> 403
dependency failure --------> 503; no anonymous fallback
legacy execution ----------> denied; legacy:admin still cannot execute
```

- 首页使用独立 `onboarding.html/js/css`，不是重写原 `app.js`。保护模式不加载旧脚本，避免旧轮询、运行与取码按钮触达被禁路由；off 模式原界面不变。
- 登录用户是本机操作者，不是 Google 账号。没有 Web 注册、重置密码或授予权限入口；管理员仍经 B2 本机 getpass 引导。
- 暂停只停止后续领取；取消不保证撤销在途或已完成动作；重新核验仅写 `fixture.recheck / NOT_SENT`，没有实际核验执行器，也不自动恢复或重发。
- 任务查询需 `onboarding:read`，变更需 `tasks:manage`，且都再次核验创建人、合成配置与实时会话。没有公共建任务、费用批准、真实调用、密钥下载或启用 API。
- 202 只表示本地事务已提交。版本/状态/请求键冲突返回 409；提交未知或网络中断不自动重试；页面保留当前请求键并要求先查询。

## 认证与输入边界

1. 全 ASGI method+path 显式策略：基线 71 组旧方法路径中，68 组禁止旧业务，首页与 GET/POST env 3 组独立保护。新增未知路由不自动继承旧权限；WebSocket 全部关闭。
2. 匿名仅固定登录页、登录 JS/CSS、bootstrap/login 和 `/healthz`。`healthz` 仅 `{alive:true}`，不是数据库/业务 readiness。
3. Cookie `__Host-rf_session`、`__Host-rf_csrf`、`__Host-rf_preauth` 都是 Secure / HttpOnly / SameSite=Strict / Path=/ / 无 Domain。csrf cookie 供服务端会话接口核验并返回当前页面内存，数据库仍只存摘要；不放 localStorage/sessionStorage/URL。
4. 同源安全 GET 可以缺 Origin，但必须精确 HTTPS Host，拒绝跨站 Fetch Metadata、错误 Origin/Referer。危险请求必须明确匹配 Origin、JSON content-type 和 CSRF；重复安全头、重复 cookie、编码路径等保守拒绝。
5. JSON 限 8192 bytes，拒绝重复键、非对象、NaN/Infinity、非法字段与格式。异常只返回 `{code,correlation_id}`，不返回请求原文、DSN 或第三方异常；唯一例外是 5 条写路由（批次创建、pool 任务命令、邮箱修改、配置保存、邮箱导入）在可证明原请求未提交时，409 VERSION_CONFLICT 额外带 `not_committed: true`，含义与前提见 `onboarding-not-committed-signal.md`。
6. B2 账户/可信 IP 的 5 次失败 / 15 分钟规则保留统一 401；B3 额外 HTTP 来源桶：bootstrap 30 次/分钟、login 10 次/分钟，成功和失败均计；各最多 4096 桶，共最多 8192，数据库持久，超额 429。共享 IP/哈希碰撞可能保守限流。IP 来自实际连接，不相信伪造转发头。
7. 认证事务先提交释放锁，再进入 task-first 业务事务；业务操作再次验证，不信前端按钮隐藏或过期 Actor 快照。
8. 全保护响应 no-store、nosniff、no-referrer、严格同源 CSP、禁止嵌入和外部脚本。FastAPI 默认文档的 CDN/内联脚本也被禁止，`/docs` / `/redoc` 的交互视图当前不可用；受权限保护的 `/openapi.json` 可读。不能为了文档放开 CSP。

## 旧配置与兼容性变化

- 敏感值 `value/default` 固定为空，另返 `configured`；不只看旧 secret 标记，还保守覆盖密码/token/header 类名称、URL 用户信息及 query/fragment。
- 敏感更新必须 `{action:"keep"}`、`{action:"replace",value:"..."}` 或 `{action:"clear"}`。空值不暗中清除，替换值不能空白；整批拒绝 CR/LF/NUL 行注入。普通非敏感项仍为字符串。
- `ONBOARDING_` / `RF_ONBOARDING_` 以及明确引用的自定义 DSN/秘密变量名不能通过此接口修改。常规/更新子进程环境最终均剥离这些名字；保护模式依然完全禁止旧执行，**剥离环境不等于 OS 隔离**。
- 保护模式保存旧 `.env` 后不 reload 旧 provider；回复 `effective_now:false`，没有借保存动作启动旧服务。
- 旧 `.env` 仍是旧明文文件体系，不属于 B2 新保险库；它没有与数据库原子联动的专用变更回执。不要向此兼容编辑器存放新 Google、银行卡或服务账号秘密，不能声称所有旧配置变更拥有原子审计。生产迁移与秘密隔离需后续专项处理。
- off 保持原正常输入语义和旧业务断言；新增控制面配置拒写以及配置行注入拒绝是刻意安全收紧，不把旧非法输入兼容当目标。

## 数据影响

**0 新表 / 0 新字段 / 0 DDL。**001 SQL 与 checksum 不变。

|既有表|B3 经既有服务读写的字段及用途|
|---|---|
|operators|读 id / username_norm / password_hash / permissions / disabled / auth_epoch；没有 HTTP 创建或提权|
|operator_sessions|创建/旋转 token_hash、csrf_hash、auth_epoch、expires_at、idle_expires_at；请求续闲置、退出 revoked_at、version / updated_at|
|auth_throttles|复用 bucket_key、window_start、failures、blocked_until、version / updated_at；登录票、B2失败限流与新增HTTP来源桶|
|onboarding_tasks|查询白名单 id/status/version/reason_code/current_step/generation/cancel_requested；暂停/取消/核验推进本地状态与 version / updated_at|
|onboarding_batches / global_configs|只读创建人/合成资源/配置绑定，拒绝跨用户或真实资源|
|task_steps|recheck 插入合成只读 NOT_SENT 标记，同任务/step/generation去重|
|operation_receipts|复用 command:pause/cancel/recheck 回执、request_hash / idempotency_key / phase / result_code / generation；重复请求不再次推进|
|audit_events|复用认证、限流与任务操作审计，无密码/token/OTP原文；旧env只有访问认证审计，不等于专用变更审计|
|schema_migrations|只读 version/checksum 校验；Web 启动和诊断不运行迁移|
|resource_leases|本批HTTP不改租约表；取消服务检查回执/步骤，不因前端取消自动释放未知逻辑占用|
|secret_objects / approvals / download_grants|本批没有公开读写路由，既有 B2 测试继续回归|

所有验收操作者、会话、任务、限流和审计写入临时 manifest schema；永久测试 schema 不插入样例用户。

## 回滚和剩余生产门禁

- 本批没有提交、推送、生产服务发布或永久迁移；回滚基于 `rf-b3/baseline.json` 和完整相对路径备份，保留 P1a/B0/B1/B2 与用户改动，禁止 `reset --hard`。
- 若未来启用保护服务，不能把切回 off 当安全回滚；先停新入口，保留保护层、回执、审计与私有密钥，再回退业务。未知外部动作必须核对，不删回执来恢复重试。
- 本轮 Linux + 合成 PostgreSQL + 本地 TLS/Chromium 验证不能证明 Windows ACL、真实反代、生产备份恢复、多节点执行隔离或第三方流程已经可用。
- P1c 真实池状态唯一源/兼容切换、P2 官方身份/支付与验证码、P3 最小权限云执行、P4 Sub2API 版本合同/隔离导入/独立费用与启用批准、P5 恢复演练与授权单账号端到端都仍是整体上线门禁。
