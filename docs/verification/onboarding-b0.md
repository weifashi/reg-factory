# P1b B0 交付与证据

2026-09-24。用户批准“先执行 B0 隔离环境与迁移骨架”，不含B1/B2业务、不含真实账号/支付/云调用、不含提交/推送/部署。

## 基线与范围

- 工作树 `/workspace/reg-factory/.worktrees/gcloud-p1a`，分支 `feature/gcloud-p1a`，HEAD `b0758484c2401ea38792e18711ff38ef8a24105c`。
- 原P1a9个文件按 `/workspace/gcloud/.local/rf-p1a/manifest.json` 核验SHA256不变；主仓库无tracked修改。
- 根requirements新增psycopg固定版本，.gitignore仅放行专用lock；没有修改webui/common/原页面/旧注册逻辑。
- 计划来源：`/workspace/gcloud/docs/superpowers/plans/2026-09-24-reg-factory-p1b.md` Task0/1 与配套contracts。后续Task2起均未执行。

## 实际运行结果

- 独立PG16.15实例；私有socket，55433不对TCP监听；两个非superuser角色，应用不能建表/改表/建临时表或删除审计。
- 显式运行001一次成功，第二次输出checksum已核验且未重建；`rf_onboarding`14表141字段。
- 元数据核验：schema_migrations=1行；其余13表均0行；无真实账号、秘密对象或业务任务。
- 测试schema使用本次UUID与manifest，清理检查库标识/owner/token及外部依赖；不删除数据库或主schema。
- 21个隔离依赖固定版本+366个SHA256；官方PyPI simple索引安装。pip-audit返回No known vulnerabilities found（仅当前公告匹配结果，不是供应链/系统安全认证）。

## 实施与审查修复

1. 两代理实现连接保护、实例管理；主代理实现类型、14表SQL、迁移和集成。
2. 规格审查发现测试夹具绕过安全连接器，补RED后改为统一load_settings/open_app/open_migrator。
3. 锁文件被通配ignore排除，添加精确例外，避免交付漏锁。
4. 实际凭据断言改SHA256/固定消息，防失败diff泄漏密码。
5. 清理CASCADE可能跨schema删除依赖，新增外部view/FK/链负测试，拒绝后保留数据与manifest。
6. 质量审查发现管理配置若是FIFO会阻塞；补非阻塞/有界文件读取回归后再统一验证。

## 原始证据目录

`/workspace/gcloud/.local/rf-b0/`（私有本机证据，不上传原始配置）包含：

| 证据 | 内容 |
|---|---|
| baseline.txt / p1a-baseline.txt | HEAD、分支和P1a34项本轮基线 |
| dependency-resolution*.txt / dependency-install.txt | 索引错误与修正，安装锁定环境 |
| installed-packages.json / dependency-audit.json | 安装metadata及公告扫描结果 |
| contracts-red.txt / contracts-green.txt | 不可变记录与安全错误4项 |
| settings-*-red.txt / settings-final-green.txt | 非法目标/文件/字段拒绝 |
| storage-*-red.txt / storage-final-green.txt | 真实连接/事务与提交未知分类 |
| tool-*.log | 实例prepare/权限/重启/清理及修复RED/GREEN |
| migration-interface-red.txt | 缺迁移接口断言RED |
| migration-cli-red.txt / migration-cli-green.txt | app凭据不能执行迁移 |
| migrations-constraints-green.txt | 原子回滚、校验和、实际CHECK/FK/唯一负例 |
| fixture-isolation-red.txt / fixture-isolation-green.txt | 测试夹具也必须遵守连接保护 |
| permanent-migration.txt / schema-inventory.json | 主测试schema显式迁移两次、14表141字段及空业务行 |
| root-suite.txt / root-final-suite.txt | 初轮与最终主代理统一测试输出，以最终为准 |
| runtime-status.json / final-state.json | 专用实例安全状态及清理核对 |
| manifest.json | B0全部文件的增删行数与最终hash |

接口存在性RED只是结构RED；后续CHECK/FK/唯一约束测试是实际数据库验证，不谎称每项都经历独立业务RED。操作工具遇环境错误（libpq路径等）单独保留，不能将其当业务RED。

## 未掩盖的异常与未验证项

- 首次uv解析误用 `https://pypi.org`，改为官方 `/simple` 后按原版本安装成功，没有降版本绕过。
- initdb缺解包动态库路径导致partial；在确认本次data未创建后保留原配置完成初始化，不触碰旧实例。
- 中途libpq `service=''` 被解释成空服务名而非禁用，修为拒绝PG覆盖；本次遗留fixture核对marker/唯一对象后清理，未通配删schema。
- 独立质量复核首轮53项中1个migration报DEPENDENCY_UNAVAILABLE；单项与随后完整串行53项通过。安全过滤日志发现一次statement timeout；未取得首次异常SQLSTATE，不能认定是锁争用，失败结果仍保留。最终主代理90项通过、0 skip（P1a34+B0新增56），见root-final-suite.txt。首轮53项失败及后续53项串行通过只有本对话原始工具输出，没有补造日志文件；质量收尾17项的原始输出已存quality-tool-final.txt。
- 旧 `test_account_records.py` 的缺Playwright基线问题本轮未处理；不声称旧业务全量回归通过。
- Python3.12实跑；3.10仅AST与依赖metadata，Windows未验证。
- COMMIT_UNKNOWN为故障注入错误分类测试，没有真实网络切断；未实现B1持久批准/回执消费和恢复，不宣称exactly-once。
- 新认证、SecretStore、租约服务、用户页面、Google/Sub2API没有接入；B0完成不代表整个P1b完成或旧WebUI可公开。

## 当前页面与回滚

原邮箱池页面“导入到邮箱池”仍走旧 `/api/mailpool`，没有进入B0数据库；只是按源码核对，没有实际导入账号。开发终端显式调用prepare/status/migrate/stop才使用新基础层。

回滚/停止命令与安全清理边界见 `../operations/onboarding-b0.md`。保留DB/manifest/配置，禁止为回退删除未知账本；本批没有外部付款或云资源可撤销。

HTML脱敏交付报告：`/home/weifashi/www/gcloud_onboarding/b0-delivery.html`。公开文件不包含真实凭据、数据库配置或原始运行日志。

## 最终主代理验收

最终代码统一运行90项，全部通过、0 skip；包含P1a34项和B0新增56项。最终检查临时schema为0；专用实例保持running以供下一批，不自动销毁。检查过所有原P1a文件hash、tracked差异、新增文件底账、Python3.10语法与依赖锁一致性；不是旧业务全量或3.10运行验收。
