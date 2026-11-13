# pay.dhy-api.com 部署与运维

## 管理页 sub2api 配置版本

- 设置页可保存站点地址、启用状态和代兑兼容确认，可检查公开接口连接。密钥自动生成并加密保存，旧密钥首次迁移时保留。
- 保存后 Web 立即生效，worker 通常在 2 秒内自动读取；关闭后保留任务，再次启用继续处理。环境变量仅首次初始化导入。
- 迁移新增私有 `sub2api_config` 表，配置不会通过公开店铺设置泄露。备份与恢复必须保留原 `SECRET_KEY`，且停止 Web 与 worker 后再恢复数据库。
- 本地 107 项后端、71 项前端测试及两套真实浏览器验收通过。设置页已检查桌面和手机布局；覆盖配置冲突、登录切换迟到响应、暂停续兑、重启保留与旧订单换站点保护。
- 此版本部署结果记录在 `.deployment/deployment-admin-config-result.json`；生产上不自动勾选兼容确认，也不自动启用商品。完整启用步骤见 [SUB2API.md](SUB2API.md)。

## 上一发布：重复金额区分与 sub2api 接入（2026-11-13 23:30，北京时间）

- 已部署当前完整版本至 `https://pay.dhy-api.com`。同渠道重复金额订单会在商品合计至加 0.10 元范围内逐分分配应付金额；付款页和收款码大图显示实际应付金额。
- `app`、`redeem-worker` 容器均 healthy。生产镜像在隔离临时数据库中运行 97 项后端测试通过，本地 67 项前端测试通过；上线后 5 组公网检查通过，七个页面／脚本／样式文件 SHA256 与本地一致。
- 发布前后 4 个商品、30 张卡密、5 笔订单及其原字段校验一致；管理员认证、设置、卡密关联和收款码均保留。Hermes、监控、new-api、PostgreSQL、Redis 容器 ID 未变；私有 Hermes 库存接口只读检查通过。
- sub2api 模块与 worker 已发布，首页允许 `https://sub2api.dhy-api.com` 嵌入，后台继续禁止嵌入。`SUB2API_ENABLED=0`：自动兑换尚未启用，仍需按 `SUB2API.md` 在管理页完成登录绑定兼容确认、启用和商品开关设置（密钥现已自动管理）。
- Nginx 仅本域名的 HTTP／HTTPS 访问日志改为无查询参数格式，错误日志按接入说明关闭，避免菜单 URL 中 token 写入日志。原 TLS、代理和 Hermes 私有路径保护保留。
- 私有备份：`/root/lightcard-pay-backups/before-latest-20261113T153044Z`，包含在线 ZIP、停服后的 SQLite 一致性快照、原源码、环境配置、Nginx 配置与部署结果。
- 发布镜像：`lightcard-pay-release:20261113t153044z`；回滚镜像：`lightcard-pay-rollback:latest-20261113t153044z`。实际上传包 SHA256：`2d63e13b5ecf9e78ebbc7fae7892538406400f1baf605f80e7b496fd8e5988b9`。
- 发布记录：`.deployment/deployment-latest-result.json`；公网验收：`.deployment/latest-live-check/report.json`。线上页面验收使用浏览器内模拟订单，未创建生产测试订单或兑换真实卡密。

以下旧发布记录为历史信息，其中清空数据库的操作不属于本次更新。

2026-11-13 追加部署 Hermes 个人微信小店助手：使用独立预构建容器，查库存、查单、导卡和人工确认发卡接入已验证。详见 [HERMES_DEPLOYMENT.md](HERMES_DEPLOYMENT.md)。此次追加更新保留全部现有业务数据，没有再次清库。

更新日期：2026-11-13。**极简人工确认发卡版已发布，公开 HTTPS、应用健康及生产浏览器检查通过。** 本次按用户授权，先完整备份再清空本服务业务数据库，保留原支付宝收款码和初次登录密码。后台支持设置自己的密码。真实收款仍需使用店主收款码另行验证。

## 收款码放大更新（2026-11-13）

已完成静态页面增量更新：付款页、联系方式查单和后台收款码均支持点击放大、两倍缩放与滚动查看。手机和桌面使用自适应大图，保持原始图片比例；付款状态失效时自动关闭大图。静态资源增加版本参数，刷新页面即可加载。

- 本次仅替换六个静态文件并重建 `lightcard-pay` 的 `app` 镜像。`app.py`、环境配置、管理员密码和原收款码保持不变；部署前后均为 4 个商品、20 张卡密、3 笔订单（部署时快照）。其他三个服务容器 ID 均保持不变。
- 备份目录：`/root/lightcard-pay-backups/before-qr-preview-20261113T142249Z`，包含已验证完整性的业务 ZIP、原源码及环境配置、元数据与部署结果。回滚镜像：`lightcard-pay-rollback:qr-preview-20261113t142249z`。本次没有清空数据库，回滚前端也不需要恢复业务数据库。
- 本地 57 项前端回归、15 组浏览器检查通过。公开 HTTPS 健康接口正常，六个发布文件与本地 SHA256 一致；真实上传的 1260×1960 收款图片在 320×640、390×844、844×390、1365×900 四种视口通过放大、滚动、关闭和布局检查。
- 线上浏览器付款验收在独立浏览器中拦截订单响应，没有在服务器创建测试订单或修改业务数据。旧保存密码已无法登录，因此后台交互的浏览器验证使用本地临时数据库完成，未修改线上管理员密码。
- 本地发布记录：`.deployment/deployment-qr-preview-result.json`；本地与线上浏览器报告分别位于 `.deployment/qr-preview-browser-check/`、`.deployment/qr-preview-live-check/`。

后续原版发布记录按历史信息保留；其中清空数据库的操作不属于此次收款码放大更新。

## 部署信息

| 项目 | 配置 |
| --- | --- |
| 服务器 | `43.108.33.221` |
| 系统 | Ubuntu 24.04 / Linux amd64 |
| 店铺地址 | `https://pay.dhy-api.com` |
| 管理后台 | `https://pay.dhy-api.com/admin` |
| 服务目录 | `/opt/lightcard` |
| Compose 项目名 | `lightcard-pay` |
| Compose 文件 | `compose.yaml`、`compose.host.yaml` |
| 应用监听 | `127.0.0.1:18080` → 容器 `8000` |
| 应用数据卷 | `lightcard-pay_shop_data` |
| Nginx 站点文件 | `/etc/nginx/conf.d/pay.dhy-api.com.conf` |
| ACME 验证目录 | `/var/www/lightcard-acme` |
| Certbot 证书名 | `pay.dhy-api.com` |
| 证书目录 | `/etc/letsencrypt/live/pay.dhy-api.com/` |

此服务器已有 Nginx、Certbot 和其他域名服务。本次沿用主机 Nginx 终止 HTTPS，只启动发卡服务的 `app` 容器；不启动项目自带的 Caddy，不占用新的公网 80/443 监听。`compose.host.yaml` 将应用端口限定在主机回环地址，Nginx 反向代理到 `http://127.0.0.1:18080`。

线上 `.env` 已设置 `COMPOSE_FILE='compose.yaml:compose.host.yaml'` 和 `COMPOSE_PROJECT_NAME='lightcard-pay'`；覆盖文件中的 Caddy 服务只有显式启用对应 profile 时才会启动。以下运维命令仍完整列出项目名、配置文件和 `app` 服务，便于核对实际操作范围。

以下命令在服务器执行，需要具备 Docker 与对应系统目录的管理权限。每条 Compose 命令均显式指定项目名和两个配置文件，避免误用默认项目 `lightcard` 或启动 Caddy。

## 本次发布（2026-11-13）

- 仅更新 `lightcard-pay` 的 `app` 服务，容器状态 healthy；公开店铺 `/` 和后台 `/admin` 均返回 HTTPS 200，HTTP 跳转 HTTPS。订单有效期为 1800 秒，固定人工确认发卡。
- 清空前的业务数据为 3 个商品、4 张卡密、4 笔订单。先完成在线 ZIP、停止服务后的 SQLite 冷备、原源码和环境配置备份，再初始化空数据库。部署脚本检查时 products、cards、orders、events、devices、lanes、audit 均为 0；后续管理员登录会正常产生新审计记录。
- 原 `alipay.jpg` 收款码的哈希校验一致；`.env`、原 `compose.host.yaml` 和收款码保留。既有 `new-api`、`newapi-postgres`、`newapi-redis` 容器 ID 均未改变。
- 公开验证确认原密码可登录，Cookie 具有 Secure、HttpOnly 和 SameSite 属性，未认证访问受拒、CSRF 检查有效、退出成功、私密路径不可访问。商品、订单与库存保持为空。
- 生产 Playwright 六组检查通过：干净的手机店铺、联系方式查询弹窗、原密码登录与空订单、空库存、密码字段遮罩及设置页无水平溢出、退出且无 JavaScript 异常。没有创建或修改生产商品、卡密、订单，**没有修改生产管理员密码**。

本次发布的备份与可回滚资产：

| 项目 | 已验证记录 |
| --- | --- |
| 私有备份目录 | `/root/lightcard-pay-backups/before-manual-reset-20261113T135354Z` |
| 备份内容 | 在线 ZIP、SQLite 冷备、原环境配置及源码 |
| 原版回滚镜像 | `lightcard-pay-rollback:20261113t135354z` |
| 实际部署包 SHA256 | `792c832f02ae198107ab07c7f569f8223313b53cb9e27e735b1517ef9b2ec14a` |
| 部署 app.py SHA256 | `d5668c6ac398203606b972c7c1ab2009ec6cdabef6aae55b51b5b868d3672640` |

哈希对应本次实际上传的发布包和代码，不代表后续本地文档修订已重新打包。必要时先停止本项目的 `app`，按同批次恢复原源码、环境配置与数据库，并使用对应回滚镜像；不要混用新旧业务数据库。备份保存在非公开目录，不应复制到网站静态目录。

本地验收为 59 项 Python、57 项 Node 和十一组浏览器检查；生产补充检查为六组。具体覆盖及限制见 [VERIFICATION.md](VERIFICATION.md)。

## 日常查看与启动

```bash
cd /opt/lightcard
docker compose -p lightcard-pay -f compose.yaml -f compose.host.yaml ps app
docker compose -p lightcard-pay -f compose.yaml -f compose.host.yaml logs --tail=100 app
```

构建并启动应用：

```bash
cd /opt/lightcard
docker compose -p lightcard-pay -f compose.yaml -f compose.host.yaml up -d --no-deps --build app
```

只重启应用进程：

```bash
cd /opt/lightcard
docker compose -p lightcard-pay -f compose.yaml -f compose.host.yaml restart app
```

修改 `.env` 后，使用 `up -d --no-deps app` 重新创建需要更新的容器；仅执行 `restart app` 不会加载新的环境变量：

```bash
cd /opt/lightcard
docker compose -p lightcard-pay -f compose.yaml -f compose.host.yaml up -d --no-deps app
```

## 验证入口

```bash
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS https://pay.dhy-api.com/healthz
curl -I https://pay.dhy-api.com/
curl -I https://pay.dhy-api.com/admin
```

健康接口预期返回 JSON，字段 `ok` 为 `true`。还需通过浏览器确认店铺和后台页面能正常加载、管理员可登录；健康接口成功不代表扫码付款与人工发卡已经验收。

维护本域名的 Nginx 配置后，先检查语法，成功后再重载：

```bash
nginx -t && systemctl reload nginx
```

检查证书与续期：

```bash
certbot certificates --cert-name pay.dhy-api.com
certbot renew --cert-name pay.dhy-api.com --dry-run --run-deploy-hooks --no-random-sleep-on-renew
systemctl status certbot.timer --no-pager
```

续期使用 `/var/www/lightcard-acme` 的 HTTP 验证路径，需保持域名解析、TCP 80/443 和该路径可用。`certbot.timer` 已确认处于 active、enabled 状态；本域名续期的 deploy hook 为 `nginx -t && systemctl reload nginx`。上述包含 deploy hook 的模拟续期命令已执行成功。当前证书有效期至 **2027-02-11 19:38:38（北京时间）**，证书 SAN 包含 `pay.dhy-api.com`。

## 备份

应用提供 SQLite 在线备份，生成的 ZIP 包含数据库、订单、卡密和收款码。更新前先下架商品，核对当前待处理订单；从旧版本升级时还需关闭原自动发卡开关，再执行：

```bash
cd /opt/lightcard
docker compose -p lightcard-pay -f compose.yaml -f compose.host.yaml exec -T app python scripts/backup.py
install -d -m 700 /root/lightcard-pay-backups
docker compose -p lightcard-pay -f compose.yaml -f compose.host.yaml cp app:/app/data/backups /root/lightcard-pay-backups/
```

容器中备份路径为 `/app/data/backups/lightcard-<UTC时间>.zip`，复制后位于主机 `/root/lightcard-pay-backups/backups/`。应定期将备份安全保存到另一台设备，并检查 ZIP 可读取。备份含明文业务敏感数据，不要放入网站公开目录。

部署时另已生成初始备份 `/root/lightcard-pay-backups/lightcard-20261113T124104Z.zip`，完成 ZIP 读回与 SQLite `integrity_check` 验证。该初始备份位于备份根目录；按上方目录复制命令创建的后续副本位于 `backups/` 子目录。

ZIP **不包含** `/opt/lightcard/.env`。还应单独安全备份 `.env`、`compose.host.yaml`、本域名的 Nginx 配置，以及需要保留的私密凭据文件。不要在服务运行时直接复制或覆盖 SQLite 主文件。恢复步骤见项目 `README.md`；恢复前停止 `app`，保留当前数据副本，并确认恢复后数据对容器 UID/GID `10001:10001` 可写。

## 日后更新

以下是保留数据的普通更新流程。本次授权清空已单列记录，后续更新不应默认清库。

1. 下架商品并核对未完成付款，先备份；从旧版本升级时关闭原自动发卡开关。
2. 上传新版源码到 `/opt/lightcard`，保留线上 `.env`、`compose.host.yaml` 和数据卷。不要重新执行初始化脚本生成新密钥。
3. 编辑既有 .env，将 ORDER_TTL_SECONDS 改为 1800；旧 DEVICE_TOKEN、PAYMENT_COOLDOWN_SECONDS 和 AUTO_DELIVERY 可删除，极简版不再使用。保留原管理员密码哈希、SECRET_KEY、COMPOSE_FILE 与 COMPOSE_PROJECT_NAME。
4. 在 `/opt/lightcard` 执行本页的 `up -d --no-deps --build app` 命令，随后检查 `ps app`、应用日志和两处健康接口。
5. 刷新网页，确认后台登录、库存卡密和历史订单；创建单张及多张订单，验证单价乘数量的总额、联系方式查询、真实小额到账及整单人工发卡，并验证取消未发卡订单释放全部预留库存，完成后再上架商品。

升级会保留商品、卡密、订单和收款码。旧订单按数量 1 迁移，原金额及卡密保持不变。新订单联系方式必填；旧订单没有联系方式时不自动补填，可继续使用原 token 查询接口或让管理员按订单号查找。

**不要运行 `docker compose down -v`，不要删除 `lightcard-pay_shop_data`。** 删除卷会丢失订单、库存、卡密、收款码和应用设置。更新代码、重建镜像或正常重启不会要求清空卷。

需要停服时，应先处理已展示二维码的待付款订单；停止服务不会阻止买家继续向个人收款码转账。仅停止本服务可使用：

```bash
cd /opt/lightcard
docker compose -p lightcard-pay -f compose.yaml -f compose.host.yaml stop app
```

## 凭据与首次使用

凭据已保存到本机 `.deployment/pay.dhy-api.com-credentials.txt` 和服务器 `/root/lightcard-pay-credentials.txt`。本机 `.deployment` 目录 ACL 仅允许当前 Windows 用户和 SYSTEM 访问；服务器凭据文件与 `/opt/lightcard/.env` 权限均为 `0600`。管理后台只有密码，无需用户名。本文不保存管理员密码或站点密钥；`.deployment` 已加入 Git 和 Docker 忽略配置，凭据文件不得放入部署包、公开仓库或网站目录。


本次保留原初始密码供首次登录，没有替用户修改密码。登录后进入“设置”，在密码表单填写当前密码、新密码和确认密码；新密码须为 12–256 字符。保存成功后全部管理员会话失效，使用新密码重新登录。

新密码哈希保存在数据库私有 `admin_auth` 表中；原凭据文件及 `.env` 仅保留部署初始值，不能用来判断用户之后设置的密码。普通重启或更新不会覆盖数据库中的密码，接口和日志不输出密码。

**极简版固定人工确认到账后发卡。** 首次登录后核对保留的收款码，按需上传自己的其他收款码，再创建商品并导入卡密。买家选择同商品数量（每单 1–100 张，默认 1）、填写联系方式后按订单总额扫码付款，付款备注填写同一联系方式；管理员核对账单后在订单中点击“发卡”。整单卡密一次发出，可凭联系方式查回并全部复制。

无需安装安卓监听。旧设备上报返回 410，旧数据库自动发卡设置不会生效；金额锁、同金额互斥与冷却均已停用。订单默认按数量预留卡密 30 分钟，超时释放整单库存；管理员可取消未发卡的待处理及已过期订单，重复取消不会重复释放。已付款的超时订单可核实后使用可售库存整单补发，数量不足时全部回滚，不占用其他订单预留卡。

## 原版本部署验收记录

以下为原版本在 2026-11-13 的部署记录，不代表极简版已完成线上更新：

- 应用容器 `lightcard-pay-app-1` 状态为 healthy，应用通过 `127.0.0.1:18080` 提供服务；检查时原有其他容器仍保持运行，显示已运行 11 天。
- 公网 `/`、`/admin`、`/healthz` 均返回 HTTP 200，健康接口 `ok` 为 `true`。HTTP 请求通过 308 跳转至 HTTPS，HSTS 响应头已生效。
- TLS 1.3 连接通过证书链与主机名校验，证书 SAN 匹配 `pay.dhy-api.com`。
- 管理员实际登录、session 查询和退出已验证；会话 Cookie 具备 Secure、HttpOnly 和 SameSite 属性。未登录访问 overview 返回 401，缺少 CSRF 的退出请求返回 403。
- 初始订单、库存与事件数量均为 0，自动发卡为 `false`；没有创建模拟商品、模拟付款或业务订单。
- 私密路径的 HTTP 访问检查返回 404，服务器凭据及环境配置文件权限、Windows 凭据目录 ACL 已核对。
- Certbot 自动续期定时器已启用，包含 deploy hook 的本域名模拟续期通过；初始备份完成 ZIP 与 SQLite 完整性验证。

**极简版上线验收：** 上传本人收款码、创建固定价格商品、导入卡密；分别完成支付宝和微信真实小额付款，确认单张及多张订单的总额、整单人工发卡与复制、联系方式查卡、取消和超时释放全部库存及手机端操作。原有服务器健康与证书记录不能替代此次业务验收。
# sub2api 可选接入更新

新增独立 `redeem-worker` 服务和加密依赖。启用方法、Nginx 日志脱敏、登录 IP／UA 绑定检查及升级验收见 [SUB2API.md](SUB2API.md)。现有商品和历史订单不自动绑定；部署代码后仍需配置环境并为对应商品启用。

已有主机 Nginx 实例更新使用 `docker compose -f compose.yaml -f compose.host.yaml up -d --build app redeem-worker`。恢复、覆盖或迁移数据库前同时停止这两个服务，保留原 `.env` 的 `SECRET_KEY`；sub2api 配置和加密后的密钥现保存在数据库内，旧环境配置仅首次导入。

