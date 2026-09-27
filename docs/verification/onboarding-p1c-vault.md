# P1c Task3a 验证记录

2026-09-24。范围：合成 typed PoolVault 只写入 + 独立 RequestMac；不是完整 Task3/池服务/Google 与支付集成，更不是上线交付。当前工作树未提交、未推送、未生产迁移或部署。

## 结论与完整证据

root 在源码冻结后执行一次 `observe_full_suite.py --suite all --legacy --require-no-skips`，终态 **exit 0，503 项 / 0 failure / 0 error / 0 skip，205.463 秒**；时间 `2026-09-24T10:23:07.948913+00:00`。其中 430 项 onboarding + 73 项原有回归，503 个 ID 均唯一，没有重复计数。observer 只包装 B1Case.setUp 的迁移绑定，182 次观测均成功，观察器错误 0；未改 SQL、超时、fsync、JIT，也没有失败自动重试。

私有证据 `/workspace/gcloud/.local/rf-p1c/task3a/`：`root-final-tests.txt`、`root-final-result.json`、`full-suite-migration-observations.jsonl`、`pre-final-source-sha256.json`。结束时冻结 10 个源码/测试文件全部 SHA 一致。

随后仅修正纯测试的低概率随机性：`test_resource_and_secret_ref_are_strict` 原使用随机 UUID 后 upper，极小概率 UUID 恰全数字而大小写相同。强制 uuid4 返回全数字做出真实 RED 后，改为固定含 a-f 的 UUID；同一复现 GREEN，types+runner **12 项 / 0 skip** 补验通过（`uuid-fixture-red.txt`、`uuid-fixture-repro-green.txt`、`uuid-fixture-final-green.txt`）。这一改动没有修改产品代码或测试业务断言，不为测试夹具改动再次运行 PG 整套；503 是修改前冻结测试版本的完整证据，最终测试夹具有上述增量证据。

## 保留的失败，不用文件名判断成功

- 作者中间 `vault-types-final.txt`：26 项 / 1 error，28.103s。失败是 `test_dependency_types_reserved_key_name_and_equal_material_are_rejected` 的 setUp 001 迁移，未进入该测试体；只看到 DEPENDENCY_UNAVAILABLE，当前没有原生观测，不能擅自断言与旧 IO 失败同根因。最终统一运行该测试执行通过；旧失败仍保留。
- `locks-owners-green.txt` 名字含 green，但实际 4 项 / 2 failures：最初观察器试图跨角色读取 wait_event，权限使信息不可见，没能证明实际锁等待。改为公开 pg_blocking_pids 精确确认 blocker PID 后，两项真实资源锁/审计锁跨 TTL 测试通过；没有额外授予 PG 监控权限。
- 前一阶段 465 项有 1 次初始化同步等待错误，97/98 采样 IO/DataFileImmediateSync，记录保留原目录。**本次一轮通过，不等于宿主 IO 根因已经修复**；环境稳定性仍是部署前风险。
- 最后钥文件读取可跨过 TTL、缺字段伪 payload、billing 非固定值、MAC 缺 value 等均曾实际 RED，再修复补验；不把一开始报错说成从未发生。

## 正反向验证范围

| 维度 | 实际覆盖 | 仍不代表什么 |
|---|---|---|
| 6 种严格 payload | frozen、隐藏 repr、拒 dict/subclass/extra/CVV/OTP/缺字段；域与合成值白名单 | 不授权真实资料输入、不保证任意同 OS Python 不可读内存 |
| 加密 | 六类实际 kind AAD 测试端解密一致；不同 id/nonce；错误 kind/id 拒解 | 无产品解密、下载或用途授权 consumer |
| 权限 / 目标 | 实际 Actor、DB live grant/session/epoch/disabled/TTL，manifest、实际 role/schema/search_path 与跨 schema 拒绝 | 无生产 provider、无人值守身份或真实管理员授权 |
| 资源归属 | mailbox email+owner、platform 父邮箱+platform+owner、card/billing owner | 不等于完整池 CRUD / 内容版本更新 / 并发导入 |
| 原子性 | 真资源 INSERT 同事务成功；source_type CHECK 失败明确到达 INSERT 后，资源/secret/audit 全部回滚；审计故障 spy 命中 | 不等于已完成 HTTP 幂等导入服务 |
| 过期与并发 | 真实 PG 阻塞 PID 证明资源/审计锁等待，过期后不返回成功；末次钥读取后也重验 session | 没有 task/lease/fence/pin 消费功能 |
| MAC | 独立域、长度 framing、action+owner+bytes、PAN 无 owner；文件权限/链接/缺失/材料重用/有界扫描 | 不等于 DTO 业务规范化、卡分配或历史 key 迁移 |
| 向后兼容 | 原 Keyring / SecretStore / downloads / security / 全部旧回归纳入 503；旧 store 不接受 pool kind | 不等于旧注册消费者已经切到新 Vault |

最终无新增路由、页面、HTML/JS 或文案代码，所以本轮不重做真实应用浏览器检查来增加数字。前阶段浏览器 12 行为/20 截图仅是历史证据。本批阶段说明 HTML 四视口排版检查是报告验收，不是应用 E2E。

## 独立审查和逐行业务规则

- b0_spec_review 逐行规格/安全审查：只写合成 scope PASS，无开放 blocking；MAC 与共享部分另独立 27 纯测试通过。见 `spec-review.md`。
- b1_receipts 对非本人实现的 vault/types 与 root 共享代码做质量审查：无阻塞；纯类型与边界 7 项通过，固定 UUID 建议已处理。见 `quality-review.md`。
- root 逐行审 MAC、核实际 audit 字段，并独立 MAC+旧 Keyring+权限接口 21 项通过；最终统一运行使用真实测试结果，不替换为代理结论。

| 文件/行段 | 原有规则 -> 本轮规则 / 影响 |
|---|---|
| pool_secret_types.py 1–183 | 新内部输入层；把六用途分型。邮箱与平台密码不混用；字段验证失败先于数据库/加密；导入与声明为无业务行为的样板/导入行一并审阅 |
| pool_vault.py 1–75 | 新只写层；合法 Settings 不能代替实际 conn 校验；只允许 manifest 临时 schema2 |
| pool_vault.py 78–103 | 精确依赖、当前 AES 与 MAC 密钥分离、逐次私有文件读取；无 fallback |
| pool_vault.py 105–135 | 行级资源身份校验；platform 沿 parent 核 owner；billing app 只追加，不能错误要求 UPDATE 权限 |
| pool_vault.py 137–169 | 校验 -> AES -> 同事务 secret/audit -> 再校验 -> ref，调用方仍拥有提交责任 |
| request_mac.py 1–99 | 新域分离 HMAC，无网络/存库/自动建 key；跨 owner 同卡同指纹只提供基础，不假称业务去重已接入 |
| security.py 132–135 | 新 mailboxes:manage 允许值；原 DB membership/epoch 不变，不隐式授权 |
| tools/onboarding_test_runner.py 分组 | 新三测试模块纳全部回归；失败/skip 标准未降低 |
| 五个测试文件 | 断言与合成夹具、真实 PG 负测，无线上业务行为改变；最终 UUID 夹具确定性修复不改变断言 |
| 两份本阶段文档 | 只说明可证范围、风险和回退，无业务运行规则改变 |

## 表字段、清理与运行环境

本批没有表/字段/索引/迁移变动；使用 C0 已有 secret_objects/audit_events 与资源表。`001_core.sql` SHA 仍 `3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782`，002 仍 `b763180ee4d782e6e88f2a78ac3bbf34df026425133e8c05ca99143eb56ec764`。

最后 `final-database-check.json`：永久 schema 仍 14 表/141 列、001 版本行 1 条，其余 13 表 0 行；临时 schema 0、fixture key 目录 0。JIT on/above_cost100000 未变。没有写入真实账户/卡/凭据。

## 下一步

推进邮箱池导入与资源回执服务，再完成 pool task/lease/pin 与 3b 用途授权消费，然后卡分配与旧消费者接入。不能因本切片通过而开放任意 decrypt、真实 PAN 或真实付款。生产密钥 provider/OS 身份、授权真环境验证、完整 API/页面、Google/Sub2API 契约及成本审批仍未完成。运维和回退见 `../operations/onboarding-p1c-vault.md`。
