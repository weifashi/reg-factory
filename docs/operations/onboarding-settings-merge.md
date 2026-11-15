# 系统配置入口合并 · 2026-09-29

用户批准：左侧只保留“系统配置”，页面内使用“运行环境 / Google 开通配置”二级 Tab。

## 页面 → 操作 → 结果
```
系统配置 → Google 开通配置（沿用 #settings 默认落点）
         ↔ 运行环境（原 #env）
切换 Tab → 保留原表单节点和未保存内容，不自动保存、不读取旧 .env
保存 Google 配置 → 原 PUT /api/onboarding/config，新版本规则不变
运行环境 → 原后端未接入，保存按钮继续禁用
```

## 文件与逐项规则影响
- `webui/static/console-protected.js`：不再创建额外“全局配置”菜单，原 env 菜单改为唯一 settings 菜单、文案“系统配置”；原信用卡池入口不变。动态创建共享标题、两枚带 ARIA 属性的 Tab，复用 view-env/global-config 节点。环境和 Google 配置仍使用各自内部视图及接口。补充无 pool 组件时的明确不可用提示，不虚构 Google 表单。环境说明不再暗示已能读取/保存 .env。原受保护编辑器链接保留。
- Tab 点击和键盘左右/Home/End 全部经过原 showView / beforeNavigate；忙碌、会话失效和未决回执保护不绕过。被拦截时保持旧 hash、内容和选中状态。`#env`、`#settings`、`#global-config` 旧地址仍定位对应 Tab，历史前进后退可用。
- `webui/static/app.js`：仅一处高亮判断增加 protected 模式下 env 视图映射到 settings 菜单；离线原模式不受影响。
- `webui/static/console-protected.css`：新增局部设置标题、Tab 及手机布局样式。没有修改此前恢复的头部高度、字体、Logo 或其他业务页。
- `tests/test_onboarding_unified_ui.py`：新增单入口/双 Tab/草稿保留/旧链接/键盘和历史/导航阻止/Google 原 API 保存检查；原 pending 测试改通过内部 env 路由验证阻止，原移动检查加入 env。
- `tests/test_onboarding_console_ui.py`：适配单菜单标签与环境 Tab 入口，保留旧模式、禁止旧请求、会话失败检查。

## 数据、权限和边界
无新表字段、API、迁移或权限变化；没有合并 .env 与任务配置存储，也没有新增秘密读取。真实运行环境后端仍未接入，原不可用状态保留。Google 配置保存仍受 config:manage 与原版本/回执保护。没有实际保存用户配置、修改任务或触发外部服务，没有提交推送或重启。

## 验证和证据
证据目录 `/workspace/gcloud/.local/preview-launch/settings-merge/`。
- red.log：新增菜单文案检查在原“全局配置”上失败。
- final-ui.log：最终代码 59 项原壳/配置/旧模式/Google 运行/布局测试通过；包含原模式依旧可加载旧任务、无未授权旧接口调用、待核对请求不丢失。
- node --check（console-protected.js、app.js）及 git diff --check 通过。
- browser-live.json、live.log：本机 TLS 登录只读实测，一个菜单、两个 Tab，草稿切换保留、旧链接正常，环境保存禁用，零业务写入。1440/375 无横向溢出或页面异常；截图已检查。公网 Coder 代理链路未另验。
- before.js、before.css、before-app.js 为本轮修改前副本。回滚仅撤销本轮差异，不整体重置其他未提交功能。
