# 登录固定 30 天，取消闲置超时

日期：2026-09-29。用户明确批准“登录后 30 天有效，取消闲置超时”。工作树 `/workspace/reg-factory/.worktrees/onboarding-minors`，分支 fix/onboarding-minors。

## 页面 → 操作 → 结果

登录页 → 输入本机管理账号登录 → 新会话及两枚 Cookie 最长 2592000 秒（固定 30 天）。
期间不操作 → 不因 30 分钟闲置退出。
继续访问 → 校验权限并记录活动，不把到期时间向后延长。
满 30 天 / 主动退出 / 账号停用 / 会话撤销 / 认证代次失效 → 拒绝访问，需重新登录。
旧会话 → 不延长、不过期复活；重新登录才采用新策略。

## 代码与字段

- onboarding/security.py：新增 SESSION_LIFETIME_SECONDS 常量；新会话单次计算期限，expires_at 和兼容字段 idle_expires_at 均设为相同值；require 不再延长 idle_expires_at，但保留锁、版本 +1、updated_at、auth.session_touch 审计及审计后复核。
- webui/onboarding_auth.py：session 和 CSRF Cookie 的 Max-Age 共用同一常量，由 28800 改为 2592000；预认证 Cookie 的短时策略及登录后清理不变。
- tests/test_onboarding_security.py：固定期限、模拟闲置 29 天、访问不续期、绝对期限后拒绝；旧会话触碰不改变期限的断言。
- tests/test_onboarding_route_guard.py：两枚 Cookie 的 30 天期限及预认证清理，保留 Secure、HttpOnly、SameSite=strict、来源及不回显令牌断言。
- docs/operations/onboarding-b2.md：同步当前策略，保留指向本记录的链接。

不新增表或字段，不执行迁移，不批量改写现有 operator_sessions。只在正常新登录时写入新期限。其他审批、下载链接、任务租约和密钥有效期不变。

## 安全评估

这是用户授权的风险取舍：长期 Cookie 扩大会话失窃或共享电脑遗留登录的风险窗口。未取消任何服务器撤销、账号停用、实时权限、CSRF、Origin、HTTPS、登录限流及审计检查。未引入本地明文令牌存储；日志不记录密码、Cookie 或令牌。

兼容 idle_expires_at 检查继续保留，以尊重旧会话、人工提前到期和所有下游截止检查。新会话 idle_expires_at 与绝对期限完全相同，不再存在独立的短期闲置限制。

## 验证及生效状态

证据目录 `/workspace/gcloud/.local/preview-launch/session-30days/`。
red.log：修改前 35 项中的 3 条目标断言失败，分别为新会话期限、活动延长闲置期限、Cookie 仍为 8 小时。
verified.log：扩大回归 141 项，140 项通过，1 项数据库实际锁等待场景出现 DEPENDENCY_UNAVAILABLE；错误原因为异常被事务边界归一化，未进一步定位具体底层原因，不断言是产品逻辑或机器负载造成。
recheck.log：未修改代码、超时或原测试断言，重跑完整认证与路由套件并包含上述用例，共 65 项全部通过，44.043 秒，退出码 0。包括 30 天固定期限、模拟闲置 29 天、旧期限不延长、过期仍拒绝、撤销/退出/停用/认证代次、Cookie 属性及真实锁等待后的有效性复核。不得将首轮 141 项描述为一次全绿。
git diff --check 通过。未等待真实 30 天，时间边界由隔离数据库构造验证。

用户随后单独批准重启本机 8799 预览服务；已执行并验证新策略生效，详情如下。

## 回退

仅按 before-security.py、before-auth.py 及 before-b2.md 快照差异撤回本次改动并重启经授权的服务。已创建的 30 天会话不会因代码回退自动缩短；如需撤销这些会话必须另行明确授权并审计，不能偷偷批量修改数据库。


## 本机生效验收（用户另行批准后执行）

- UTC 2026-09-29 05:56:00，仅重启监听 127.0.0.1:8799 的预览进程：核对 PID、启动时间、命令、工作目录、TLS 路径、保护模式及合成池模式后发送 SIGTERM；等待旧进程退出且端口释放后启动一次。
- 原命令和环境完整复用，新 PID 2698065，旧 PID 1735491；本机 TLS healthz 与 login 均返回 200。旧进程记录及日志已按 before-session30 时间戳归档，数据库、密钥、TLS 文件均保留。
- 只读对比 57 条旧会话，expires_at 和 idle_expires_at 在重启前后完全不变；无迁移、无旧会话批量延长。
- 实际 HTTP 登录新建独立验收会话，两枚 Cookie 的 Max-Age 均为 2592000，Secure/HttpOnly/SameSite=strict/Path=/ 保留、无 Domain。
- 实际数据库读取：新会话两个期限完全相同，剩余约 2591999 秒。GET 会话后两字段仍不变，验证不滑动续期。
- 使用该验收会话加载真实页面，资源管理及邮箱表格显示；等待 aria-busy=false、查询按钮可用后截图，无页面 JS 异常。没有触发邮箱、信用卡或任务写入。
- 验收会话通过正常 logout 接口撤销，数据库 revoked_at 已设置，随后 session 请求返回 401。两次浏览器验收的独立会话都已退出，不影响用户自己的旧会话。
- 最后确认新进程仍存活、PID 启动时间一致并继续监听 8799；git diff --check 通过。
- 公网域名无凭据 GET 探测发生 ConnectError，原因未定位，未确认公网代理链。上述验收通过本机 TLS 及浏览器本机域名解析完成，不把它说成公网访问验收。

证据：restart.json、live-verification.json、live-verify.log、after-restart.png。需用户重新登录一次才会获得新 30 天会话。
