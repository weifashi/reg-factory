# 资源管理四 Tab 合并验收

日期：2026-09-29。工作树：`/workspace/reg-factory/.worktrees/onboarding-minors`，分支 `fix/onboarding-minors`。

## 页面 → 操作 → 结果

```text
资源管理
  +-- 邮箱池     默认打开，保留列表、导入等现有操作
  +-- 信用卡池   保留现有卡片列表、新增及导入
  +-- 资产 API   保留原页面及未接入限制
  +-- 网络出口   保留原页面及未接入限制
```

侧栏四个入口合并为一个，Tab 点击切换原页面，不另建独立业务系统。手机端四个 Tab 分两行。支持左右方向键及 Home/End。其他任务页面隐藏资源标题和 Tab。旧 mailpool、card-pool、network-panel、legacy-tools 等地址继续生效；resources 默认进入 mailboxes。

## 文件与规则影响

- `webui/static/console-protected.js`：构建统一入口、标题与页签，保留原面板节点、导航拦截和路由别名；缺少组件时只显示不可用，不转入旧明文邮箱池。
- `webui/static/console-protected.css`：复用系统配置的页签样式，增加手机两列布局。
- `webui/static/app.js`：资源子页统一高亮资源管理入口；原非保护模式导航不变。
- `tests/test_onboarding_unified_ui.py`：新增六项资源整合测试，并更新旧导航选择器。
- `tests/test_onboarding_console_ui.py`、`tests/test_onboarding_card_ui.py`：更新入口定位，原权限、秘密清理和未决回执断言保留。

无数据库表或字段改动，无 API、授权、数据迁移或真实执行能力变更。邮箱及卡片敏感输入离开时仍清空；忙碌时禁止切换；待核对回执仍阻止进入不允许的旧工具。不放开真实绑卡、支付或模型调用。未接入能力不会因合并导航而变为可用。

## 验证证据

证据目录：`/workspace/gcloud/.local/preview-launch/resources-merge/`。

- 五组 UI 回归：86 项通过，耗时 191.743 秒，退出码 0，见 `final-ui.log`。
- 两份修改后的 JS 均通过 `node --check`；`git diff --check` 通过。
- 本机 TLS 浏览器登录验证：单入口、四 Tab、旧链接、导入文本离开清空、资产及网络原限制均通过；1440/375 宽度无页面横向溢出，无 JS 异常，无业务写请求。见 `browser-live.json` 和 `live-final.log`。
- 已查看桌面邮箱页、手机信用卡页截图；所有四页两种宽度截图均保存在证据目录。
- 首次浏览器脚本在卡片自动加载期间立即切换，被原忙碌保护拦截；脚本改为等待加载空闲，未修改产品保护规则。第二次登录等待超时，原因未确定，原日志保留。第三次相同调整后脚本完整通过。
- 公网 Coder 代理链未单独验证；浏览器使用本机域名解析访问本机 TLS 服务。静态文件由现有服务读取，未重启、提交、推送或部署。

## 回退边界

证据目录的 before.js、before.css、before-app.js 为本次修改前快照。仅撤销本次导航整合差异；不得覆盖已有其他工作。测试选择器和本次新增测试同步还原；数据库无需回滚。
