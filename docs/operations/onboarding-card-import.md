# 信用卡 CSV：隔离导入解析器

这是信用卡池的**输入模块**，不是信用卡管理页面或支付接口。当前不写数据库、不生成提交令牌、不绑卡、不申请赠金，也不查询余额。

## 模板与调用

在项目根目录用隔离环境执行：

```python
from onboarding.card_import import csv_template, parse_for_import, preview

header = csv_template()  # 仅完整列名和换行，不带任何示例卡资料
# csv_text 由后续受保护界面在内存中传入，不从日志或命令行参数传 PAN。
# result = preview(csv_text)
# parsed = parse_for_import(csv_text)  # 内部使用，禁止 dataclasses.asdict 后返回客户端
```

完整 CSV 表头：

```csv
alias,pan,expiry_month,expiry_year,holder_name,country,address_line1,address_line2,city,region,postal_code,account_limit,sms_channel_ref
```

- 可调整列顺序；列名区分大小写。未知列、重复列、缺少必填列均拒绝，包括 `cvv`、`otp`。
- `address_line2`、`region`、`sms_channel_ref` 可缺列，缺省为空，其他列必须存在。**不会自动生成或补全地址。**
- 当前只接受既有 `pool_secret_types` 的合成资料。其 US 测试地址必须含 `fixture:region`，所以省略 `region` 虽通过 CSV 结构检查，仍不能通过合成资料检查。没有真实数据开关。
- 导入上限为 UTF-8 **262144 字节、1000 条数据记录**，空数据行也计数；允许开头 BOM、CRLF、合法 CSV 引号。字段不自动去空格，空数据行、注释行不能导入。
- 数字必须 ASCII 正整数且无前导零；月份 1–12、账号上限 1–10000；测试有效期年份限定 2099。金额不是导入字段，`>$50` 不作为程序强制条件。
- 别名最多 80 字符且不能为空，短信引用最多 128 字符；都拒绝 Unicode 控制/格式字符及累计 12 个以上十进制数字，避免将卡号误填成非秘密元数据。

## 预览结果与后续操作

```text
CSV -> 解析 + 合成资料检查 -> 脱敏预览
  |       同卡同资料       -> 标重复，保留首条
  |       同卡不同资料     -> 标冲突，禁止整批确认
  |       任意其他错误     -> 修正后重新预览，不能跳过坏行
  +-- 全局大小/数量/语法错误 -> 不保留可导入记录
```

`preview()` 返回 `items / issues / accepted_count / duplicate_count / conflict_count / can_import`。
每条预览只含源行号、别名、卡种、末四位、账号上限和三个布尔完整性标记：`sms_configured / billing_configured / expiry_configured`。不返回完整卡号、有效期年月、持卡人、地址、短信引用原文、秘密引用或哈希。

`can_import=true` 只表示**输入校验通过**，不是数据库已导入、卡可用、支付验证成功或官方试用资格获批。`accepted_count` 是保留供预览的条数；即使非零，有冲突或错误仍为 `can_import=false`。

`ParsedCard` 与 `ParsedCardImport` 的 repr 隐藏内容，但内部对象仍含业务层需要的秘密。调用方不得直接序列化、记录 `vars/asdict`，也不得把输入回显为错误提示；隐藏 repr 不等于内存擦除。

## 固定错误码

| code | 意义 |
|---|---|
| INVALID_INPUT | 非字符串或非法 UTF-8 编码 |
| INPUT_TOO_LARGE | 超过字节上限 |
| TOO_MANY_RECORDS | 超过数据记录上限 |
| INVALID_HEADER | 缺少、重复、未知或大小写不符的列 |
| EMPTY_IMPORT | 没有输入或只有表头 |
| INVALID_CSV | CSV 结构无法安全解析 |
| INVALID_RECORD | 列数、元数据或数字语法不合规 |
| SYNTHETIC_DATA_REQUIRED | 资料不符合现有隔离测试白名单 |
| DUPLICATE_CARD | 同批同卡且所有资料相同；保留首条 |
| CONFLICTING_CARD | 同批同卡但资料不同；整批不可确认 |

错误仅含 `line/code`；行错误报告该记录开始的物理行号，全局错误为第 1 行，不带原输入或底层异常。

## 尚未接入

本模块没有增加或迁移任何表字段，也未修改邮箱业务、代理或现有绑卡流程。数据库重复校验、owner 鉴权、HMAC 预览令牌、事务回执、额度占用、API 与一级页面需在后续卡池管理闭环实现，不能仅凭此模块声明上线可用。
