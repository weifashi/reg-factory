# P1b B0 专用测试存储操作记录

2026-09-24。仅适用于本次同机 Linux、合成数据测试。不是生产部署说明，也不授权真实邮箱/信用卡/Google/Sub2API操作。

## 已建立的边界

- 代码工作树 `/workspace/reg-factory/.worktrees/gcloud-p1a`，未提交；main不变。
- 专用运行目录 `/workspace/reg-factory/.local/onboarding-p1b`（0700），data/socket/本次日志均在此。
- PG16二进制复用 `/workspace/gcloud/.local/pgdist/usr/lib/postgresql/16/bin` 与解包动态库，仅复用程序，不读旧库/旧socket/旧凭据。此路径是本机约束，搬机器必须重新审查，不称可移植安装器。
- 独立 PostgreSQL 16.15、库 `rf_onboarding_test`、schema `rf_onboarding`。仅私有socket，55433是socket后缀端口号，无TCP监听。
- 应用角色 `rf_onboarding_app`、迁移角色 `rf_onboarding_migrator`，均非superuser/createdb/createrole/replication/bypassrls。migrator拥有本库建schema权限，应用无DDL/临时表/public建表权限。
- `app.json` 和 `migrator.json` 均0600；不要cat、上传、贴聊天或放HTML静态目录。应用不用迁移身份，测试工具的peer bootstrap只用于本次实例管理。
- 14表/141字段为结构底座，不是已实现登录、加密、租约或审批流程。schema_migrations有1行，其他表本轮无业务记录。

## 在代码工作树执行

```bash
uv pip sync --python .venv-onboarding/bin/python requirements-onboarding.lock --require-hashes --index-url https://pypi.org/simple
.venv-onboarding/bin/python tools/onboarding_test_db.py status
.venv-onboarding/bin/python tools/onboarding_test_db.py prepare
.venv-onboarding/bin/python -m onboarding.migrate --settings /workspace/reg-factory/.local/onboarding-p1b/migrator.json
```

prepare用于首次初始化或已确认ready实例的启动，不重复生成凭据。遇到preparing或未知既有文件时拒绝，先查本次私有状态，不删除目录重来。迁移只显式执行：已应用相同版本/校验和为no-op，同号不同内容拒绝；失败事务不留半套表。没有Web启动时隐式迁移。

依赖锁固定21个包及hash；root requirements增加psycopg意味着以后安装全量requirements也会安装它，并非pip意义的optional extra。独立venv没有安装旧浏览器/Android整套依赖。首次解析误用非simple索引端点曾失败，改官方/simple后成功，未改版本绕过。

## 测试与安全清理

```bash
PYTHONDONTWRITEBYTECODE=1 .venv-onboarding/bin/python -m unittest discover -s tests -p 'test_onboarding_*.py' -v
```

本批DB测试必须连接此专用实例；缺库是失败，不skip。不要与其他迁移/实例管理测试同时运行验收套件，以免触及短事务锁/语句超时；异常需保留原始结果，串行复现不是抹掉失败证据。

每个fixture创建 `rf_p1b_test_<32hex>` schema及本次manifest（0600），schema内部另有 `_test_marker` 技术表，不计入产品14表。正常结束自动清理；失败manifest保留。手工清理仅使用本次manifest的精确路径：

```text
python tools/onboarding_test_db.py cleanup-schema --schema <本次精确UUID schema> --manifest <本次0600 manifest绝对路径>
```

工具校验库标识、owner、token与schema内标记，取得advisory锁并检查级联依赖。发现外部view/FK/传递依赖或不明归属对象即拒绝，不能加force绕过。不接受rf_onboarding/public/旧p1b，不删除数据库。禁止 `rm -rf`、通配DROP或把manifest改成另一个schema来清理。

## 停止、回退、备份

```bash
.venv-onboarding/bin/python tools/onboarding_test_db.py stop
```

只停止本次专用实例，不删除data/回执/配置；main与现有WebUI无变化。B0回退优先停实例并保留证据，不需要还原任何外部账号/付款状态，因为没有执行这些操作。

若需删除本次库/实例或撤销迁移，先独立确认目标和备份；此工具不提供drop-database。后续有业务数据时迁移只前向兼容，不能修改已应用001造成checksum漂移；不可把丢表当作回滚远端副作用。

可停止本次实例后对专用目录做加密离线备份；本轮未实施备份或还原演练，不宣称备份已有效。此库目前含配置密码与空业务结构，不能把整个目录公开归档。

## 剩余限制

- 测试配置受限文件与socket隔离不抵御同OS用户或root；未完成B2 Web认证/SecretStore安全评审，不接受真实PAN/SA JSON。
- 本轮未改webui/server.py、common、页面或旧注册流程；旧匿名入口没有因此变安全，不能公开旧WebUI。
- PostgreSQL连接/UoW验证目标和schema owner，不代表已核验所有业务就绪条件或审批权限；迁移checksum由显式迁移器核验。
- 真实COMMIT断线实验未做；UoW的COMMIT_UNKNOWN使用故障注入证明错误分类，B1仍需完整回执/崩溃恢复验收。
- Python3.12实际执行；3.10只做AST与依赖metadata检查，Windows未验证。
