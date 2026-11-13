"""Gateway-owned worker: durable shop outbox, bounded retries, owner-only delivery."""
import asyncio
import json
import logging
import os
from urllib.request import Request, build_opener, HTTPRedirectHandler

logger = logging.getLogger("lightcard-notify")
_task = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def shop_request(path, data=None):
    token = os.environ["LIGHTCARD_API_TOKEN"]
    body = json.dumps(data).encode() if data is not None else None
    request = Request("http://127.0.0.1:18080/_hermes/" + path, data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with build_opener(NoRedirect).open(request, timeout=15) as response:
        return json.load(response)


def reminder(order):
    channel = {"wechat": "微信", "alipay": "支付宝"}.get(order["channel"], order["channel"])
    # Normalize controls and keep the notification bounded. Never include a card code.
    name = " ".join(str(order["product_name"]).split())[:100]
    contact = " ".join(str(order["contact"]).split())[:128]
    return (f"【新订单 · 待核款】\n订单号：{order['id']}\n商品：{name}\n"
            f"数量：{order['quantity']} 张\n总额：￥{order['price_cents'] / 100:.2f}\n"
            f"支付方式：{channel}\n联系方式：{contact}\n"
            "新订单不代表已经到账。请核对真实收款记录；确认后回复“给 [订单号] 发卡”。\n"
            "后台：https://pay.dhy-api.com/admin")


async def run_worker():
    from plugins.platforms.weixin.adapter import send_weixin_direct
    while True:
        try:
            account = os.environ.get("WEIXIN_ACCOUNT_ID", "")
            owner = os.environ.get("WEIXIN_HOME_CHANNEL", "")
            token = os.environ.get("WEIXIN_TOKEN", "")
            if account and owner and token:
                pending = await asyncio.to_thread(shop_request, "notifications")
                for entry in pending.get("notifications", []):
                    try:
                        result = await asyncio.wait_for(send_weixin_direct(
                            extra={"account_id": account, "base_url": os.environ.get("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com")},
                            token=token, chat_id=owner, message=reminder(entry["order"])), timeout=40)
                        delivered = result.get("success") is True
                    except Exception:
                        delivered = False
                    await asyncio.to_thread(shop_request, f"notifications/{entry['id']}/result", {"delivered": delivered})
                    logger.info("order notification id=%s delivered=%s", entry["id"], delivered)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Exception messages may contain credentials or customer data.
            logger.warning("notification poll failed: %s", type(exc).__name__)
        await asyncio.sleep(10)


async def handle(event_type, context):
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(run_worker(), name="lightcard-order-notify")
        logger.info("lightcard order notification worker started")
