# 极简网页前端

- 店铺 `/`：默认商品列表，顶部只有店名与“查订单”。点击购买后在弹窗选择同商品数量、填写联系方式和支付方式，再显示订单总额和收款码。数量默认 1，最多 100，且不能超过可用库存。
- 付款弹窗自动查询发卡结果，关闭弹窗不会取消订单。到期隐藏收款码并整单释放预留；已发卡显示全部卡密并可一键复制。
- 查订单只需 QQ、手机号或邮箱，按联系方式精确匹配，分页展示对应订单和已发卡密。联系方式不进入 URL。
- 当前订单查询 token 仅保存在 sessionStorage，作为内部轮询凭证，不要求买家保存。原订单号加 token 查询接口继续兼容旧订单。
- 后台 `/admin` 只保留订单、商品库存、设置。订单按联系方式或订单号搜索，显示单价、数量和总额；点击发卡整单执行，执行时禁用按钮。已发卡优先展示及复制 `card_codes` 数组，兼容旧 `card_code` 字符串。
- 管理员可取消未发卡的待处理及已过期订单，释放整单预留库存；已发卡订单不能取消。重复取消与重复发卡均不会重复操作库存。
- 库存支持查看卡密、状态筛选、批量导入、编辑和删除；仅可用卡密允许修改。
- “设置”包含密码修改表单：当前密码、新密码、确认密码；新密码 12–256 字符。调用 `POST /api/admin/password` 成功后清空表单并返回登录页，当前及其他管理员会话均失效，使用新密码登录。
- HTML/CSS/JavaScript 为本地静态文件，不依赖 CDN 或 npm。动态数据通过 textContent 和 DOM 节点写入，不作为 HTML 插入。
- 管理写入携带 session cookie 与 X-CSRF-Token。订单、卡密请求不缓存；使用 HTTPS，以启用安全 Cookie 和剪贴板。
- 密码不写入浏览器存储或 URL；后端仅持久化密码哈希到私有认证表。数据库清空前必须另存完整备份，普通前端更新不会清空数据或重置密码。

验证：

```bash
node --check static/common.js
node --check static/store.js
node --check static/admin.js
node --test --test-timeout=10000 tests/store.test.js tests/admin.test.js
```

异步回归执行真实 store.js 和 admin.js，验证下单、查询、发卡、编辑和轮询的竞态保护。Playwright 浏览器验收入口为 tests/browser_smoke.cjs；结果和运行方式见项目 VERIFICATION.md。真实扫码付款仍需使用店主自己的收款码验证。
