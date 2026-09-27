# Task5b 纯类型分段验证（不是整体验收）

证据目录：`/workspace/gcloud/.local/rf-p1c/task5b-history`。范围为 pool_contracts、新类型测试和 runner 的 offline 登记/显式集合期望；数据库历史入口尚未实施。

## 实际测试

- 作者初始缺模块 RED 保留；首次尝试的 SQL 集合测试误将 task-only combined 混入邮箱平台集合，失败保留，随后明确区分两个原有约束，未修改 SQL。
- spec 发现 exact Platform 可绕构造伪造；四个 constructor/validate 子用例 RED 后，以声明成员 identity 最小修复。源码固定 SHA `564ee950aaeaac8299bf501fd9cc913e84c66688f1da194cb3c6de7d4e326a10`。
- quality 发现测试 reload canonical module 使旧对象失效；独立最小复现与作者 RED 保留。测试改为临时 alias import、保留边界 spies，并补类身份/旧对象/清理回归。最终测试 SHA `37e0501cdb81bc625dd9ef17639a64dd84d9f7fce9a0b279e3c89bf4a8ebc4d3`。
- root runner 注册：文件发现+显式集合 2 RED → 2 GREEN，未登记不存在的 pool_repository。
- root 最终 offline：**62 tests / 0 failures / 0 errors / 0 skipped / exit 0**；耗时 1.756s（runner总耗时1.981s），见 root-offline-final.txt/json/exit。
- root 相关旧 contracts + 完整 runner：**10 tests / 0 failures / 0 errors / exit 0**，0.764s，见 root-related-final.txt/exit。原 legacy 测试触发 webui/server.py:833 未关闭读取文件的 ResourceWarning，未修改/屏蔽该无关代码，不能说日志无告警。
- 两组共72个测试，无重复模块；新纯类型12项包含在offline内。构造/import/validate边界的 DB/socket/subprocess/auth spies 实测零调用；不等于 provider 集成验证。

复现：
```bash
PYTHONDONTWRITEBYTECODE=1 .venv-onboarding/bin/python tools/onboarding_test_runner.py --suite offline --require-no-skips
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests .venv-onboarding/bin/python -m unittest -v test_onboarding_contracts test_onboarding_test_runner
```

## 证据及未验证边界

最终4代码文件SHA见 root-final-code-sha.json，74原文件基线仅授权的runner与其集合测试2处变化。独立 SPEC PASS 已补核最终测试SHA；独立 QUALITY PASS（quality-types-review.md补核02）关闭测试污染，原最小复现2项及相关纯组合24项通过、PG连接0，4文件前后SHA稳定。root仍独立执行上述72项最终验证，未以代理口述替代。

Task5a root255中的跨operator race FAIL、旧审批001setup ERROR仍未关闭。新一次遥测仅1case未复现，未修改配置服务代码/超时。详情 task5a-config/race-diagnostic-01/root-interpretation.md；不得把本段72项纯回归换算为旧255通过。

无新DDL、表字段、权限、HTTP、页面、worker或云资源操作；没有完整PG回归、真实Google/Vertex/Sub2API集成、费用批准、生产安全/部署验收。整体目标保持进行中，本段不表示上线可用。
