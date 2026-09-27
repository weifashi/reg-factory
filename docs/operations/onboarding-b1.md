# B1 持久任务、租约与回执（仅合成执行）

## 范围与边界

- 使用现有隔离 worktree `feature/gcloud-p1a`，不修改 main、旧邮箱池或旧注册入口。
- 复用 B0 私有 Unix socket / `rf_onboarding_test` / `001_core.sql`，B1 无 DDL、无迁移 checksum 修改。
- `FixtureActor` 只代表内部测试身份；查询库中权限、禁用与任务所有者，不是登录认证。不能作为公网 API 接入凭证。
- 所有资源必须是 `fixture:` 合成引用；配置全部是 `fixture` 名称，`secret_refs` 本阶段必须为空。审批 primitive 支持 test/enable/download，但协调器只执行内置 test，且执行器无网络/子进程/插件注册。
- 没有真实 Google 注册、试用赠金、支付、Vertex、密钥或 Sub2API 调用；也不实现代理换 IP 规避验证、虚假支付地址或重复领取赠金。
- 旧匿名控制面、旧脚本并发并没有被新租约保护。B2/B3 认证、保险库和路由守卫另行验收。

## 数据与事务约定

```text
FixtureActor -> task lock + DB permissions + ownership
             -> lease lock + current owner/fence + DB clock
             -> approval lock + revision + DB clock
             -> INTENT + consumed approval + audit
             -> confirmed COMMIT -> local one-use permit
             -> fixed fixture -> receipt + step + task + audit
```

- 数据变更服务必须在 `storage.unit_of_work(settings)` 中执行；异常必须传播到 UoW，不能捕获失败后继续提交同一业务事务。
- `repository.bump_task` 是内部 CAS primitive，不能改 id、owner、config；状态合法性与审计由调用服务负责。所有命令 version 必须正整数，缺失/布尔值不允许绕过 CAS。
- 最新全局配置决定新合成任务的不可变配置快照，创建接口不接收批次模型/地域/目标覆盖。
- 任务版本用于并发控制；`resource_revision` 由任务 id、generation、config_revision 计算，普通版本递增不会让已批准资源自动失效。
- 稳定请求键：同键同参读原回执，异参冲突；原 INTENT 不能重新获得发送许可。取消、暂停、核验自身也有回执，但其 SUCCEEDED 仅表示本地命令受理。
- 准备事务确认提交后产生的许可只存在于本进程、绑定对象身份、消费一次；重启丢失许可宁可等待核验，不恢复或扫描重发 INTENT。
- 暂停不能承诺撤回已经在途的动作。取消只停止新动作，未决回执/正在执行的步骤保留占用。
- 租约 TTL 到期不等于逻辑释放；owner 交接需要内部 `FixtureStopEvidence`，它不是实机停止证明，不能接受客户端随意声明。
- 旧 fence 不能续租、推进或释放；当前 owner 可以核验同代旧回执。迟到事实追加审计而非强改终态，矛盾证据进入 CONFLICT。
- 审计 summary 有固定字段和值白名单，不写原始秘密/OTP/第三方响应。应用角色只能读/追加审计；DB owner/root 仍可更改，不能宣传不可篡改。

## 本机验证

在 worktree 运行（不得打印 app.json / migrator.json 内容）：

```bash
.venv-onboarding/bin/python -m unittest discover -s tests -p 'test_onboarding_*.py' -v
.venv-onboarding/bin/python -m compileall -q onboarding tests/onboarding_b1_support.py
.venv-onboarding/bin/python tools/onboarding_test_db.py status
git diff --check
```

- 每个 DB 测试生成 `rf_p1b_test_<UUID>`，读取明确的固定私有配置；独立进程不通过 pickle 传递密码。
- 完整测试集串行运行。真实并发用 spawn + Barrier/Event + 独立连接；不用随机 sleep 赌竞态。
- 测试库不可达必须失败，不能 skip 或退回内存库。不要导入旧业务启动模块来跑这些测试。

## 故障与回滚

1. 未确认 COMMIT：返回 COMMIT_UNKNOWN，只查询稳定请求键，不重发、不返还批准。
2. 发送前/结果前/结果审计失败：INTENT 可能仍在库中；已消费许可不复原。没有真实调用，因此本批只证明本地保守行为，不证明外部 exactly-once。
3. PostgreSQL 明确死锁回滚或提交前连接断开：测试验证回滚后可显式重试一次；没有部署自动重试循环。任何未知提交不得走该路径。
4. 暂停 B1：停止运行合成测试/调用入口即可，本轮无常驻调度器，无代码部署。
5. 代码回退：只按 B1 文件底账备份/移除本批新增文件，保留既有 P1a/B0 与用户工作树改动；不要 `git reset --hard`。
6. 数据回退：B1 无 schema 迁移可回退。临时 schema 只走 B0 manifest 校验清理；不得 DROP 整库或清空永久业务表。
7. 不释放 UNKNOWN/CONFLICT hold 来“解除阻塞”，不删除回执以重跑；保留审计/回执供核验。
