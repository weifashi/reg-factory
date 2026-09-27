# B2：本地认证与秘密存储（隔离合成测试）

## 适用范围

- 分支 `feature/gcloud-p1a`；只复用 B0 私有 Unix socket / PostgreSQL 测试库与 001 schema，不改已应用迁移。
- 本轮实现的是本机控制面操作者认证，不是 Google 登录、银行验证或邮箱取码。
- 新服务只使用 `fixture:` 合成数据；禁止录入真实 Google 密码、PAN、CVV、OTP、服务账号 JSON。不调用 Google/Billing/Vertex/Sub2API，不尝试获取赠金，不变更调度。
- 没有接入旧 webui、HTTP、SSE、Cookie、代理或后台任务。旧匿名入口尚未受 B2 保护；B3 必须独立验收，不能公开部署当前底座。
- B1 用户验收曾因迁移 statement_timeout 失败。B2 六次受控探针未复现，不等于已修复，保留旧验收结论。

## 已实现的入口与行为

1. 本机交互创建：`python -m onboarding.bootstrap operator-create --settings <私有配置路径> --permission <明确权限>`。stdin/stderr 必须 TTY；用户名用 input；密码与确认用 getpass，关闭回显失败即拒绝。无默认管理员、无密码 argv/env、已有用户名不覆盖。不要在聊天/报告录入密码。
2. `security.create_operator/update_operator` 仅可信本机维护原语，不是自助提权 API。权限/禁用变更递增 auth_epoch，使旧会话失效；本轮没有公开重置密码端点。
3. 固定 scrypt N=131072,r=8,p=1，16 字节随机 salt；密码 12–1024 UTF-8 字节。未知/错误/禁用账号走同成本校验和统一认证失败码。
4. 登录前置票据 10 分钟、一次性、摘要入库；必须精确 HTTPS Origin 与 CSRF。登录失败计数与审计先确认提交，再返回失败。用户名、可信连接 IP 各 4096 桶，每桶 15 分钟窗口最多 5 次失败；hash 碰撞和共享 IP 可能保守限流。前置票据最多 4096 行，复用已用/到期行。
5. 会话随机令牌只存 SHA256，8 小时绝对上限、30 分钟闲置；登录旋转自己的旧会话；退出撤销；每次危险操作重查当前权限、禁用、会话与认证代次，不信 Actor.permissions 快照。
6. `SecretStore(Keyring(...))` 只接收合成 bytes，owner 策略精确绑定操作者；AES-256-GCM 随机 12 字节 nonce，AAD 为 schema/id/kind/revision。密文/AAD/密钥异常失败关闭，不退回明文。
7. 密钥只能从既定隔离目录 `BASE/keyrings/fixture-<32hex>/<version>.key` 读取；目录 0700、文件 0600、当前 OS 用户、常规单硬链接文件、32 字节，路径逐层拒绝符号链接。没有环境变量密钥 fallback。
8. `rotate` 为逐对象 CAS 重加密原语，保持业务 revision，只推进行 version；新 key 通过显式 Keyring.active_version 配置，旧 key 仍可读取且不会自动删除。没有全库扫描、全局切换协调、生产备份恢复工具。丢失 key 不能凭空恢复。
9. SecretStore.use 拥有完整事务，解密/审计/最终有效性检查后，确认 COMMIT 才调用固定无 I/O 的 FixtureConsumer。提交失败或结果未知均零调用；内部 read_for_download 不是可公开的解密接口。
10. 下载专用 approve 绑定任务代次/配置、秘密 id/revision、会话，期限 60–600 秒；issue 原子消费批准、写本地签发回执和最长 60 秒 grant。token 哈希包含 session_id；同账号另一会话也不能用。
11. consume 串行锁定并复核后，原子消费 grant + 审计，确认提交后才返回内容；两个独立进程只能一个成功。回包丢失不补发、不恢复原票据。暂停任务禁止新下载，但保护性撤销仍允许。
12. 所有秘密/下载授权最终检查位于阻塞锁和审计之后；期限使用数据库 clock_timestamp。不能因排队时间偷偷延长授权。

## 事务与 B3 集成约束

```text
AUTH transaction      BUSINESS transaction
------------------    --------------------------------------------
require + CSRF        task -> operator/session -> approval
COMMIT + release           -> secret -> grant -> audit -> final check
             Actor ->      -> confirmed COMMIT -> content/consumer
```

- B3 不可在同一个事务先 require/check_csrf 持有 session 锁，再锁 task；会与 task-first 服务形成反序。认证/CSRF 完成并释放锁后进入业务事务，业务层再次 revalidate。
- conn 参数服务要求调用方持有 UoW；异常必须传播到事务边界，不捕获异常后强行提交半成品。
- consume/use/login 自己拥有完整 UoW；任何 COMMIT_UNKNOWN 禁止自动重试内容交付。
- Actor 是内部已认证快照，不接受 HTTP JSON 反序列化后直接信任。FixtureActor 只保留 B1 合成支路，不能进入保险库/下载。
- B3 需接入 Host/Origin 来源、可信代理 IP、Cookie HttpOnly/Secure/SameSite、路由默认拒绝、API/SSE 脱敏与旧执行阻断；当前函数单测不证明浏览器防线已上线。
- 持久会话/限流重启可读取；DB 不可用不建立临时内存管理员。

## 数据影响

0 新表 / 0 新字段 / 0 DDL；写入 operators、operator_sessions、auth_throttles、secret_objects、approvals、download_grants、operation_receipts、audit_events 八张既有表。
下载仅查询/锁定任务、批次、全局配置，不推进任务状态。下载回执 SUCCEEDED 仅指本地票据签发，不是 Google/支付成功。

## 隔离验证与回滚

- 使用 `.venv-onboarding/bin/python -m unittest discover -s tests -p 'test_onboarding_*.py' -v`；真实 PostgreSQL fixture、临时 schema、零 skip，不导入旧 webui/真实环境。
- 并发用 spawn 独立进程/连接；锁等待与 TTL 以数据库时钟观测。密钥测试文件只在独立 fixture 子目录创建并清理。
- 停止调用 B2 即停止本阶段，没有新增常驻服务；未提交、未部署。
- 代码回退先备份 B2 清单，恢复 audit/repository 的 SHA 校验旧内容，仅移除 B2 新文件；保留 P1a/B0/B1 与用户改动，禁止 reset --hard。
- 无 DDL 可回退。测试 schema 只经 B0 manifest/marker 清理；不能 DROP 整库或清空永久数据。不要删除旧 key、审计、回执或已消费 grant 来恢复操作。
- Linux 权限测试不是 Windows ACL 证明；OS 同用户/DB owner 不在应用权限防线内；Python 不保证内存零化。

## 2026-09-24 依赖修复补记（本机隔离实例）

- 共享 pgdist 还被另一实例使用，因此没有向共享目录补库，也没有系统级 apt install。仅从 Ubuntu 包 `libllvm17t64=1:17.0.6-9ubuntu1` 解出匹配的 LLVM17 常规库文件；包 SHA256 为 `1863b5459ca4a6691f5e039e76157f074ee08b48176267642ee4dfea065f5db4`。
- 本机安装位置固定为 `/workspace/reg-factory/.local/onboarding-p1b/runtime-lib/libLLVM-17.so.1`；目录0700、库0600、本UID、无符号/硬链接。其余依赖由现有系统库满足，不用LLVM20软链接冒充17。
- 管理工具 `_run` 仅在上述固定专用目录存在且通过原有路径/权限检查时，将它置于原共享库路径之前；目录不存在保留旧行为，坏权限/owner/普通文件/符号链接（含悬空链接）拒绝启动子进程。继续覆盖不可信 ambient LD_LIBRARY_PATH，清除PG连接覆盖环境变量。
- 只经原工具 stop/prepare 重启授权实例，使新进程获得专用加载路径。未更改JIT、数据库配置、SQL、迁移校验和或超时；没有重启另一实例。
- 修复验证用单独事务 `SET LOCAL jit_above_cost=0` 强制触发一个小查询的JIT，退出事务即恢复默认阈值。这是证明JIT可执行，不是关闭JIT或改变全局阈值。
- 先按旧记录精确列出7个失败schema，逐个调用原cleanup_schema：实例/manifest/owner/marker/锁/依赖闭包都通过后才DROP；再次确认schema不存在且manifest未变后，删除该manifest。禁止通配DROP或force。
- 回滚：先用当前工具停止目标实例，备份并移走本次唯一库文件/空目录，再恢复工具及测试的本轮基线；旧缺库问题会重新出现，因此不得将回滚视为验收通过。不要删除测试库或凭据，不操作共享pgdist/另一实例。
- 首次新增负测试失败时mock诊断意外包含继承环境；原日志已限制0600、整个本轮证据目录0700且不公开，后续改为只断言调用布尔值。公开证据只采用脱敏摘要或成功日志；不发布原始敏感RED文件。
