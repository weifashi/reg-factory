# 删除联系支持入口 · 2026-09-29

用户要求删除“联系支持”。本次从原 index.html 移除侧栏按钮、对应 Telegram 联系二维码弹窗及外链；app.js 删除对应事件绑定，避免访问不存在节点造成页面启动失败。未删除二维码文件、通用样式，也未修改任何业务接码渠道或 Telegram 业务功能。

无接口、表字段、权限、任务及配置数据变更。资源管理四 Tab 方案未在本次顺带实施。

新增界面断言先以原按钮仍存在失败。最终产品代码下统一壳 26 项通过；原控制台测试初轮仅旧资源清单仍要求加载支持二维码失败，更新为不再请求该资源后原控制台 11 项完整重跑通过。JS 语法和 git diff --check 通过。本机 TLS 登录只读检查确认入口/弹窗数量均为零，无二维码请求，无页面错误，16 项原任务入口保留；截图已查看，公网代理链路未另验。

证据 `/workspace/gcloud/.local/preview-launch/remove-support/`：red.log、verified.log（含首次资源清单预期失败）、console-final.log、browser-live.json、desktop.png。before.html/before.js 为本轮前副本；回滚仅恢复这组入口/弹窗与绑定。没有提交、推送、迁移或重启。
