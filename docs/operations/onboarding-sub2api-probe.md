# Sub2API 实际管理连接核验

本机命令位于 reg-factory 原项目，使用真实 HTTP 请求；不是假响应或 Google 注册执行器。它不修改页面、任务状态、业务表或既有池配置。

## 执行

从 `/workspace/reg-factory/.worktrees/onboarding-minors` 运行：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-onboarding/bin/python \
  tools/onboarding_sub2api_probe.py \
  --config /workspace/reg-factory/.local/sub2api-temp-20260929/admin-access.json \
  --group regfactory-gemini \
  --allow-loopback-http
```

配置文件仅含 `url`、`email`、`password` 三字段。本机临时实例已生成该文件；不要把内容复制进聊天、工单、日志或版本库。文件须本人持有、0600、单硬链接，直接父目录须本人持有且0700，路径不得经过软链接。

正式 HTTPS 目标不要传 `--allow-loopback-http`。默认验证 TLS，忽略环境代理和自定义 CA 环境变量，不跟随重定向。HTTP 例外只匹配显式端口的 `127.0.0.1`，不是允许任意私网地址。该工具不是通用反向代理，也没有任意 endpoint 调用接口。

## 行为与状态

```text
读取本机私有配置
       |
       v
GET health --> POST login --> GET compliance
                                  |
                +-----------------+------------------+
                |                                    |
             待确认                                已确认
                |                                    |
      COMPLIANCE_REQUIRED                GET groups/all 精确匹配
                |                                    |
                +-----------------+------------------+
                                  |
                              POST logout
                                  |
                           输出白名单 JSON
```

- 不调用 compliance/accept；不能代替本人接受承诺。
- 分组必须名称精确唯一、平台 gemini、状态 active、正整数 ID。
- 只允许登录/退出造成会话变化，其余为管理读取。服务器仍可能记访问日志和限流计数，不声称“零内部副作用”。
- `READY` / 退出码0只表示上述管理检查成功且登出成功；始终 `execution_connected=false`。
- 未确认合规、找不到分组、坏响应、认证失败等均非零退出；登出失败单独标识，不覆盖先前失败，不报完全就绪。
- 响应最多1MiB，拒绝压缩响应，按 raw chunk 计数。`asyncio.timeout` 每请求10秒，包住连接、响应头、正文，避免持续半截响应头或103通知拖住同步客户端；每次登出也有独立期限。系统 DNS resolver 若在线程中阻塞，协程取消不等于系统调用已中断；当前真实联调目标是127.0.0.1，不声称任意操作系统 DNS 挂死也有精确10秒硬抢占保证。
- 不自动重试，不持久化 token，不回显外部错误正文、URL或密码。

## 已核验的真实实例结果

证据：`/workspace/gcloud/.local/preview-launch/sub2api-probe/live.json`、`live.exit`。

```json
{"auth":"passed","compliance":"required","execution_connected":false,"group":"not_checked","health":"passed","logout":"passed","reason_code":"COMPLIANCE_REQUIRED"}
```

命令实际退出1，符合当前官方合规门禁，不把它改成假成功。没有创建 Gemini 分组、导入 SA、触发模型调用或变更调度。

## 文件、规则、表字段影响

| 文件 | 作用 |
|---|---|
| `onboarding/sub2api_probe.py` | 私有配置读取、固定 HTTP 核验、脱敏投影 |
| `tools/onboarding_sub2api_probe.py` | 明确配置路径的本机 CLI；只有 READY 返回0 |
| `tests/test_onboarding_sub2api_probe.py` | 配置/协议/真实回环HTTP与TLS/超限及脱敏测试 |
| `tools/onboarding_test_runner.py` / `tests/test_onboarding_test_runner.py` | 新探针登记到offline/all，补回此前漏登记的邮箱表格和安全取消测试；保留全模块覆盖断言 |
| `docs/superpowers/plans/2026-09-29-live-sub2api-probe.md` | 本次执行与验收合同 |
| `docs/operations/onboarding-live-integration-research.md` | 真实存储与下游合同缺口、工具准备 |

**没有数据库表/字段/迁移变化，没有修改原任务运行入口或既有取消逻辑。** 原 synthetic 默认、用户改动和8799服务保持不变。操作核验是新本机能力，不把旧 `/api/onboarding/config` 的 fixture 实例替换成真实目标。

## 验证记录与回滚

新增测试覆盖健康/认证/合规/分组/登出、URL/文件权限、软硬链接、错误JSON、大小边界、压缩、slow-drip、重定向、代理环境、自签TLS拒绝和秘密输出。真实 TCP 回归还覆盖半截响应头、连续103、合规/登出阶段各自超时及超时连接关闭。

测试证据保存在同一私有目录：`red.log`、`bounds-red.log`、`header-103-red.log`、`async-deadline-green.log`；主代理最终独立offline套件为 `root-offline.log/json/exit`：131项通过、0失败/错误/跳过、退出0。最终异步代码已重新实际访问本机实例，`live.json` 仍为上述合规待确认结果。先前 `root-final.log` 的28项通过属于旧同步版本，不能当作最后异步版本验收。测试登记修复另有 `runner-red.log`、`runner-green.log` 及退出码。

停止使用 CLI 即可撤回这项能力；移除本次新增模块不需要回滚业务库，也不得删除原池历史或占用。临时 Sub2API 的启停和保留数据说明另见其私有运行目录 README。

## 不属于本次完成范围

真实邮箱迁移、worker、注册/绑卡、Google IAM与Vertex、JSON密钥消费、Sub2API 原子禁用导入、费用批准及整体上线验收均未因此完成。尤其官方0.2.9常规创建API不能原子禁用新账号，不能用创建后立即关闭绕过既有业务要求。

## 2026-09-29 后续已授权核验

用户明确确认官方承诺后，另外的本机操作通过官方API提交确认并创建regfactory-gemini分组ID=2；本探针本身没有增加写接口。最终probe-after-consent.json返回READY、group_id=2、execution_connected=false，退出0。先前COMPLIANCE_REQUIRED保留为真实历史，已不再是当前阻塞。下一项是临时实例原子停用创建补丁，不把管理连接成功当整个项目上线。
