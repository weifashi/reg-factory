# 隔离验证依赖检查（2026-09-24）

只修改 `requirements-onboarding.in/lock` 的 Requests 固定版本：2.32.5 → 2.33.0。其余27个包的完整版本/hash块字节不变；仍为28包。未改全局Python、项目通用requirements或生产机器。

## 依据与影响

PyPA pip-audit 2.10.1对原锁文件返回两个重复公告条目，实际为**同一个**已知问题：GHSA-gc5v-m9x4-r6x2 / CVE-2026-25645。涉及直接调用 `extract_zipped_paths()` 时复用可预测临时文件；不能据此声称普通HTTP请求存在远程攻击面。项目非测试源码未检出直接调用。修复版本与适用范围见 [Requests 官方公告](https://github.com/psf/requests/security/advisories/GHSA-gc5v-m9x4-r6x2)。

测试在独立TemporaryDirectory内创建合成zip和预置同名文件，旧版本实际读到预置内容；升级后读到zip正确内容且不覆盖预置文件。没有攻击其他用户目录、联网调用或真实秘密。

## 证据

原始目录：`/workspace/gcloud/.local/rf-readiness/`。

| 证据 | 结果 |
|---|---|
| dependency-audit.json/txt | 28包，Requests一个唯一公告，exit1；原始重复条目保留 |
| dependency-red.txt | 新回归1项真实FAIL，读到了预置合成内容 |
| lock-update.txt / lock-install.txt | 仅Requests固定版本升级，hash锁安装 |
| dependency-green.txt | 相同回归1项PASS |
| legacy-after-upgrade.txt | 原WebUI/账号解析73项PASS、0skip |
| dependency-audit-after.json/txt | 28包，当前服务未报告已知漏洞，exit0 |
| dependency-review.md | 独立复核官方公告、diff、完整hash块与测试 |

新锁SHA256：`7c1d576bfd2ff84dd3dcdf11c18886865f38173cb4043eb60068447d3170a4fd`。`uv pip check --python .venv-onboarding/bin/python`也通过。

## 重跑

```bash
umask 077
uvx --from pip-audit==2.10.1 pip-audit \
  --require-hashes --disable-pip --strict --progress-spinner off \
  -r requirements-onboarding.lock -f json -o /私有证据目录/audit.json
PYTHONPATH=tests .venv-onboarding/bin/python -m unittest test_onboarding_dependencies -v
```

检查工具运行在独立uvx环境，不进入应用依赖；未使用`--fix`、忽略公告或跳过包。参数含义按 [PyPA pip-audit 官方文档](https://github.com/pypa/pip-audit)核对。扫描会向公告服务查询公共包名/版本，不发送项目代码、账号、卡资料或配置秘密。

“未报告已知漏洞”不是无漏洞保证，不覆盖宿主OS、浏览器二进制、全部旧项目可选依赖或供应链全面认证。B3历史413项使用旧锁的结果保留；新的完整回归须另记实际结果，不能复用旧证据。
