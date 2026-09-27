# B3 验证记录（隔离环境）

## 基线与范围

- 工作树：`/workspace/reg-factory/.worktrees/gcloud-p1a`；分支 `feature/gcloud-p1a`；基线 HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`。
- 本批差异相对于本轮完整路径快照，不是相对于 HEAD 把全部未提交的 P1a–B2 混算成 B3：证据根 `/workspace/gcloud/.local/rf-b3/`，`baseline.json` 覆盖61个原文件，`baseline/` 保存对应相对路径副本。
- 只用 `.venv-onboarding` 和 B0 专用 PostgreSQL 的临时 manifest schema；没有真实用户、付款、Google/Vertex/Sub2API调用、永久schema迁移或上线部署。
- B2 回归基线267项。B3 新增73个顶层测试（包括runner测试内部执行的旧用例，不重复计为新的顶层测试），另显式跑73个原WebUI/账号解析回归，总计目标413项。

## 可重跑命令

```bash
cd /workspace/reg-factory/.worktrees/gcloud-p1a
umask 077
.venv-onboarding/bin/python tools/onboarding_test_runner.py \
  --suite all --legacy --require-no-skips \
  --json-result /workspace/gcloud/.local/rf-b3/root-final-result.json \
  > /workspace/gcloud/.local/rf-b3/root-final-tests.txt 2>&1
.venv-onboarding/bin/python /workspace/gcloud/.local/rf-b3/browser-check.py
node --check webui/static/onboarding.js
node --check webui/static/onboarding-login.js
uv pip check --python .venv-onboarding/bin/python
git diff --check
```

runner也可选择`db/security/web`命名分组；缺依赖、缺测试库、连接失败、skip均不能验收成功。不与其它代理同时跑PG测试。

## 真实浏览器（不是只画静态页面）

`browser-check.py`导入当前真实`webui/server.py`，所有ROOT/env/static指向临时合成目录；用临时TLS证书+127.0.0.1随机端口启动真实ASGI应用，`proxy_headers=False`。Chromium仅允许当前回环origin，未挂载真实账号目录；结束关闭浏览器/服务线程/端口并清理临时schema。

`browser-result.json`记录12项行为检查，390/768/1024/1440四个实际viewport，各截图登录/控制页/403/503/会话过期，共20张。已检查：

- 首页匿名导航重定向到真正存在的登录入口。
- 浏览器同源GET bootstrap不人为补Origin；真实强scrypt登录成功，浏览器接收Secure/HttpOnly/Strict cookie。
- 页面内存CSRF取得与实际任务查询/暂停/只读核验；无浏览器storage、无密码URL、无pageerror和外站请求。
- 本人范围外查询403；注入真实依赖服务失败503；数据库会话过期401，按钮禁用而不自动重试。
- 真实env编辑UI保留/替换/明确清空，检查临时文件与空密码输入；退出登录撤销会话并由浏览器删除cookie。
- 四宽度document/body均无横向溢出，root实际查看了桌面控制页及手机失效页截图。

浏览器夹具曾有两处错误并已修复，未改安全策略：Playwright wait_for_function内部eval被CSP正确禁止，改为受控CDP evaluate轮询；到期注入只改expires_at违反idle_expires_at≤expires_at约束，改为同时设置两者。不能把这些夹具错误报告成产品绕过成功。

## 失败保留与修复依据

1. 旧WebUI初跑73项：3fail/2error。2error为隔离venv缺curl_cffi，补入锁定依赖；3fail用原baseline server原样复现，同样3fail。Windows两例仅给server局部OS视图/常量，不改宿主系统或旧断言；代理一例保留旧测试本已安装但被module reload冲掉的Mock，不跳过reload或真实业务判断。证据`legacy-first.txt`、`legacy-baseline-three-red.txt`、`reproduce-legacy-baseline.py`、`legacy-full-green.txt`。
2. 首轮全413：1fail/0error/0skip，173.925s。旧B2测试的`mkdir(mode=0755)`被验收进程umask077收紧为0700，导致“危险路径”实际安全。仅给测试wide_mode夹具显式chmod0755；产品安全校验未变。原始证据`root-first-tests.txt`/`root-first-result.json`；修复负测`umask-fixture-green.txt`。
3. 原始RED/GREEN覆盖guard/auth/bootstrap限流、安全头、任务服务、env严格JSON/敏感URL/错误投影、真实页面alias和登录表单POST退化保护；不删除失败记录以制造首次通过。

## 独立复核

- `review.md`：独立规格/安全逐行审查、基线71method+path库存、修复闭环和生产门禁；纯/mock48项通过，不拿代理口头结论代替root整验。
- `review-server-independent.md`：另一代理独立审server全部diff，URL query/fragment分类缺口已修。
- `auth-implementation-review.md`、`server-legacy-delivery.md`：函数行段、原有规则变化、表字段、页面操作结果。
- `remaining-readiness.md`：整体上线仍缺P1c–P5，列已确认需求R01–R18、真实合同/权限/授权/环境差距。

## 依赖与数据完整性

- 28个锁定包；旧21包版本及原SHA均保留，新增7包用于旧回归/真实浏览器验证；没有自动下载浏览器二进制，也没安装整个Android/全浏览器服务栈。
- `requirements-onboarding.lock` SHA256：`b901f926e133a59c11fb2af1038fbbe15841224f1a2a0a79fe18a3a687dd16bd`。
- 001迁移SHA256仍为`3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782`，B3没有DDL。最终清理后再核对14表/141列、永久计数与临时schema/key文件。
- `uv pip check`通过只证明当前依赖一致，不等于漏洞扫描/供应链认证。真实Windows、反代、生产TLS、OS执行身份隔离、备份恢复和第三方实机尚未验证。

## 最终结果

第三轮带观测统一验收结束于 **2026-09-24 09:44:09 UTC**：**413项 / 0fail / 1error / 0skip，216.798秒，退出1**。原样runner仅在B1Case.setUp迁移绑定加私有观测wrapper；不修改SQL、超时、JIT或持久性配置，不重试失败用例。

- 159次临时迁移中1次失败，观测自身0错误。失败是`test_onboarding_repository.RepositoryTests.test_lock_rejects_disabled_wrong_permission_and_other_owner`的setUp，尚未进入该业务断言。
- 捕获001整批DDL阶段原生`QueryCanceled / SQLSTATE 57014`，耗时6.565秒；126个采样中125个为`IO / DataFileImmediateSync`，没有blocking PID或未授予锁。这支持**本次超时发生于数据文件同步等待**；并未定位宿主磁盘/文件系统具体故障，也不能反推所有历史超时同因。
- 第二轮413项曾有2个setUp错误，176.392秒；私有PG日志确认statement timeout，但旧日志未保留SQL阶段。`root-second-*`原样保留。中间40次有界迁移全部成功，只是未复现，不算修复或验收。
- 证据：`root-observed-tests.txt`、`root-observed-result.json`、`full-suite-migration-observations.jsonl`；wrapper及无PG自检保存在`.evidence/`。不公开SQL、参数、DSN或原始异常。
- 最终数据核对：永久14表/141列，schema_migrations仅1行，其余13表均0；临时schema为0、fixture key目录为0；001 checksum不变，JIT仍on。详见`final-database-check.json`。

**当前B3完整阶段验收仍未通过。** 不调大超时、关闭fsync、迁移到内存盘或反复重跑至绿来冒充修复。后续可并行开发无PG依赖的离线组件；它们单独记录为P1c，不混入本批413项证据。整体系统尚未达到上线标准。
