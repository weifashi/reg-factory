# 外站导航误拦截修复（2026-09-28）

## 问题和范围
用户从外站进入 Coder 预览后得到 FORBIDDEN。实际诊断确认 HTTPS、Host 正确，无重复安全头/Cookie；请求携带 cross-site 和外站 Referer。旧 `_transport` 在认证之前一律拒绝这种正常顶层导航。

本修复写在原 reg-factory 的 `webui/onboarding_auth.py`；使用既有隔离工作区，不创建另一个产品服务或存储。主目录尚未合并，本轮无提交/推送/生产发布。

## 页面 → 操作 → 结果
```text
外站链接 → GET 管理页 → 固定 303 /login → 静态登录页
                                               |
                              同源 bootstrap + 登录提交
                                               |
                                     原认证、权限 → 控制面
```
只覆盖 `/login`、`/`、`/onboarding`、`/onboarding/pools`；要求真实 HTTPS、精确 Host、无 Origin、GET、Accept 包含 text/html、Fetch Site 为 same-site/cross-site、Mode=navigate、Dest=document。外站进入管理页无论是否带会话 Cookie，一律先到登录页，不读会话、不执行业务、不修改 Cookie。

原规则“拒绝所有外站来源请求”收窄为“拒绝所有外站 API/提交/资源请求，但固定 HTML 页面允许安全登录落地”。外站 Referer 仅这一导航例外接受 HTTP(S) 且合法主机/端口、不含用户凭据。非法来源、HTTP、错误 Host、编码/反斜杠路径、重复安全头/受保护 Cookie 仍拒绝。参与导航判断的 Accept/Fetch Mode/Fetch Dest 新增重复头拒绝。

不允许外站 bootstrap、session、邮箱/任务 API、旧业务接口、脚本、iframe、fetch、HEAD 或写请求。固定目标不采用 next/return_to/Referer；权限、CSRF、限流、Secure/HttpOnly/SameSite Cookie、CSP、no-store/no-referrer 均保留。未 ready 仍503；off模式不安装保护层的旧行为不变。

## 数据和现有业务影响
- 新增/修改表与字段：0；迁移：0。
- 修复本身不读写数据库；用户随后真正登录仍按旧流程产生会话/审计。
- 不放开旧代理、邮箱文件、短信、执行器或付费调用。
- 不代表 Coder Public sharing 已配置，也不代表真实 Google/Sub2API 链路已上线。

## 验证和回滚
新 `ExternalNavigationTests` 并入原 route_guard 模块，由原 Web suite 自动发现。覆盖外站跳转链、Cookie无副作用、API/资源拒绝、精确导航元数据、Host/TLS/raw_path、重复头/Cookie、非GET/未ready和同源认证回归。实际浏览器还须跑外站链接→登录→邮箱池。

证据保存在本机私有运行目录 `/workspace/gcloud/.local/preview-launch/navigation-fix/`；公网用户链路与本机模拟 Coder 域名链路分别报告，不能混称。
回滚先用预览的 PID+启动标识核验脚本停服，只回退本修复的 auth/test/doc 差异，不 reset 工作树、不清空数据库或密钥、不切回匿名模式。再按原受保护启动配置启动。旧代码会恢复外站导航403，直接新标签地址栏访问登录页仍可用。

### 本轮已执行结果
- 新增8个导航测试先红（25个子场景失败），修复后 Guard/AuthRouter/Navigation 共27项通过。
- 完整 Web suite：215项，0失败/错误/跳过，进程exit0；包含真实隔离数据库与浏览器DOM回归。
- 两种外部来源（cross-site、same-site）实际本机Chromium导航、登录和邮箱API链路均通过，0 JS错误、0横向溢出。浏览器观测脚本首轮因未剥离API query而漏记请求，保留失败记录后修正观测，不修改产品来绕过断言。
- 真实TLS负向检查：错误Host、错误Origin、无CSRF、旧执行和未知路由均拒绝。
- 预览已重启；经过Coder的公网用户链路仍待用户确认，Coder共享权限没有变更。原项目入口/数据/执行器整合仅完成调查，不算实现。
