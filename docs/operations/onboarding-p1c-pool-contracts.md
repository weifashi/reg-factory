# Pool 任务上下文：纯类型边界

## 本段用途

新增 `onboarding.pool_contracts`，为后续任务、租约服务提供严格内部数据类型。没有 HTTP、页面按钮、任务创建或执行服务；不能用构造成功代替认证、资源占用、权限或扣费批准。

```text
构造 / trusted class validate()
        | 合法形状
        v
返回 None / 保留内部记录  !=  实时认证  !=  租约有效  !=  允许执行
```

- `Platform`：7 个实际声明成员，google/claude/chatgpt/grok/kiro/github/k12；不含 combined。任务表原有 combined 不变，不在本段定义其执行子步骤。
- `PoolLeaseToken`：task_id、resource_kind、resource_id、owner_id、fence、credential_version 六字段。资源只允许 mailbox；owner_id 是本地标签而非身份认证；UUID 必须规范字符串；两个整数必须为正 signed bigint，拒绝 bool。
- `PoolExecutionContext`：actor、task_id、platform、lease 四字段。真实 Actor 的四字段只检查完整形状，不信任 permissions 快照；task_id 必须与 token 一致。平台必须是已声明枚举单例，拒绝绕构造伪造的 exact Platform。
- 构造与 trusted class validate 重检嵌套类型、额外/缺失属性、篡改、旧类型与子类。拒绝时固定 INVALID_INPUT；多余构造参数保留 Python TypeError。repr 不展示任何 ID。
- Frozen 不是安全沙箱。服务调用应使用 `PoolExecutionContext.validate(context)` 而非信任调用者覆写的实例方法；之后仍需实时认证及 lease owner/fence/TTL/current credential 检查。本段没有这些执行功能。

## 页面、数据与兼容性

页面 → 操作 → 结果：本段未接页面，没有新增可点击操作。没有数据库表/字段/DDL、秘密读写、provider 调用或原有 fixture 类型替换。仅新增类型及纯测试，并将该测试登记到 runner 的 offline/all 集合，不提前登记尚不存在的历史仓储模块。

回退可移除这两个新增源码/测试及 runner 的对应登记、显式集合期望；无数据库回滚或云资源清理操作。后续模块若已依赖本类型，必须先核对调用者，不能单独删文件。

## 未决边界

Task5a 历史并发 FAIL 与审批迁移 setup ERROR 未关闭。一次额外遥测未复现超时，不构成稳定性修复。本段通过也不代表任务历史持久层、信用卡池、真实 Google/Vertex/Sub2API 流程或整体上线验收通过。
