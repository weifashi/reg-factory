# P0A / P1a 首批交付验收

日期：2026-09-24。结论：本批离线规则实现及对应测试通过；尚未接入页面、数据库或真实外部服务。

## 1. 基线、代码位置与范围

- 分支：`feature/gcloud-p1a`；工作树：`/workspace/reg-factory/.worktrees/gcloud-p1a`。
- 基线/HEAD：`b0758484c2401ea38792e18711ff38ef8a24105c`（未提交）。主树仍是main，已有业务源码未改。
- 已执行批准计划 P0A、P1a Tasks1–5；本记录替代原计划中的待执行状态，但不改写计划历史。
- 三个实现代理分别负责domain、allocation、gates；独立规格审查全部SPEC_PASS，后续独立质量审查QUALITY_PASS；影响审查核实无现有生产调用。
- 只新增4个Python模块（含空包入口）、3个测试、2个验证文档。所有文件仍未跟踪/未暂存；不能只看空git diff误认为没改。
- 本地Git `.git/info/exclude` 增加 `/.worktrees/` 用于隔离目录，不改版本化.gitignore；未提交/推送/合并/部署。

## 2. 实际实现

| 功能 | 实际行为 | 尚未实现 |
|---|---|---|
| 路由判断 | 未授权阻塞、普通步骤可自动、适用验证码渠道可尝试、未知转人工、权限策略转管理员 | 官方页面识别、登录/注册、自动收码与接管UI |
| 绑卡结果判断 | 确认成功/明确失败/未知/矛盾分开；取消只有明确未发送才可返回释放 | 真实绑卡、容量账本、事务锁、对账器 |
| 选卡规则 | 最少成功关联优先，排除忙/有预留/满额/禁用/资料不全卡，稳定排序 | DB预留、银行卡池页面、真实支付数据 |
| 测试/启用门禁 | 独立action、资源/配置匹配、有效期、暂停取消、隔离/合同核验及状态判断 | 审批持久化/原子消费、Sub2API调用、真正启用调度 |

返回AUTO不是已经执行；返回LINKED不代表真实卡已绑定；allow_test/allow_enable=True只允许未来进入事务重检，不产生费用、不启用调度。

## 3. 字段与原有业务规则

本批DB表/字段/索引/迁移变更数均为0。3个冻结dataclass的24个属性是内存快照，不是数据库字段：

- CardView：card_id、linked、reserved、account_limit、last_assigned、busy、enabled、complete。
- ApprovalView：action、task_id、resource_revision、config_revision、expires_at、consumed、revoked。
- ExecutionView：task_id、resource_revision、config_revision、paused、cancelled、isolation_verified、contract_verified、verification、scheduling。

原规则影响：新增包目前只被新增测试import，未接入webui/common/旧注册脚本；既有邮箱全局排除/售卖资格、代理轮换、旧上传重试均未改。后续改造这些规则须按P1b/P1c/P2–P4执行，不用本批单测背书。

### 逐行审查分类

| 文件 | 行段 | 分类与含义 |
|---|---|---|
| onboarding/__init__.py | 0行 | 空入口，无规则影响、无副作用 |
| onboarding/domain.py | 1–6、13–14、28–29、36–37、59–60 | 文档/import/留白，无业务规则变化 |
| onboarding/domain.py | 7–12、15–27 | 新增Route状态及分支；不更改原业务调用 |
| onboarding/domain.py | 30–35、38–58、61–65 | 新增绑定状态/确认与未知/取消判断；不写账本 |
| onboarding/allocation.py | 1–6、17、31–32 | 文档/import/留白，无规则影响 |
| onboarding/allocation.py | 7–16、18–30、33–48 | 冻结快照、整数/容量校验、过滤/排序，无持久化 |
| onboarding/gates.py | 1–14、24–25、37–38、65–66、72–73 | 文档/import/留白；明确可信输入与非事务边界 |
| onboarding/gates.py | 15–23、26–36 | 审批/执行快照及失败关闭默认值 |
| onboarding/gates.py | 39–64 | 审批绑定/UTC有效期/运行状态/隔离检查 |
| onboarding/gates.py | 67–71、74–78 | 一次测试与独立启用的不同前提 |
| tests/test_onboarding_domain.py | 1–191 | 11个测试与支撑代码，验证规则，不改变业务 |
| tests/test_onboarding_allocation.py | 1–87 | 9个测试与支撑代码，验证规则，不改变业务 |
| tests/test_onboarding_gates.py | 1–207 | 14个测试与自包含时区fixture，不改变业务 |
| docs/verification/onboarding-baseline.md | 全文 | P0A事实与限制，无业务规则影响 |
| docs/verification/onboarding-p1a.md | 全文 | 本交付记录，无业务规则影响 |

## 4. 当前页面 → 操作 → 结果

当前邮箱池仍为“批量导入邮箱池”文本框页面：粘贴→点击“导入到邮箱池”→原 `/api/mailpool` 解析/去重/写原邮箱文件并返回数量。代码未改，本批不会自动创建Google任务、分配卡或触发费用。

原任务页“运行任务”仍走原有API/子进程/日志链。新计划中的统一邮箱列表、信用卡池和审批按钮尚未接入，不绘制成已经可用的生产页面。这里依据现有代码核对，未执行真实导入或注册。

## 5. RED / GREEN 与修复

证据目录 `/workspace/gcloud/.local/rf-p1a/`：

| 任务 | RED | GREEN |
|---|---|---|
| 路由 | domain-route-red.txt：缺domain接口，退出1 | domain-route-green.txt：5项，退出0 |
| 绑定 | domain-binding-red.txt：原5通过，新6缺BindingState，退出1 | domain-binding-green.txt：11项，退出0 |
| 选卡 | allocation-red.txt：模块缺失；allocation-type-red.txt：统一ValueError的24子用例失败 | allocation-green.txt：9项，退出0 |
| 门禁 | gates-red.txt：接口缺失，退出1 | gates-green.txt：14项，退出0 |
| DST回归 | gates-dst-red.txt：内存变体去掉UTC转换，双向反例失败 | 实际产品实现UTC比较，最终通过 |
| 时区数据依赖 | gates-tzdata-red.txt：禁用系统tzdata时2个错误 | gates-tzdata-green.txt：用自包含重复小时fixture，14项通过 |

质量审查发现的唯一Important：初版测试使用ZoneInfo依赖环境IANA数据；已移除该依赖，保留两个DST方向断言，没有skip或新增依赖。最终产品门禁依旧只使用标准库datetime，按UTC绝对时刻比较过期。

## 6. 主代理最终验证

从目标隔离工作树执行（不是gcloud的同名模块）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONTZPATH='' python3 -S -m unittest discover -s tests -p 'test_onboarding_*.py' -v
```

原始 `root-final-suite.txt`：**34项（domain11 + allocation9 + gates14），OK，退出0**。规划23方法，加了必要边界后实际34，以真实输出为准。

`root-offline-audit.txt`：同样34项通过；Python audit hook禁止socket/新子进程事件，本次禁止事件0；确认加载4个模块均来自目标工作树，不是旧gcloud包。业务模块AST import仅enum/typing/dataclasses/datetime，7个新Python文件均通过Python3.10语法解析。只证明本批测试路径无这些副作用，不代表未来适配器网络隔离已完成。

实际解释器Python3.12.3。没有实际运行Python3.10或Windows，不将AST检查称兼容全验收。

`git diff --check`及新增文件逐个no-index空白检查；新增文件完整检查以status/ls-files底账为准。现有webui/common/config/requirements相对基线无差异。

## 7. 未通过、未验证与下一阶段

- 旧 `test_account_records.py` 基线因缺Playwright导入失败（退出1），没有执行其业务测试；原始p0-baseline.txt保留。未安装依赖，旧业务完整回归没有通过声明。
- 无DB连接/迁移、多进程锁、原子审批消费、服务端鉴权、秘密库、页面接入。
- 无真实邮件/短信/Google/Billing/gcloud/Sub2API合同验收，无真实付费请求或调度启用。
- 可信typed输入由后续服务端构建；本批不是通用不可信JSON输入验证器，版本引用也不是自行核验过的外部证据。
- 下一阶段先细化P1b持久层/安全/事务详细计划；新增依赖安装、隔离测试库和迁移对象需明确，不能复用旧DEMO真实数据。

## 8. 保留与回滚

工作保留在上述分支/隔离工作树，未合并main。无需数据库或外部资源回滚，因为没有创建这些资源。撤回时仅按实际9文件底账移除本批文件；先核对用户新增改动，不用 `git reset --hard`、`git clean -fd` 或强制删除工作树。删除分支/工作树或调整本地exclude规则都需确认，不自动清理。

HTML说明页位于 `/home/weifashi/www/gcloud_onboarding/p1a-delivery.html`，只公开脱敏设计/测试结论，不放真实凭据或原始业务日志。
