# 临时 Sub2API 原子停用导入实施计划

> 使用 subagent-driven-development，TDD，规格和安全复审。用户已明确确认官方承诺及临时实例修改授权，不再重复询问。不得提交、推送、改正式实例或触发模型费用。

**Goal:** 本机临时 Sub2API 完成官方承诺确认，创建专用 Gemini 分组，支持账号初次写入即停用且不可调度。

**Architecture:** 固定官方 v0.2.9 基线，新增显式可选创建停用参数，省略时保留原行为；请求 DTO→服务输入→首次 repository.Create 前设定 inactive + schedulable=false。按任务显式传入分组，不依赖默认分组。不是先创建启用再关闭。无新数据库字段/迁移，不跳过原安全校验；失败未知先核对，不重发。

**Tech Stack:** 官方 Go 源码、现有 PostgreSQL/Redis，本机管理 HTTP API；不连接 Google 模型。

## 授权与边界
- 临时实例唯一地址 http://127.0.0.1:8800，目录 /workspace/reg-factory/.local/sub2api-temp-20260929。
- 先核对 runtime PID/start_ticks/exe/cwd；不输出凭据。
- 官方承诺必须经现有 accept 接口提交且读回确认，不写 DB 伪造。
- 只在空账号、零API Key、零用量的临时实例做测试；不能用真实Google凭据验证。
- 现有账号和普通创建默认行为不变；保留上游制品备份与清晰回滚命令。
- 首次禁用解决调度窗口，不声称顺带修复上游跨表事务或未知结果重试语义。

## 步骤
- [x] 根代理：核验实例指纹，提交已获用户授权的承诺并读回，创建/复用精确匹配 Gemini 分组，运行现有探针。
- [x] 研究代理：确认最小 DTO/service/repository 传播链、编译需求、测试方式。
- [x] 实现代理：先失败测试，再最小 patch；覆盖停用首次写入、省略默认、批量入口传播、分组失败仍不可调度，不改原权限/校验。
- [x] 独立规格审查及安全复审；root 复跑受影响测试，构建嵌入前端的真实二进制。
- [x] 本机备份、指纹确认、仅替换应用，健康/登录/合规/分组 smoke，人工造的无上游权限测试材料验证停用并清理，读回零调度/零调用。
- [x] 文档记录patch文件/表字段/业务变化/测试/制品hash/回滚；原reg-factory预览和业务库不动。

## 验收限制
本切片不是整个Google流程上线。真实邮箱消费、worker、Google结算、专用项目/Vertex/SA创建和收费调用仍需后续独立验收。若工具链、兼容或安全测试阻塞，保留原二进制运行，不部署未验证patch。

## 已核验事实

2026-09-29 用户明确确认后，官方accept接口成功且读回required=false；创建专用gemini分组regfactory-gemini，ID=2。现有真实探针返回READY且execution_connected=false（仅管理连接）。证据位于临时实例evidence/authorized-init.json与probe-after-consent.json。

账号停用应使用既有inactive，不能使用其他资源的StatusDisabled：账号编辑UI会把未知disabled回退active，有保存时误启用风险。工具链Go1.27.0已成功下载，不降级go.mod。

## 本切片执行结果

补丁已部署到唯一临时实例，健康、前端200、匿名管理401、管理员连接均核验。root独立46顶层+25子场景通过；repo包仅编译未匹配所选测试，不把它记成repository单测通过。另有真实API+PostgreSQL读取核验单/批量停用创建，2个无效测试账号已逻辑删除，可调度0、APIkey0、用量0。此计划全部完成仅指本切片；总项目仍未上线。
