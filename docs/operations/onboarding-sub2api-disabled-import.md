# 临时 Sub2API：官方确认与首次停用创建

日期：2026-09-29。适用且仅适用 `/workspace/reg-factory/.local/sub2api-temp-20260929`，地址 `http://127.0.0.1:8800`。不是正式实例上线报告。

## 已授权的目标

用户明确确认官方 Sub2API 承诺并授权修改临时实例。官方承诺 `v2026.06.10` 通过现有 `/api/v1/admin/compliance/accept` 提交，随后读取 `required=false`；没有改数据库伪造确认或绕过中间件。专用 Gemini 分组 `regfactory-gemini`，ID=2。管理探针退出0，`READY`但仍`execution_connected=false`。

## 改了什么

```text
原创建请求（不传新参数） --> active   + 可调度  （兼容原行为）
指定 create_disabled=true --> inactive + 不调度  （首次INSERT）
                                      |
                                 用户另行启用
```

- 单笔账号创建及批量创建中每个账号均支持 `create_disabled`。
- 使用账号已有 `inactive` 状态。不能改用其他资源的 `disabled`，否则原编辑页面可能把未知状态当active。
- API → 服务输入 → builder → 原repository同次INSERT，不是先创建启用再关闭。
- 原权限、管理员合规、输入验证、复制、默认分组逻辑及普通创建保持不变。
- `omitempty` 保留省略或false时旧请求JSON，避免改变已有幂等指纹。
- Gemini service_account不满足创建后的OpenAI/Grok探测或OAuth隐私设置条件；不做任何真实模型测试。

## 代码和表字段

可复用补丁保存在原reg-factory项目：`integrations/sub2api/patches/0001-create-disabled.patch`。固定官方v0.2.9 commit，manifest记录基线与修改后文件hash。

| 下游文件 | 变化 |
|---|---|
| `backend/internal/handler/admin/account_handler.go` | DTO新bool字段；单笔和批量分别透传 |
| `backend/internal/service/admin_service.go` | CreateAccountInput新bool字段 |
| `backend/internal/service/admin_account.go` | builder为显式停用请求设置inactive和false |
| `backend/internal/handler/admin/account_create_disabled_test.go` | DTO兼容/类型及单批量映射回归 |
| `backend/internal/service/admin_account_create_disabled_test.go` | 首次持久化状态、失败保持停用、原校验回归 |

没有新增表、列、索引或迁移；使用已有 `accounts.status` 与 `accounts.schedulable`。原reg-factory数据库、任务占用、邮箱池和8799进程均不改。临时Sub2API承诺确认更新的是其原有settings机制；专用分组使用其原有groups表。

## 原有业务规则没有被改写的部分

- 创建与分组绑定仍不是完整事务；绑组失败可能留下一条已停用的记录。
- 幂等不是网络失败可无脑重试的许可，未知结果先核对。
- 原官方未补丁实例可能忽略未知字段；不能把create_disabled发给未经核验的实例再假定安全。
- 当前补丁是API能力，前端未新增开关；原页面仍显示已有停用状态，编辑/启用必须走原管理员操作。
- 页面创建任务不等于Google已开通。当前reg-factory真实worker没有因本补丁自动接上。

## 验证与运维证据

证据目录 `/workspace/gcloud/.local/sub2api-atomic-evidence/`：
- `atomic-red.log`：原基线明确失败，不以下载/编译中当RED。
- `atomic-green.log`：新回归通过。
- `root-tests.jsonl`、`root-tests-summary.json`：根代理独立回归结果。
- `frontend-build.log`：官方pnpm9冻结锁；i18n/类型检查与嵌入前端构建。
- `backend-build.log`、`build-source.sha256`：Go1.27.0，未降级依赖。

临时实例 `evidence/authorized-init.json`、`probe-after-consent.json` 记录实际确认和管理连接。`backups/before-create-disabled/manifest.json` 记录官方二进制和pg_dump归档hash；数据库归档目录检查通过。初次pg_dump因为私有libpq不在搜索路径而未启动，设置该命令私有LD_LIBRARY_PATH后成功，不改系统库。

重启脚本采用私有回执、产物gate、PID/start_ticks/exe/cwd/监听socket核对、pidfd定向停止，仅启动当前临时应用，不重启数据库/Redis/8799。测试脚本只使用不可用的伪SA材料，通过管理API创建、核验、删除本次记录；清理是原接口逻辑删除，不声称物理记录清空。未知写不自动重放，幂等key在请求前落盘。

## 回滚

先停用任何新创建调用方。核对runtime中的当前PID/start_ticks/exe/cwd后，通过pidfd停止本机补丁应用；使用备份或保留的`bin/sub2api`官方二进制和同一私有env重新启动，并更新runtime指纹、做健康/登录/合规核验。无迁移，不常规恢复数据库备份或抹去合规/审计记录。原官方版本不认识create_disabled，回滚后该调用能力必须保持关闭。未经身份核对不按端口批量杀进程。

## 不代表完成

这是临时下游的一个验收切片，不是整个Google开通平台达到上线标准。真实执行器、真实邮箱/秘密消费、结算及赠金核验、专用项目/Vertex/SA、收费批准和断点恢复仍需独立完成。

## 最终运行结果

- 根代理独立选定回归46个顶层测试+25子场景全部通过，0失败；repo包没有匹配本次过滤条件，只编译成功，不声称其集成测试运行过。
- 前端构建包含3项i18n检查、vue-tsc和Vite，退出0；存在上游大chunk及旧浏览器数据提示，未把提示消掉冒充无警告。Go1.27.0嵌入前端构建退出0，三个生产源hash与冻结基线核验一致。
- 规格、安全与操作脚本独立复审通过本范围，原子停用补丁已实际运行，版本0.2.9-regfactory.1。
- 单笔和批量分别创建1个无效测试SA，API及数据库均读回inactive/false；之后按精确ID/名称删除。数据库剩2条已删除审计记录，未删除账号0、可调度账号0、APIKey0、usage_logs0。没有真实Google秘密或模型请求。
- 最终管理probe为READY、group_id=2、execution_connected=false；嵌入前端200且有脚本，未登录管理API401。
- 部署及真实smoke证据：临时实例evidence/patch-deploy.json、disabled-import-smoke.json、post-patch-http.json、probe-after-patch.json。临时补丁binary SHA256为 `ad00eec28051dc72e7488efffcda2e98487287eb1d1ee138ec9ec43e64c92364`。
