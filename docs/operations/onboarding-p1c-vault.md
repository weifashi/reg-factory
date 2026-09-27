# P1c Task3a：合成秘密只写入层

2026-09-24。本页说明当前实现与操作边界；最终测试结论由独立verification记录给出，不在此预填“全部完成”。

## 做了什么 / 没做什么

```text
合成typed输入 -> 私有测试目标核验 -> DB会话/权限/归属
             -> AES-GCM密文 + 同事务审计 -> SecretRef
             -> 调用方绑定资源并提交（本层不替它提交）

没有：公开解密、下载、consumer、HTTP路由、新页面、真实生产输入
```

新增 `pool_secret_types.py`、`pool_vault.py`、`request_mac.py`；复用既有SecretStore的纯AAD编码、Keyring私有读取、DB认证、UoW与审计。没有改001/002，没有新增或修改表字段，没有放宽原SecretStore/FixtureConsumer/下载接口。当前页面上没有新增“导入真卡/读取密码”按钮。

## 精确六种payload

均frozen、隐藏repr，拒dict/子类/未知字段/伪造缺字段；写入时再次验证而非只依赖构造校验。

| 类型 | 字段 | 允许范围 / 权限 |
|---|---|---|
| MailboxCredential | **email_norm**、password、refresh_token、client_id、provider、mail_api_url、mail_api_key、two_factor | canonical fixture.invalid及子域；秘密为空或fixture前缀；client_id/URL/TOTP固定测试值；mailboxes:manage |
| PlatformCredential | **email_norm**、platform、account_password | 同合成域；固定平台枚举；独立平台口令不回退邮箱密码；mailboxes:manage |
| Pan | value | 仅两张固定测试卡号；cards:manage |
| BillingHolder | name | 固定fixture:holder；cards:manage |
| BillingAddress | country、line1、line2、city、region、postal_code | US及固定fixture地址字段；cards:manage |
| CardExpiry | month、year | 月1–12、年2099，仅合成范围，不证明有效期/付款资格；cards:manage |

两个email_norm是经root确认补充的输入绑定字段，不自动修改或猜测平台身份。没有CVV/一次性OTP/Google凭据/SA JSON类型。格式门禁不能证明字符串来自何处；它不授权真实资料输入，更不是生产provider。

## 目标、权限和归属

`SyntheticPoolPolicy.from_settings(settings)`要求既有严格测试Settings，额外限制manifest临时schema。每次put重验私有manifest的marker/schema/token/owner；不接受生产目标或现有rf_onboarding作为此policy。

在业务SQL前核对真实连接socket/port/database/current_user、数据库marker、schema及owner、精确search_path、没有temp schema、已装受审002前缀。仅把正确Settings传入，不足以让错误连接通过。

锁序为operator/session→资源；DB重验权限、disabled、auth_epoch、session撤销与absolute/idle期限。已有mailbox/card锁行再核owner；platform经父mailbox核owner、email、platform并重新确认引用关系。**billing_identities是app只读/追加表**，因此读取owner而不要求FOR UPDATE所需UPDATE权限；不可描述为所有资源都锁行。

新权限mailboxes:manage必须显式授予；不自动给旧操作者全部权限。FixtureActor不被接受；keys:download/config:manage不能替代池写权限。

## 加密与MAC隔离

AES-GCM AAD绑定schema、secret UUID、实际kind、revision；nonce随机且DB唯一约束兜底。访问策略只服务端生成并绑定actor/resource，客户端不能传任意kind/access_policy。

Keyring仍只原私有fixture目录、32-byte版本文件、descriptor NOFOLLOW/owner/0600/单链接检查，无新生产路径、无环境fallback、无自动生成或覆盖key。

新PoolVault拒绝把`request-mac.key`作为AES active version；Vault每次及末尾重读当前AES/MAC，拒同材料。RequestMac自身有界检查目录：最多128 entries、最多64个加密候选版本；拒危险路径、链接、同材料，超过固定拒绝，不无限扫描。request MAC绑定action+owner+规范DTO；PAN指纹用独立domain且不绑定owner，避免一张卡按owner拆成多份额度。无裸密码/PAN SHA用于请求幂等。

上述强制隔离限定新路径；原Keyring仍可被同OS可信Python代码显式构造成该版本，未发现当前Web/boot可达配置路径。本层不声称阻止同OS任意Python代码读取或复用密钥。

资源锁、审计锁或最后key文件读取跨过会话期限，都须在返回前再次DB校验；失败由外层事务回滚。这里没有明文交付，测试中的解密只用合成key验证AAD，不是产品接口。

## SecretRef不是提交收据

`put_locked(conn,actor,resource,payload)`只在调用方已有事务中INSERT secret_objects与audit_events，返回`SecretRef(id,revision)`。调用方仍须同事务INSERT/更新资源引用并提交；不得先把ref当导入成功返回给HTTP。新建UUID尚无资源行时，policy仍绑定当前actor+UUID；孤立密文不代表已创建邮箱/卡。

已覆盖同事务资源INSERT成功及真实CHECK失败：失败时secret/audit/resource全部回滚。恶意owner测试用跨事务fixture故意造不一致归属，仅是安全负例，不是合法创建流程。无任何新资源CRUD/API已经因此完成的承诺。

## 回退与后续门禁

- 当前没有新路由/页面开关需要关闭；回退应停用新增调用方并恢复**本切片指定文件**，保持原B1/B2接口，不删除整个未提交工作树或其他代理/用户改动。
- 不删除已有key、request MAC key、secret对象、审计或002账本；丢key会使已有密文不可恢复。密钥恢复/轮换须明确范围与备份证据，不自动重建替代。
- 故障/COMMIT_UNKNOWN先核对实际事务结果，不重复发送不明操作；本层返回ref之前发生错误不能当成功。
- 下一步3b仍需真实pool task/lease、双凭据pin、fence/TTL、用途与固定合成consumer的COMMIT后交付闭环；PAN/Billing仍无消费权限，不用FixtureActor或任意callback绕依赖。
- 生产OS身份/密钥provider、完整资源服务与所有旧consumer切换、无人值守身份、真实资料授权及官方接口/付款资格均独立审查。合成通过不等于可运行Google/支付/Sub2API或可以部署。
