# 资源管理入口与四 Tab

用户已批准。保留唯一“资源管理”一级入口，默认邮箱池，内部“邮箱池 / 信用卡池 / 资产 API / 网络出口”。内部路由仍为 mailboxes/cards/assets/network，新增 resources 别名指向 mailboxes，旧 mailpool/mailbox-card/card-pool/network-panel/legacy-tools 等别名不变。

只整合导航与标题，不合并表单、不改变 API、数据与权限，不启用尚未接入的资产/网络旧执行能力。Tab 全部通过原 showView/beforeNavigate：忙碌拦截、未决回执拦截旧工具、邮箱和卡片秘密在离开时清空。原壳不含 pool 片段时展示组件不可用，不调用旧明文池。

- [x] 新测试先验证单入口四 Tab、旧地址、键盘、移动布局及导航阻止。
- [x] console-protected.js/css 新增资源页签；app.js 复用内部导航高亮映射，原离线模式不动。
- [x] 原壳/原模式/邮箱导入/卡池秘密和回执/Google运行回归。
- [x] 本机预览登录只读验收、1440/375 截图，不创建任务或改变现有配置。

无后端、表字段、迁移、权限变更，不提交、推送或重启。
