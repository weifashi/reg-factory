# 真实 Sub2API 接入第一步：受控联通核验

> For agentic workers: 使用 subagent-driven-development；先写失败测试，分开做规格和安全审查。用户已要求连续执行，不再询问是否继续。不提交、不推送、不部署正式环境。

**Goal:** 在 reg-factory 中提供实际访问已授权 Sub2API 的受控核验入口，明确区分服务健康、认证成功、合规待确认、Gemini 分组匹配；不是 Google 流程完成标志。

**Architecture:** 新增独立、只读管理接口客户端及本机 CLI，使用当前 httpx 依赖，不复用旧上传器的写请求自动重试。凭据只从本机私有文件读取；不接受 HTTP 页面传入的任意地址。保留原 synthetic 池和已有任务，不直接放开旧保护。除登录/退出会话外不进行远端写操作。

**Tech Stack:** Python、httpx 0.28.1、unittest、真实本机 Sub2API v0.2.9。

## 已确认的总目标与当前分解

真实数据与共享占用 → 官方 Google 授权/支付核验 → 专用项目/Vertex/服务账号 → Sub2API 禁用导入 → 单次费用批准 → 端到端及恢复验收。

当前先实现其中不依赖 Google 凭据的实际下游连接核验。Sub2API 法律承诺必须由用户确认；本模块绝不调用 `/admin/compliance/accept`。Google 凭据、首次本人验证及费用批准不因本计划而省略。后续生产数据库/秘密消费者和执行器必须分别设计、测试，不能把本步骤误报为整体交付。

## 文件与合同

- 新增 `onboarding/sub2api_probe.py`：私有目标配置、有限响应解析、HTTP 核验和脱敏结果。
- 新增 `tools/onboarding_sub2api_probe.py`：本机 `--config`、`--group`、`--allow-loopback-http` 入口；只打印白名单 JSON，失败非零退出，不把秘密作为命令行参数。
- 新增 `tests/test_onboarding_sub2api_probe.py`：配置边界、真实 HTTP/TLS 行为和结果白名单测试。
- 新增 `docs/operations/onboarding-sub2api-probe.md`：用法、实际证据、未连接的能力。
- 修改 `tools/onboarding_test_runner.py` / `tests/test_onboarding_test_runner.py`：把新增探针纳入 offline/all，补回已完成但漏登记的 mailbox_table/web 与 safe_cancel/pools；既有“所有模块都登记”回归必须通过，不修改业务断言。
- 不改现有表、迁移、权限、配置 API、页面成功状态或任务状态。

### Task 1：配置边界与失败测试

- [x] 测试先执行 `importlib.util.find_spec('onboarding.sub2api_probe')` 并断言非空，确认新增能力缺失。
- [x] 配置 JSON 精确字段为 `url,email,password`；兼容已授权临时实例的 `admin-access.json`。拒绝重复 key、超大文件、软链接、非普通文件、非本人持有、非 0600 文件、组/他人可访问的直接父目录。限制输入 16 KiB。
- [x] 凭据类型严格为非空字符串；拒绝 URL userinfo、路径（允许空或 `/`）、query、fragment、控制字符。默认只接受 HTTPS；仅显式 `--allow-loopback-http` 时允许 `http://127.0.0.1:<port>`，不允许 localhost、其他 IP 或域名伪装。
- [x] 目标对象 repr 不包含秘密；错误仅固定代码，不回显文件内容、URL 凭据、服务器错误正文、token。

### Task 2：有限真实 HTTP 核验

- [x] 顺序 `GET /health` → `POST /api/v1/auth/login` → `GET /api/v1/admin/compliance` → 若已确认则 `GET /api/v1/admin/groups/all` → `POST /api/v1/auth/logout`。
- [x] `httpx.AsyncClient(trust_env=False, follow_redirects=False)`，TLS 验证保持开启；`asyncio.timeout` 覆盖完整请求 stream，每请求10秒且退出独立预算，响应最多 1 MiB（raw 流式限制）。OS DNS 线程退出限制见操作文档。不自动重试任何请求，不访问响应中给出的 URL。不得开放任意方法/path 的公共调用入口。
- [x] 严格要求健康 JSON `status=ok`，登录 code 精确整数 0、data 包含非空 access_token/refresh_token。只按固定 schema 读取合规 required 布尔值及版本；未知/畸形响应失败关闭。
- [x] 合规待确认返回固定 `COMPLIANCE_REQUIRED`，不做分组请求。已确认时按精确名称、`platform=gemini`、有效正整数 ID、可用状态匹配唯一分组；缺失、停用、重名或错误平台不得就绪。
- [x] 登录成功后使用 finally 尝试退出；退出失败独立标识，不掩盖先前失败，也不得报完全就绪。所有结果均明确 `execution_connected=false`；就绪只表示管理端连接核验成功。
- [x] 测试健康失败、登录失败、423、重定向、HTML、坏 JSON、bool/int 混淆、超限响应、timeout、不安全配置零联网、代理环境不生效、无秘密输出及请求顺序；断言没有创建账号、接受承诺或模型调用请求。

### Task 3：验证与独立审查

- [x] 执行：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests .venv-onboarding/bin/python -m unittest discover -s tests -p 'test_onboarding_sub2api_probe.py' -v`；保存真实红/绿结果，不以代理口头通过代替证据。
- [x] 先规格审查，再代码/安全审查，修复后重跑。
- [x] 使用私有配置路径对 `http://127.0.0.1:8800` 执行 CLI。当前预期健康和登录成功、合规未确认；此时命令必须非零返回 `COMPLIANCE_REQUIRED`，不得当成功接入。
- [ ] 用户确认后再按官方 API 创建专用 Gemini 分组并重复核验；未确认不执行该写操作。
- [x] `git diff --check`，记录精确变动及无业务表变更；保留既有所有改动。

## 审查发现与修复要求

- 配置硬链接、压缩响应及正文 slow-drip 边界先有失败回归，再修复。
- 独立审查实际 TCP 复现：同步 `client.stream` 在持续响应头/103 Early Hints 下无法进入 deadline 检查。必须先补真实 TCP 回归，再用可取消的异步请求期限覆盖连接、完整响应头和正文；仅块间时钟检查不算通过。
- root 独立核对测试登记原先漏了 mailbox_table、safe_cancel、新增 sub2api_probe；三个覆盖断言明确失败后才补登记，不把原先未登记的测试说成被 all 套件运行过。

## 最终核验（2026-09-29）

root 在最终异步代码上执行 offline 套件：131 tests，0 failures/errors/skipped，退出0；新探针29项包含真实TCP回归。规格与安全独立复审均通过本切片。实际本机探针健康、登录、退出成功，合规仍 required，按预期退出1，没有创建分组或任何上游账号。证据见操作文档。此结果不是整个项目上线验收。
