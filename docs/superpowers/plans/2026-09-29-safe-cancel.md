# 任务安全取消实施计划

**Goal:** 用户批准：一个取消入口，对有完整未执行证据的任务原子结束并释放邮箱；其余只请求取消，保留占用。
**Architecture:** 新增内部 `pool_cancellation.py`，调用方持有 task→operator/session→mailbox→platform locks，在同一事务核验历史、锁 lease、CAS 释放并写审计。`pool_commands.cancel` 保留请求键、历史回放及既有鉴权。仅支持当前 synthetic Google pool 创建来源，不为真实 worker 签发停止证明。
**Tech Stack:** Python/psycopg PostgreSQL、原生 JS、unittest/Playwright。

- [x] 测试先行：真实隔离 PG 的成功释放/重新创建、重复请求、旧请求不释放新占用、外部步骤/未知回执/归属漂移拒绝释放、审计失败回滚、无权限、取消与创建竞态。
- [x] 内部核验：版本/代次/状态/步骤；批次创建回执及创建/claim审计；管理变更连续历史；无执行/卡片/其他资源；lease仍是原批次持有且未改变fence/version；Google状态为本任务RESERVED。
- [x] 原子写入：lease清空task/owner/TTL/hold，递增fence/version；平台UNUSED清除last_task，递增version；任务CANCELLED_SAFE/cancel_requested，递增version；审计和幂等回执同事务。任一失败回滚。
- [x] 保守分支：只记录取消，保留邮箱/卡占用；没有原原因码时记录固定取消阻塞原因，不清除已有业务错误。旧成功回执不升级为新取消。
- [x] 页面：安全取消日志与原因中文展示，终态暂停/取消禁用；更新确认及原“不释放”绝对文案；预检失败清除检查中提示。
- [x] 统一运行后端/HTTP/页面回归并记录原始结果；只读检查本机6条任务，不执行批量取消。不提交/推送/迁移；服务重启另需授权。
