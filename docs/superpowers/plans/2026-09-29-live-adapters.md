# Google 真实执行接线实施计划

> 执行：subagent-driven-development；沿用已确认的默认自动、失败转人工设计。当前工作树是原项目的工作树，不新增独立产品。不提交、不上线未知链路。

**Goal:** 将普通 Playwright 操作、Google 资源 API、停用 Sub2API 导入与持久操作意图接在同一明确接口上，最终由原页面启动；不能把组件测试称作端到端开通成功。

**Architecture:** 历史 synthetic 任务保持不执行真实动作。新增 explicit live 标记和单次已提交操作许可；每次效果前检查 actor、版本、租约和撤销。HTTP/浏览器超时后只核对，不盲重试。普通授权资料自动填表，验证码无法处理时保留现场。

**Tech Stack:** 原 Python/Playwright/httpx/PostgreSQL；原 WebUI 不换壳。

## 已确认约束
- 不制造地址、身份或赠金资格，不绕过挑战；不把 gcloud 登录当作浏览器登录。
- 不自动削弱组织策略或提权；收费 Gemini 调用和调度启用仍需单独批准。
- 所有真实 secret 禁止进入日志、公开 DTO、异常文本、命令行参数。
- 缺真实资料只阻塞相应联调，不阻塞其他组件实现。

## 并行任务与文件边界
- [x] A：`common/browser.py` normal 模式；`onboarding/google_browser.py` ordinary 表单、暂停、重发保护；对应单测与本地 HTML 浏览器测试。保留旧默认行为。
- [x] B：`onboarding/google_cloud.py` 固定 API 请求与只读核对；token 注入、单次许可、IAM etag 合并、密钥安全 sink；MockTransport 单测，不真实创建云资源。
- [x] C：`onboarding/live_effects.py`、`migrations/003_live_effects.sql`；mode 默认 synthetic，INTENT + confirmed commit 才能发单次 permit；旧 fence/恢复/取消不能发送；隔离 PostgreSQL 验证。
- [x] D：`onboarding/sub2api_import.py`；仅已核临时实例，原子 inactive+false 导入，成功须 GET 读回；未知不重发；秘密不进入结果。
- [ ] E：统一接口并审查 migration/runner；先红后绿，运行涉及的完整测试及 legacy 浏览器回归。

## 接口
```python
# permit 只由已确认提交且经过权限/租约核验的 runtime 签发。
await permit.consume(action)  # 严格 True；一次性，恢复不能重新取得
await GoogleCloudAdapter.execute(action, permit)
await Sub2APIImporter.import_disabled(secret_json, permit)
# browser 必须提供两个回调，禁止默认放行：
GoogleBrowserDriver(page, guard=guard, intent=intent)
# 普通 fill 前 guard；提交前持久意图并消费对应 permit。
```

## TDD 验证顺序
1. 写 `tests/test_onboarding_sub2api_import.py`：模块存在、拒绝没有许可、成功停用导入、读回状态不符、未知只核对、无模型请求、脱敏结果。
2. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests .venv-onboarding/bin/python -m unittest test_onboarding_sub2api_import -v`，确认缺失功能失败。
3. 实现固定目标适配器，重跑该命令，全部通过后增加异常边界用例。
4. 所有代理完成后核对 API 与 SQL，统一执行新测试和原 offline/相关 DB 回归。退出码、跳过和原始输出保留本机 evidence。

## 仍必须完成，不能由本切片冒充
- [ ] 真实资料加密导入与 task-bound 解密消费；原池目前只接受测试资料。
- [ ] real 配置/运行权限和后台授权快照；本切片仅验证 session-bound permit。
- [ ] worker 进程、浏览器恢复/停止证明、取消/资源释放闭环及原页面接入。
- [ ] 真实 Google DOM、联系人/支付 iframe、授权验证码渠道；未知页面保持现场。
- [ ] 单账号真实端到端联调，确认 billing/赠金证据，再云资源及停用导入；未批准模型调用仍不发送。
- [ ] 运行维护、迁移、备份恢复、故障和安全验收后才能称上线完成。

当前没有修改 preview 8799 的运行模式，没有把旧测试数据升级为真实任务。

## 本轮状态说明

A–D 勾选仅表示组件已实现和对应局部验收，不表示原页面、真实 worker 或完整开通流程已经上线。全部剩余门槛保留在上方；新增联系人自动填表不含保存/支付提交。最终全套回归证据另记，先前失败/中断不计成功。

## 运行时后续切片（2026-09-29，未开放原页面真实执行）

- [x] 固定 Google token 来源、临时 Sub2API 进程/制品守卫；SDK 真解析合同与真实只读授权核验。
- [x] 同 session/进程的 batch→worker 交接、心跳、发送与只读核对守卫；25 条局部测试。
- [x] task/source-receipt 绑定的 service-account JSON 加密保存、封闭停用导入；发送前再核秘密撤销/过期。
- [x] 不可变 live 计划 + 实际观察证据；004 只在测试 schema，36 条迁移/存储回归通过，投影边界独立复审通过。
- [x] 内部单任务组合器串行连接上述组件；固定身份/目标、LRO 只读核对、心跳失败关闭发送门。20 条隔离组合测试通过；`CHAIN_VERIFIED` 不是整任务成功/释放，也不是网页真实入口。
- [x] 同进程停止证明、已观察未知提交的冻结否决；组合器结束不再仅凭布尔值证明停止。
- [x] 内部 `run_managed` 收尾：原 session/fence/plan 下，仅已取消且零效果史可安全释放；其他保留占用，七步通过也不冒充首次开通成功。
- [x] 原任务列表/详情/右侧日志按数据库 mode 准确读取；保留旧 schema 2，schema 3+ 不降级；live 历史仅只读展示，未开放启动。
- [ ] 可信 live 创建入口、原 UI/API、真实资料及全流程终态；不得把本切片组件验收冒充该项。

细节与原失败证据见 `docs/operations/onboarding-live-runtime.md`。旧全量 1282 条出现 1 failure + 1 error，尚不能记全量通过。
