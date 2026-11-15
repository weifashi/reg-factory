# 信用卡池：合成资料持久化服务

> 2026-09-28 更新：列表、手动/CSV 导入、管理信息编辑、启停及受保护 HTTP 已接入同一原项目页面，见 [卡池管理说明](onboarding-card-management.md)。以下说明仍只描述底层导入服务。

本模块把卡 CSV 的合成资料写入**本机隔离 PostgreSQL**，不操作 Google、银行或真实银行卡。它不是完整的卡池管理页面，也不执行卡分配、绑卡、赠金申请或模型调用。

## 服务调用

受信应用装配现有 `Settings / Actor / PoolVault / RequestMac`；它们不能由 HTTP 参数指定：

```python
from onboarding import cards

# csv_text 为内存里的合成 CSV，不写日志、不放命令行参数。
view = cards.preview_import(settings, actor, csv_text, vault=vault, mac=mac)
# 显示脱敏预览并由操作者确认；只有非空 preview_digest 才能进入提交。
result = cards.import_csv(settings, actor, csv_text, view['preview_digest'],
                         request_key, vault=vault, mac=mac)
```

两个入口都重新核验 `cards:manage`、当前会话和隔离策略，不相信缓存的权限。预览只读；`preview_digest` 绑定 owner、隔离 schema 与全部规范业务字段，不是授予权限的凭证。BOM、表头顺序、换行形式和完全重复行不改变规范请求；实际字段变化必须重新预览。

结果只含 `created_ids / skipped_ids / request_key`。不返回 PAN、指纹、秘密引用、地址或有效期；导入模式为 **skip**，不是更新：同卡同 owner 已存在时不覆盖资料，跨 owner 则整批拒绝。更新功能需另走显式版本校验，不能靠重复导入改资料。

## 真实数据写入

```text
CSV -> 校验 -> 脱敏预览 -> 确认（稳定 request_key）
                           |
                           v
                    同 owner 串行化锁
                           |
             +-------------+----------------+
             |                              |
       已有同键回执                    首次导入事务
       验证后返回原结果                4 秘密 + 身份 + 卡
             |                         + 回执 + 审计
             +-------------+----------------+
                           |
                 重新核验会话/策略/密钥
                           |
                     COMMIT 后才返回
```

本轮复用现有表，**不新增表或字段、不修改已应用迁移**：

| 表 | 新卡导入写入内容 |
|---|---|
| `secret_objects` | 四条加密对象：PAN、有效期、持卡人、账单地址，分别绑定 card/billing 资源 |
| `billing_identities` | owner、持卡人/地址秘密引用、国家；验证状态仍为 `UNVERIFIED` |
| `payment_cards` | owner、别名、卡种、末四位、三类引用、HMAC 指纹、账号上限、短信渠道引用；默认启用、关联数和预留数为 0 |
| `operation_receipts` | owner 范围的 `card.import` 成功回执、请求摘要、原结果 IDs |
| `audit_events` | 每个秘密的 `secret.put` 和整批 `card.import`；无卡资料原文 |

新建一张卡：4 秘密 + 1 账单身份 + 1 卡 + 1 回执 + 5 审计；双卡共用同一批次回执与导入审计。只跳过已有卡的新请求仍有一条回执和导入审计；同键重放不重复写入。不会写 `card_reservations/card_account_links`，不会增加成功关联或预留数量。

## 原子性与未知结果

- 任意输入错误、跨 owner 卡、权限失效、审计失败或密钥/策略变化，整批回滚；不保留半套新卡和孤立秘密。
- 同 owner 的两个会话通过固定事务级 advisory 锁串行化；跨 owner 的卡号唯一性由 HMAC 指纹与数据库唯一约束保证。不把互斥锁当官方付款确认。
- 同键、同规范请求返回原结果；同键不同请求返回 `IDEMPOTENCY_CONFLICT`，损坏回执为 `DEPENDENCY_UNAVAILABLE`，都不能自动换键再写。
- `COMMIT_UNKNOWN` 表示提交结果未知，**只允许保留原 CSV、摘要和请求键进行核对/重放**，不得因网络错误自动重建请求。不要将 CSV 存在浏览器 storage 或日志中。
- 新卡服务没有 `not_committed=true` 承诺，不扩展既有五处证明点；页面不能套用旧路由的解除未决规则。

## 仍未交付

当前只允许既有固定合成资料，不支持真实 PAN。持久化不代表卡可用、银行验证通过或获得 Google 官方赠金。卡列表/管理信息编辑、一级页面、受保护 HTTP 已由卡池管理模块接入；真实资料存储、秘密资料修订、资源预留/结果观察与真实外部集成仍需后续实现和单独验收。生产密钥/权限/备份恢复与费用批准不在这次服务验证中完成。
