# 头部高度恢复 · 2026-09-29

用户批准恢复原项目桌面高度，手机保留自适应。

## 变更与影响
`webui/static/console-protected.css` 将 grid-template-rows:auto 的覆盖从全尺寸移入既有 <=760px 媒体规则。桌面恢复引用原 `style.css` 的 56px 行高，761–900px 保留原 52px；手机保留换行、64px 最小高度、原内边距。不改 logo、字体、按钮大小、颜色或其他布局。不修改业务、权限、数据库字段/API，无迁移、提交、推送或服务重启。

`tests/test_onboarding_unified_ui.py` 新增桌面/平板高度及手机按钮容纳测试；覆盖 900/901、760/761 临界宽度，检查主内容与头部不重叠、页面无横向溢出。

## 验证
- 新测试先在旧实现失败：桌面/平板自动收缩，不符合原固定高度。
- 最终原壳、导入页和布局 25 项检查通过；git diff --check 通过。
- 本机 TLS 登录只读实测宽度 1440/901 时 56px；900/761 时 52px；760 时 64px；375 时 93.296875px；320 时 127.296875px。账号/退出按钮保持在头部内，主内容无重叠，无横向溢出、页面异常或业务写入。桌面/手机截图已检查。未另验公网代理链路。
- 证据目录 `/workspace/gcloud/.local/preview-launch/header-height/`：red.log、verified.log、browser-live.json、header-1440.png、header-375.png。修改前副本 before.css。回滚只移动该 CSS 规则，不整体回退此前功能。
