# 极简发卡小店 API

金额使用整数分，时间使用 Unix 秒。同商品每单可购买 1–100 张卡。JSON 错误格式为 `{error:{code,message}}`。订单和卡密响应禁止缓存。

## 买家

| 接口 | 请求 | 返回 |
| --- | --- | --- |
| `GET /api/products` | — | `{products,shop_name,channels,order_ttl_seconds,support_notice,auto_delivery:false}` |
| `POST /api/orders` | `{product_id,channel,contact,quantity?}` | 201 `{order,token}` |
| `POST /api/orders/lookup` | `{contact,offset:0}` | `{orders,total,has_more}`，每页 50 条，最新在前 |
| `GET /api/orders/<id>` | `Authorization: Bearer <token>` | `{order}`，兼容原查询凭证 |
| `POST /api/orders/<id>/cancel` | 同上 | `{order}`，仅可取消待处理订单 |
| `GET /qr/<channel>` | — | 对应的个人收款码图片 |

- `channel` 为 `alipay` 或 `wechat`，仅上传了收款码的渠道可下单。
- `quantity` 缺省为 `1`，必须是 1–100 的整数；字符串、小数和布尔值均不接受。可用库存必须足够整单数量，单价以服务器当前商品价格为准。
- `contact` 必填，长度为 3–128 字符，支持 QQ、手机号和邮箱；去首尾空格，邮箱统一大小写。联系方式精确匹配，不支持模糊查询，不放入 URL。查询接口每 IP 每分钟最多 30 次，超限返回 429。
- `token` 保留用于当前浏览器内部轮询和兼容原接口，买家不需要主动记住它。联系方式查询直接返回订单及已发卡密，不返回原查询 token。
- `order` 包含 `{id,product_name,channel,unit_price_cents,quantity,base_price_cents,payment_adjustment_cents,price_cents,contact,status,created_at,expires_at,paid_at,qr_url,delivery_mode,card_codes?,card_code?}`。`unit_price_cents` 是下单时单价，`base_price_cents` 为单价乘数量，`payment_adjustment_cents` 为金额区分加价（新订单 0–10 分），`price_cents` 为实际应付金额，即前两项金额之和。订单生成后金额固定，前端付款展示和人工核对均使用 `price_cents`。
- `status` 为 `pending`、`paid`、`expired`、`cancelled`。只有 `paid` 返回 `card_codes` 数组及兼容字段 `card_code`；`card_code` 为全部卡密以换行连接，单张订单与原接口保持一致。新订单 `delivery_mode` 为 `manual`，历史订单保留原交付记录。
- 未配置或已下架的商品、缺货的商品不能下单。下单按数量预留库存，默认 1800 秒，超时或取消整单释放预留。同渠道跨商品检查所有待付款实际金额，在商品合计至商品合计加 10 分之间选择最低未占用金额；金额分配、订单创建及库存预留处于同一写事务，保证并发不重复分配。
- 11 个金额档位均占用时返回 HTTP 409、`error.code=payment_amount_busy`，提示稍后重新下单；不创建订单、不占库存。不同渠道独立分配，已发卡、取消、过期后可复用金额。升级前已有重复金额不改价，新订单会避开这些金额；过期补发和历史重复金额需结合账单时间、备注人工核实。
- 旧订单按 `quantity=1` 迁移，`unit_price_cents` 与原订单金额一致，原发出的卡密不会重新分配。
- 旧订单缺少联系方式时，不能通过联系方式查询；继续支持原订单号加 token 的接口及后台查询。

## 管理后台

登录使用 HttpOnly、SameSite=Strict session cookie。所有后台写入请求都必须携带 `X-CSRF-Token`。

| 接口 | 请求 | 返回 |
| --- | --- | --- |
| `POST /api/admin/login` | `{password}` | `{csrf_token}` |
| `GET /api/admin/session` | — | `{authenticated,csrf_token?}` |
| `POST /api/admin/logout` | — | 退出登录 |
| `POST /api/admin/password` | `{current_password,new_password,confirm_password}` | `{ok:true}`，成功后退出全部管理员会话 |
| `GET /api/admin/overview` | — | `{stats,settings,channels,orders}` |
| `GET /api/admin/orders` | `?query=&status=all&offset=0` | `{orders,total,has_more}`，每页 50 条，待处理优先、其余最新在前 |
| `GET /api/admin/orders/<id>` | — | `{order}` |
| `POST /api/admin/orders/<id>/confirm` | `{}`，无需备注 | `{order}` |
| `POST /api/admin/orders/<id>/cancel` | `{note?}` | `{order}` |
| `GET /api/admin/products` | — | `{products}` |
| `POST /api/admin/products` | `{name,description,price_cents,active}` | 201 `{product}` |
| `PATCH /api/admin/products/<id>` | 商品字段，可部分修改 | `{product}` |
| `GET /api/admin/products/<id>/cards` | `?status=all&offset=0` | `{cards,total,has_more}`，每页 50 条 |
| `POST /api/admin/products/<id>/cards` | `{codes:多行字符串}` | `{inserted,duplicates}` |
| `PATCH /api/admin/cards/<id>` | `{code}` | `{card}` |
| `DELETE /api/admin/cards/<id>` | — | `{ok:true}` |
| `POST /api/admin/settings` | `{shop_name,support_notice}` | `{settings}` |
| `POST /api/admin/qr/<channel>` | multipart `file` | `{ok:true}` |

- 订单 `query` 按完整订单号或规范化联系方式精确查找；`status` 可为 `all` 或订单状态。
- 卡密 `status` 可为 `all`、`available`、`reserved`、`sold`。每条卡密包含 `{id,product_id,code,status,created_at}`，仅管理员可访问。
- 每张卡密占一行，最多 4000 字符；批量导入每次 1–10000 张，总输入最多 1000000 字符。编辑接口与页面采用相同的单张长度限制。
- 只有 `available` 卡密可修改或删除，预留及已售卡只读。卡密全文哈希全库去重；修改重复内容返回 409。
- 确认发卡必须先人工核实真实到账。接口自动记录审计备注；整单卡密在一个事务内发出，重复确认返回原结果，不会重复发卡。
- 超时订单可用可售库存补发，不占用其他订单预留卡；数量不足返回 409，整单回滚，不会部分发卡或错误标记已发卡。已取消订单不能发卡。
- 管理员可取消 `pending` 和 `expired` 订单，释放该订单全部预留库存；重复取消 `cancelled` 订单幂等返回原结果，`paid` 订单不能取消。取消无需金额通道解锁，也不会锁住同价商品后续下单。
- 收款码支持 PNG、JPEG、WebP，最大 2MB；有对应渠道待处理订单时沿用收款码更换保护。
- `settings.auto_delivery` 固定为 `false`。前台不展示自动发卡配置，后台请求不能启用通知发卡。
- 修改密码需要已登录会话、有效 CSRF 和正确的 `current_password`。`new_password` 须为 12–256 字符，并与 `confirm_password` 完全一致；校验失败不会修改密码。
- 密码哈希持久化到数据库私有 `admin_auth` 表，不放入公开 `settings`，也不会从接口返回。首次初始化使用部署配置中的初始哈希，已有数据库中的密码不会被环境变量覆盖。
- 修改密码后，当前和其他已登录管理员会话全部失效；后续管理接口必须以新密码重新登录。该接口只修改管理员认证，不清空业务数据。

## Hermes 私有集成

专用 `HERMES_API_TOKEN` 鉴权，`Authorization: Bearer <token>`。Nginx 拒绝公网 `/_hermes/`，仅供服务器本机的微信助手使用。禁用或未配置令牌时拒绝全部请求；不授予后台登录权限。

| 接口 | 用途 |
| --- | --- |
| `GET /_hermes/inventory` | 商品和可用/预留/已售库存，不返回卡密 |
| `GET /_hermes/orders?query=&status=pending&offset=0` | 分页查询订单摘要，不返回卡密 |
| `POST /_hermes/products/<id>/cards` | `{codes}`，复用原导入校验和去重 |
| `POST /_hermes/orders/<id>/confirm` | `{payment_verified:true,expected_total_cents,expected_quantity}`，显式人工核款后发卡 |
| `GET /_hermes/notifications` | 读取到期的待发送订单提醒 |
| `POST /_hermes/notifications/<id>/result` | `{delivered:boolean}`，确认成功或安排重试 |

部署和微信用法见 [HERMES_DEPLOYMENT.md](HERMES_DEPLOYMENT.md)。

## 停用的旧功能（历史兼容）

`POST /api/device/events`、`POST /api/device/heartbeat` 返回 410，不写入收款事件、不触发发卡。设备在线状态不影响下单。旧后台事件处理 `/api/admin/events/<id>/resolve` 和金额解锁 `/api/admin/lanes/unlock` 在管理员认证后返回 410。旧金额锁、冷却已停用，历史记录保留，不需要人工解锁；新订单通过上述 0–10 分金额分配规则区分。

授权边界：联系方式就是买家的查卡凭证，知道联系方式的人可以查看对应订单和已发卡密；系统不校验联系方式归属。管理员权限仍用于商品、库存、发卡及设置操作。
# sub2api 自动兑换扩展

完整启用要求见 [SUB2API.md](SUB2API.md)。普通订单原接口保持兼容。

| 接口 | 鉴权／输入 | 返回 |
| --- | --- | --- |
| `POST /api/sub2api/session` | JSON `{token,user_id?}`，用户编号仅用于一致性核对 | `{session_token,expires_at,user:{id,email}}` |
| `GET /api/sub2api/session` | `X-Sub2api-Session` | `{user,needs_reauth}` |
| `GET /api/sub2api/orders?offset=0` | `X-Sub2api-Session` | 本账户 `{orders,total,has_more}`，每页 50 条 |
| `POST /api/sub2api/orders/<id>/retry` | 同上，必须属于原账户 | `{order}` |
| `POST /api/admin/orders/<id>/redeem-retry` | 管理员会话和 CSRF | `{order}`，解除本实例兼容暂停并重试该订单未成功项 |
| `GET /api/admin/sub2api` | 管理员会话 | `{sub2api:{enabled,base_url,relay_confirmed,paused,message,worker_seen}}` |

- `POST /api/orders` 可携带 `X-Sub2api-Session`。无此头为普通订单；无效凭据返回 401，不静默降级。用户不能在订单请求中指定充值用户。目标账户、是否自动兑换在下单时固定。
- 商品创建、更新和响应增加布尔 `sub2api_redeem`，默认 false；公开商品接口另返回 `sub2api_enabled`。管理员 overview 增加 `sub2api` 诊断对象。
- 订单响应增加 `redemption`：未绑定为 null；已绑定包含 `{status,auto,target_user:{id,email},items,active,can_retry}`。
- `items` 按卡密顺序排列，字段 `{position,status,message,redeemed_at}`；position 从 0 开始，成功时间为上游兑换记录的 Unix 秒。
- 状态包含 `not_requested`、`awaiting_delivery`、`cancelled`、`pending`、`processing`、`retry`、`succeeded`、`partial`、`reauth`、`failed`、`incompatible`。订单 `paid` 仍仅表示已发卡。`active` 表示有任务正在运行或等待自动重试。
- 卡密只在 `paid` 后返回，成功兑换后继续返回。token 密文、原始登录 token、任务租约与本地会话凭据不出现在订单查询响应中。
- 身份接入每个 IP 每 5 分钟最多 20 次，返回的店铺会话仅以哈希持久化。



## 管理页 sub2api 配置

- `GET /api/admin/sub2api`：返回 `sub2api` 的地址、启用状态、代兑确认、`revision`、`key_configured`、`worker_seen`、`worker_revision`、暂停原因。需要管理员登录，无密钥返回。
- `POST /api/admin/sub2api`：管理员登录及 CSRF；提交 `{base_url, enabled, relay_confirmed, revision}`。布尔值须为 JSON 布尔型。启用要求 HTTPS 首页和代兑确认；版本冲突或未完成绑定订单阻止换站点时返回 409。成功返回更新后的 `sub2api`。保存后立即生效；已发出的请求可能继续完成。
- `POST /api/admin/sub2api/check`：管理员登录及 CSRF；提交 `{base_url}`，只检查该地址公开接口，返回 `{site_name, message}`。不保存、不充值、不携带用户凭据，不能证明登录绑定兼容；连接失败或接口不兼容返回 503。

数据库私有表 `sub2api_config` 独立于公开店铺设置；环境变量仅首次导入，密钥自动生成或保留原值后加密保存。恢复备份需保留原 `SECRET_KEY`。
