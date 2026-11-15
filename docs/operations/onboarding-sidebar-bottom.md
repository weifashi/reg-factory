# 侧栏工具区贴底恢复

2026-09-29，用户批准恢复原项目布局。当前分支 fix/onboarding-minors，工作树 /workspace/reg-factory/.worktrees/onboarding-minors。

## 原因与修复

原受保护壳把 `.script-nav` 改为不伸缩、内容全部展开占高，只给 `.sidebar-footer` 设置自动上边距，因此工具区留在任务列表下方，空白出现在工具和状态栏之间。

本次仅修改 `webui/static/console-protected.css` 两条规则：任务列表恢复 flex:1 1 auto、overflow-y:auto、min-height:80px；状态栏 margin-top:0。工具和状态栏不缩小；窗口极矮时保留侧栏整体滚动兜底。未使用绝对定位遮挡内容。

页面 → 折叠任务分类 → 空白归到任务列表区域，工具仍贴底。
页面 → 展开所有分类并滚动 → 只有任务列表滚动，工具及状态栏位置不变。
手机 → 打开侧栏 → 同样保留贴底工具及可滚动任务列表。

资源管理四 Tab、系统配置、其他入口不变。无表字段、接口、权限或业务规则变更，无数据迁移、外部服务调用、提交或推送。

## 证据

目录 `/workspace/gcloud/.local/preview-launch/sidebar-bottom/`。

- red.log：修复前新增几何回归测试在三种尺寸失败；桌面折叠时工具和状态栏之间有 381px 空白。
- 首次测试导航超时，保留 red-navigation-timeout.log；原样重跑后得到上述布局断言失败。
- live.log / browser-live.json：本机实际预览登录后，1440/375 宽度、折叠和展开四种组合通过贴底断言；无页面 JS 异常，无业务写请求。
- 已查看 1440-collapsed.png 和 375-expanded.png；全部截图在同目录。
- 首次预览登录等待超时，见 live-login-timeout.log；未修改产品或放宽等待，原脚本重跑通过。
- 使用本机域名解析访问 TLS 服务，未验证公网代理链；静态文件无需重启即可由现有服务读取。
- 完整五组回归共 87 项：85 项通过，2 项等待超时，见 verified.log；直接重跑两项仍超时，见 recheck.log，不能宣称整套一次全绿。
- diagnose.log 中卡池用例后续通过。配置入口超时时 DOM 显示按钮可见、未禁用且在视口内，无页面异常；截图等待也发生超时，未修改产品或放宽超时掩盖问题。
- compare.log 通过仅替换测试响应中的 CSS 做修改前/当前对照：修改前同样发生入口点击超时，当前 CSS 的原用例通过。该超时并非只出现在本次 CSS 下；具体调度/渲染原因未确定。两项失败用例在当前代码上最终均获得单独通过证据。
- 新增贴底几何测试、资源页签及移动布局回归通过；git diff --check 通过。

## 回退

按 before.css 与现文件差异，仅撤回本次两条 CSS 规则及注释；同步恢复原测试断言。不覆盖其他功能改动，无数据库回退。
