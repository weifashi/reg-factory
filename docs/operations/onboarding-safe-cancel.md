# 任务安全取消与邮箱释放

2026-09-29，`/workspace/reg-factory/.worktrees/onboarding-minors`，分支 `fix/onboarding-minors`。
用户已批准安全取消设计。本次不补 Google/银行真实执行器，不批量取消本机已有任务。

## 页面 → 操作 → 结果

```text
Google 开通任务 / 当前任务
  取消 → 确认 → 后端锁定当前任务并核验
                 ├─ 完整未执行证据 → 已安全取消 → 释放本任务邮箱
                 │                              → 重新预检后可以分配
                 └─ 已执行/证据不足 → 记录取消 → 保留占用 + 具体原因
  再次提交同一请求键 → 返回原回执 → 不重复释放、不碰新任务占用
  预检失败 → 显示检查结束/未创建 → 不再停在“正在检查”
```

释放不等于外部业务回滚；没有释放信用卡绑定。已取消任务仍保留在历史列表。前端读到安全终态后禁用暂停和取消，日志区显示“已安全取消，邮箱已释放”。如果邮箱随后被新任务占用，旧释放事件只是历史事实，不保证现在仍可分配。

## 什么情况下才释放

仅针对当前 synthetic 策略下由正式 `pool.batch.create` 创建的 Google 单平台任务。必须是第一代、尚无当前步骤的 QUEUED/PAUSED 任务；不能仅凭这些字段或租约到期判断安全。

1. 原创建回执、批次/配置/任务/邮箱对应关系完整一致。
2. 创建审计和租约申领审计对应同一回执；创建版本为 2。
3. 后续每一版本只来自成功暂停/取消；回执与审计连续匹配。JSON 证明精确比较类型，不把 true、1、1.0 混为同一值。
4. 无任何步骤（包括 NOT_SENT 核验标记）、未知/外部回执、其他执行审计。
5. 无本任务或 Google 状态关联的卡预约/卡绑定，无费用授权、其他资源占用、同邮箱其他活动任务。
6. 锁定的邮箱租约仍由原批次持有；任务/owner/hold/fence/version 与原申领证据一致，不能有停止证据漂移。
7. Google 状态仍为本任务 RESERVED，平台和邮箱凭据 pin 未改变。

未满足时只记录取消请求，不释放。旧业务原因码不被覆盖；最新取消阻塞原因通过固定枚举审计事件显示。先前 CANCEL_* 原因会更新为本次原因。

不修改邮箱的历史注册标记、健康、身份资格或凭据。平台 state 创建版本未存入原回执，当前仅证明状态归属和 pins 并对锁定版本做 CAS，不声称证明平台历史从未变化。未来真实 worker 必须遵循既有锁和 fence 校验；本规则不是跨系统停止证明。

## 表与字段（无新增表/字段，无迁移）

| 表 | 安全取消时变化 |
|---|---|
| onboarding_tasks | status=CANCELLED_SAFE，cancel_requested=true，reason_code=CANCEL_SAFE_RELEASED，version/updated_at 更新 |
| mailbox_platform_states | 仅本任务 Google 状态 RESERVED→UNUSED，last_task_id=NULL，version/updated_at 更新 |
| resource_leases | 仅本任务原邮箱租约清空 task_id/owner_id/lease_until/hold_reason；stopped_evidence_ref 保持 NULL；fence 和 version 单调递增 |
| operation_receipts | 原幂等结构不变，安全取消 result_code=CANCELLED_SAFE、summary status=CANCELLED_SAFE；旧 COMMAND_ACCEPTED 回执保持可回放 |
| audit_events | 新增本次 lease.release 与 task.cancel 事件；阻塞原因使用固定枚举，无原始秘密 |
| payment_cards / card_reservations / card_account_links | 不修改；存在相关记录时禁止自动释放 |

上述写入与任务结束、审计、回执在一个事务中完成。任何故障全部回滚；保留原鉴权、版本冲突、未决请求核对和不自动重试规则。

## 文件与规则影响

- `onboarding/pool_cancellation.py`：新增内部未执行证明与邮箱 CAS 释放，不连接外部系统。
- `onboarding/pool_commands.py`：取消接入新判定；成功回执支持安全终态；暂停/核验的原业务语义不变。
- `onboarding/task_events.py`：允许安全取消与固定阻塞原因投影；仍不暴露审计原始摘要。
- `webui/static/onboarding-pools.js`：新日志文案、原因翻译、终态控件禁用、取消确认、预检结束提示。
- `webui/static/onboarding-pools.html`：修正“取消永不释放”的旧说明。
- `tests/test_onboarding_safe_cancel.py`：隔离真实 PostgreSQL 的释放/回放/并发/权限/证明损坏/卡关联/故障回滚/HTTP 投影测试。
- `tests/test_onboarding_pool_commands.py`：取消仍未释放时的审计预期改为具体原因，保留原状态矩阵和拒绝断言。
- `tests/test_onboarding_run_ui.py`：预检失败加载态结束、安全取消日志和终态控件、保留业务错误时仍显示当前取消阻塞原因。

## 验证与上线边界

原始证据目录：`/workspace/gcloud/.local/preview-launch/safe-cancel/`。
前后端新增测试均先在旧代码得到预期失败，再实施。首次扩展测试中出现测试夹具把 CardImportResult 当字典的问题及两项 setup 迁移依赖失败，保留 focused3.log；修正夹具属性访问，未放宽业务验证和超时。

只读评审已覆盖锁序与损坏证明；发现并修复 JSON bool/int/float 混淆和旧取消原因停留的问题。截图使用浏览器拦截测试数据，不冒充本机任务执行。

本机 8799 后端需用户授权重启后才能加载此 Python 代码；代码完成不等于运行服务已更新。现有六条任务未经用户操作不会自动取消。

## 回退

不做数据库回滚或重新占用邮箱。若需停用自动释放，仅关闭取消中的新释放分支，但保留新回执/审计 outcome 的读取兼容，否则旧请求回放和日志会失败。不得整体覆盖本工作树其他未提交改动。未提交、推送或执行生产操作。

### 最终验收证据

- 最终代码后运行 193 项：安全取消专属 18 项、历史命令/HTTP/权限边界/审计事件/运行页/统一页面回归全部通过，321.492 秒，退出码 0（final.log）。
- node --check 和 git diff --check 通过。
- safe.png / blocked.png / preflight.png：实际 Chromium 渲染的测试场景，已检查成功与预检失败截图；无 JS 异常，1440px 无横向溢出。
- preview-readonly.json：本机已有 6 条任务仍 QUEUED/RESERVED，PID 2698065 未重启。未自动取消用户任务。
- 后端需重启才能生效；已通过异步问题请求本机 8799 重启授权，截至此次验收尚未收到答复。公网代理链未另行验证。

### 已授权本机重启（2026-09-29 07:36 UTC）

用户随后明确批准重启本机 8799，使上述“尚未重启”状态结束。

- 精确核对旧 PID 2698065/start ticks/工作目录/HTTPS命令/保护模式后，仅停止该进程；原命令与环境启动新 PID 2858915，仍监听 127.0.0.1:8799。
- 启动前核对 193 项最终测试与源文件时间，并记录五个修改源文件 SHA256；没有改应用代码、迁移或配置。进程元数据与旧日志已备份为 before-safe-cancel 时间戳文件。
- `/healthz`、`/login` 均 200；62 个已有会话到期时间保持不变。
- 登录独立验证会话，实际任务详情/事件接口均 200；浏览器历史任务读取、取消按钮可用、新说明与 JS hash 验证通过，无页面错误。
- 重启前后及浏览器检查后，任务/平台状态/邮箱租约摘要一致；原 6 条任务仍 QUEUED/RESERVED。没有执行取消、预检提交、绑卡或模型调用；验证会话已正常登出。
- 公网直连探测返回 ConnectError，因此只确认本机实际 TLS 服务及浏览器通路，不声称公网代理验证通过。
- 证据：`restart.json`、`restart.log`、`live-after-restart.json`、`live-after-restart.log`、`live-after-restart.png`，均位于上述证据目录。
