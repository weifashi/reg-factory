# 本机临时 Sub2API 兼容补丁

这是 reg-factory 对临时下游实例的可审查补丁，不是第二套业务项目，也不会自动改正式 Sub2API。

基线：官方 v0.2.9，commit `4c00df2e0183e2c70b7fa8ba45914205e36aad0c`。文件及差异 SHA256 见 `patches/0001-create-disabled.manifest.json`。在**另外准备的精确基线源码副本**根目录执行：

```sh
git apply --check /path/to/reg-factory/integrations/sub2api/patches/0001-create-disabled.patch
git apply /path/to/reg-factory/integrations/sub2api/patches/0001-create-disabled.patch
```

不能对未知版本盲套，也不自动下载、编译、迁移或重启服务。

## 新接口参数

单笔 `POST /api/v1/admin/accounts` 或批量 `/accounts/batch` 的每个账号可显式传 `create_disabled: true`。首次写入即为 `status="inactive"`、`schedulable=false`。省略或false保留上游默认active/true；省略字段的旧请求JSON及幂等指纹保持兼容。此处inactive是原账号UI/修改API的状态，不用其他资源的disabled。

没有新增表、字段、索引或迁移。账号已有的status和schedulable在同一个INSERT写入，不靠事后UPDATE。组绑定仍遵循上游行为；绑定失败可能留下停用账号，不声称整个多表流程事务化。网络或提交结果未知时先核对，不重发创建。

**未打补丁的官方0.2.9可能忽略未知字段并创建启用账号。** 因此只对已核实制品哈希及合同的实例调用，不以HTTP200或返回账号ID代替状态验收。当前reg-factory真实worker尚未因此接入；现有配置没有被改成真实执行。

## 验证方式

要求Go1.27.0，不降低go.mod。单测使用unit标签、内存stub/httptest；不跑需要真实上游凭据的live测试。完整记录见 `docs/operations/onboarding-sub2api-disabled-import.md`（相对项目根）。界面未修改；原账号停用显示沿用现有组件。
