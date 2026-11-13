"""Version-independent Sub2API protocol boundary. Never log upstream bodies/tokens."""
from __future__ import annotations

import json
import socket
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class IntegrationError(Exception):
    def __init__(self, kind, message, *, uncertain=False):
        super().__init__(message)
        self.kind, self.message, self.uncertain = kind, message, uncertain


def normalize_base_url(value):
    if not value:
        return ""
    parts = urlsplit(value.strip())
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise ValueError("SUB2API_BASE_URL 必须是无路径、无凭据的 HTTPS 站点地址")
    # Reject whitespace/control characters before constructing headers or CSP.
    if any(ord(c) <= 32 or c in "'\";\\" for c in value):
        raise ValueError("SUB2API_BASE_URL 含非法字符")
    _ = parts.port
    return "https://" + parts.netloc.lower()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the bearer credential to another endpoint.


def incompatible(message="sub2api 接口不兼容，请管理员检查对接"):
    return IntegrationError("incompatible", message)


class Sub2APIClient:
    def __init__(self, base_url, timeout=10, opener=None):
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self.opener = opener or build_opener(NoRedirect())

    def _request(self, method, path, token, body=None):
        if not self.base_url:
            raise incompatible("尚未配置 sub2api 地址")
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.base_url + path, data=data, method=method, headers={
            **({"Authorization": "Bearer " + token} if token else {}), "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "LightCard-Sub2API/1",
        })
        status = 0
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(2_000_001)
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(100_000)
            exc.close()
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise IntegrationError("retry", "sub2api 暂时无法连接，将自动重试", uncertain=method == "POST") from None
        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeError):
            envelope = None
        code = ""
        if isinstance(envelope, dict):
            error = envelope.get("error", envelope)
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                code = error["code"].upper()
        if status in (401, 403):
            raise IntegrationError("reauth", "登录已失效或不允许后台代兑，请从 sub2api 重新进入并核对登录绑定设置")
        if status == 429 or status >= 500 or code in ("REDEEM_CODE_LOCKED", "REDEEM_RATE_LIMITED"):
            raise IntegrationError("retry", "sub2api 暂时繁忙，将自动重试", uncertain=method == "POST")
        if code == "REDEEM_CODE_USED":
            raise IntegrationError("used", "兑换码已使用，需要核实到账账户", uncertain=True)
        if code in ("REDEEM_CODE_NOT_FOUND", "REDEEM_CODE_EXPIRED", "INSUFFICIENT_BALANCE", "SUBSCRIPTION_NOT_FOUND"):
            raise IntegrationError("failed", "兑换码无效、过期或不适用于此账户，请联系店主")
        if status not in range(200, 300) or not isinstance(envelope, dict) or len(raw) > 2_000_000:
            raise incompatible()
        # The documented envelope is {code: 0, data: ...}; added fields are ignored.
        if type(envelope.get("code")) is not int or envelope["code"] != 0 or "data" not in envelope:
            raise incompatible("sub2api 返回格式变化，已暂停兑换，请管理员检查")
        return envelope["data"]

    def check_connection(self):
        data = self._request("GET", "/api/v1/settings/public", None)
        if not isinstance(data, dict) or not isinstance(data.get("site_name"), str):
            raise incompatible("公开设置接口缺少站点名称，请检查地址和接口兼容性")
        return {"site_name": data["site_name"][:200],
                "message": "连接正常，公开接口可用。此检查不验证登录 IP / UA 限制或实际兑换，请使用测试账户完成一次验证。"}

    def identity(self, token):
        data = self._request("GET", "/api/v1/auth/me", token)
        if not isinstance(data, dict):
            raise incompatible("sub2api 身份接口返回格式不兼容")
        user = data.get("user", data)
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            raise incompatible("sub2api 身份接口缺少有效用户编号")
        if user.get("status", "active") != "active":
            raise IntegrationError("reauth", "sub2api 账户当前不可用")
        email = user.get("email", "")
        if not isinstance(email, str) or len(email) > 254:
            raise incompatible("sub2api 身份接口返回邮箱格式不兼容")
        return {"id": user["id"], "email": email}

    @staticmethod
    def receipt(item, code, user_id):
        if not isinstance(item, dict) or item.get("code") != code or item.get("status") != "used":
            return None
        if "used_by" in item and (type(item["used_by"]) is not int or item["used_by"] != user_id):
            raise IntegrationError("failed", "兑换码已用于其他账户，请联系店主")
        # Never manufacture an upstream redemption timestamp.
        used_at = item.get("used_at")
        try:
            timestamp = int(datetime.fromisoformat(used_at.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, AttributeError, OverflowError, OSError):
            raise incompatible("sub2api 兑换记录缺少有效兑换时间") from None
        if timestamp <= 0:
            raise incompatible("sub2api 兑换记录时间无效")
        return {"redeemed_at": timestamp}

    def history_receipt(self, token, code, user_id):
        deadline = time.monotonic() + 45
        for page in range(1, 201):
            if time.monotonic() >= deadline:
                raise incompatible("兑换历史核实耗时过长，请管理员检查")
            query = urlencode({"page": page, "page_size": 100})
            data = self._request("GET", "/api/v1/redeem/history?" + query, token)
            legacy = isinstance(data, list)
            if legacy:
                items = data
            elif isinstance(data, dict) and isinstance(data.get("items"), list) and type(data.get("total")) is int and data["total"] >= 0:
                items = data["items"]
            else:
                raise incompatible("sub2api 兑换历史接口格式变化，无法核实到账")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                    raise incompatible("sub2api 兑换历史缺少必要字段")
                if item["code"] == code:
                    result = self.receipt(item, code, user_id)
                    if not result:
                        raise incompatible("sub2api 兑换历史状态不兼容")
                    return result
            if legacy or not items or page * 100 >= data["total"]:
                return None
        raise incompatible("兑换历史超出核实范围，请管理员人工核对")

    def redeem(self, token, code, user_id):
        data = self._request("POST", "/api/v1/redeem", token, {"code": code})
        if isinstance(data, dict) and data.get("used_by") == user_id:
            result = self.receipt(data, code, user_id)
            if result:
                return result
        # Older/newer success summaries may omit the receipt: prove it in history.
        result = self.history_receipt(token, code, user_id)
        if result:
            return result
        raise IntegrationError("incompatible", "兑换响应无法确认到账，已暂停，请管理员核实", uncertain=True)
