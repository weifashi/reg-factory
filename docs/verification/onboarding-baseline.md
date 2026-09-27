# P0A：reg-factory 首批离线实现基线

日期：2026-09-24。用户授权：多代理执行已批准计划 P0A/P1a，非P1b及后续接入。

## 仓库与隔离

- 主仓库 `/workspace/reg-factory`，`main`，初始工作树干净。
- 基线 `b0758484c2401ea38792e18711ff38ef8a24105c`。
- 隔离工作树 `/workspace/reg-factory/.worktrees/gcloud-p1a`。
- 实现分支 `feature/gcloud-p1a`。不切换主树分支，不提交、暂存、推送、部署。
- 无可用原生worktree工具；使用 `git worktree add .worktrees/gcloud-p1a -b feature/gcloud-p1a`。
- `.worktrees` 原先不存在且无忽略规则。为避免修改主树版本化 `.gitignore` 或未经授权提交，仅在本地 `.git/info/exclude` 增加 `/.worktrees/`；已用 `git check-ignore -v .worktrees/gcloud-p1a` 确认。该本地Git元数据不属于业务源码diff。
- 适用规则 `/workspace/AGENTS.md`；目标仓库无项目级AGENTS.md。

## 解释器与测试环境

- `python3 --version`：Python 3.12.3。
- `node --version`：v22.23.2；本批无JS修改，不以本机Node替代未来CI Node20验收。
- 无第三方依赖安装、未创建虚拟环境、未读取或复制 `.env`、邮箱池、凭据、代理配置或DEMO数据库。
- 新增业务模块只用标准库，保持Python3.10兼容语法；真实运行测试使用3.12.3，不能称实际验证过3.10。

尝试的旧基线命令：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_account_records.py -v
```

**退出1，环境未满足，不算业务回归通过**：`tools/import_plus_codex.py` 导入Playwright时报 `ModuleNotFoundError: No module named 'playwright'`，测试方法尚未执行。没有安装依赖或屏蔽错误来伪造通过。首批计划仅标准库纯模块，该限制不阻塞本批；既有业务回归须以后在隔离依赖环境核验。

原始日志：`/workspace/gcloud/.local/rf-p1a/p0-baseline.txt`。不宣称现有完整测试基线通过。

## gcloud 原模块只读复用判断

| 旧文件 | 可复用思想 | 本批处理 | 不能沿用的限制 |
|---|---|---|---|
| /workspace/gcloud/onboarding/domain.py | 未授权阻塞、无法判断等待 | 依已批准契约重写小型Route决策 | 原使用StrEnum及旧状态词，不能直接当reg-factory平台流程 |
| /workspace/gcloud/onboarding/policy.py | 费用/证据门禁、aware时间与UTC比较 | 新ApprovalView/ExecutionView纯门禁；保留DST正确性意识 | 原Permit不含本次独立启用和资源版本契约 |
| /workspace/gcloud/onboarding/runner.py | UNKNOWN不自动续跑 | 本批只做BindingState归类，不迁移runner | 内存runner不等于持久队列、跨进程锁、回执或一次调用 |

未迁移Flask、DEMO数据、账号密码、运行配置、数据库或密钥；本批也不建立账号永久IP绑定。

## 授权边界

允许：隔离工作树新增纯规则与测试、只读代码复核、静态说明页、原始测试证据。
不做：数据库连接/迁移、真实共享池切换、凭据采集、浏览器登录、付款、短信、Google/gcloud/Sub2API调用、模型费用、权限变更、部署/提交。
