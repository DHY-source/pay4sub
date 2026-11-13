"""Persistent account binding and leased, per-card redemption outbox."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing
from contextvars import ContextVar

from cryptography.fernet import Fernet, InvalidToken
from flask import g, has_request_context, jsonify, request

from sub2api_adapter import IntegrationError, Sub2APIClient, normalize_base_url


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def credential_deadline(token, now):
    # An unverified exp is ONLY an upper lifetime bound, never proof of identity.
    deadline = now + 86400
    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        if type(payload.get("exp")) in (int, float):
            deadline = min(deadline, int(payload["exp"]))
    except (ValueError, IndexError, TypeError, OverflowError, AttributeError):
        pass
    return deadline


class Sub2APIIntegration:
    def __init__(self, app, transaction, error):
        self.app, self.transaction, self.error = app, transaction, error
        self._operation = ContextVar("sub2api_config", default=None)
        secret = app.config["SECRET_KEY"]
        if isinstance(secret, str):
            secret = secret.encode()
        self._master = Fernet(base64.urlsafe_b64encode(hmac.new(secret, b"lightcard/sub2api/config/v1", hashlib.sha256).digest()))
        self.migrate()

    def read_config(self, db=None):
        if db is None:
            with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
                connection.row_factory = sqlite3.Row
                return self.read_config(connection)
        result = dict(db.execute("SELECT * FROM sub2api_config WHERE id=1").fetchone())
        try:
            result["cipher"] = Fernet(self._master.decrypt(result["token_key_cipher"].encode()))
        except (InvalidToken, ValueError):
            raise RuntimeError("sub2api 密钥无法读取，请恢复原 SECRET_KEY 和数据库备份") from None
        result["client"] = self.app.config.get("SUB2API_CLIENT") or Sub2APIClient(result["base_url"])
        return result

    def snapshot(self, db=None, refresh=False):
        current = self._operation.get()
        if current is not None:
            return current
        if has_request_context():
            if refresh or not hasattr(g, "sub2api_config"):
                g.sub2api_config = self.read_config(db)
            return g.sub2api_config
        return self.read_config(db)

    base_url = property(lambda self: self.snapshot()["base_url"])
    enabled = property(lambda self: bool(self.snapshot()["enabled"]))
    relay_confirmed = property(lambda self: bool(self.snapshot()["relay_confirmed"]))
    cipher = property(lambda self: self.snapshot()["cipher"])
    client = property(lambda self: self.snapshot()["client"])

    def migrate(self):
        with self.transaction() as db:
            # Private table: the generic settings table is exposed to shop visitors.
            db.execute("""CREATE TABLE IF NOT EXISTS sub2api_config (
                id INTEGER PRIMARY KEY CHECK(id=1), base_url TEXT NOT NULL,
                enabled INTEGER NOT NULL, relay_confirmed INTEGER NOT NULL,
                token_key_cipher TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL, worker_seen INTEGER NOT NULL DEFAULT 0,
                worker_revision INTEGER NOT NULL DEFAULT 0)""")
            if not db.execute("SELECT 1 FROM sub2api_config WHERE id=1").fetchone():
                base = normalize_base_url(self.app.config.get("SUB2API_BASE_URL", ""))
                enabled = bool(self.app.config.get("SUB2API_ENABLED"))
                relay = bool(self.app.config.get("SUB2API_RELAY_CONFIRMED"))
                if enabled and (not base or not relay):
                    raise RuntimeError("启用 sub2api 前必须配置地址并确认允许后台代兑")
                key = self.app.config.get("SUB2API_TOKEN_KEY", "")
                key = key.encode() if key else Fernet.generate_key()
                try:
                    Fernet(key)
                except (ValueError, TypeError):
                    raise RuntimeError("原 SUB2API_TOKEN_KEY 格式无效，请核对原密钥") from None
                db.execute("INSERT INTO sub2api_config(id,base_url,enabled,relay_confirmed,token_key_cipher,updated_at) VALUES (1,?,?,?,?,?)",
                           (base, int(enabled), int(relay), self._master.encrypt(key).decode(), int(time.time())))
            product_columns = {r[1] for r in db.execute("PRAGMA table_info(products)")}
            if "sub2api_redeem" not in product_columns:
                db.execute("ALTER TABLE products ADD COLUMN sub2api_redeem INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS sub2api_accounts (
                id INTEGER PRIMARY KEY, base_url TEXT NOT NULL, user_id INTEGER NOT NULL,
                email TEXT NOT NULL, token_cipher TEXT NOT NULL, authorized_until INTEGER NOT NULL,
                credential_version INTEGER NOT NULL DEFAULT 1, auth_state TEXT NOT NULL DEFAULT 'ready',
                updated_at INTEGER NOT NULL, UNIQUE(base_url,user_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS sub2api_sessions (
                token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES sub2api_accounts(id),
                expires_at INTEGER NOT NULL)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(orders)")}
            if "sub2api_account_id" not in columns:
                db.execute("ALTER TABLE orders ADD COLUMN sub2api_account_id INTEGER REFERENCES sub2api_accounts(id)")
            if "sub2api_auto_redeem" not in columns:
                db.execute("ALTER TABLE orders ADD COLUMN sub2api_auto_redeem INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS orders_sub2api_account ON orders(sub2api_account_id,created_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS sub2api_tasks (
                id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
                card_id INTEGER NOT NULL UNIQUE REFERENCES cards(id),
                account_id INTEGER NOT NULL REFERENCES sub2api_accounts(id),
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT NOT NULL DEFAULT '', uncertain INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', redeemed_at INTEGER, updated_at INTEGER NOT NULL,
                UNIQUE(order_id,card_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS sub2api_tasks_due ON sub2api_tasks(state,next_attempt_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS sub2api_runtime (
                base_url TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '', worker_seen INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS sub2api_attempts (
                ip_hash TEXT NOT NULL, created_at INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS sub2api_attempts_time ON sub2api_attempts(ip_hash,created_at)")
            base = db.execute("SELECT base_url FROM sub2api_config WHERE id=1").fetchone()[0]
            if base:
                db.execute("INSERT OR IGNORE INTO sub2api_runtime(base_url) VALUES (?)", (base,))

    def available(self):
        if not self.enabled:
            raise self.error("自动兑换尚未启用，可选择普通购买", "sub2api_disabled", 409)

    def api_error(self, exc):
        status = {"reauth": 401, "failed": 409, "used": 409}.get(exc.kind, 503)
        return self.error(exc.message, "sub2api_" + exc.kind, status)

    def session_account(self, db, required=True):
        self.snapshot(db, refresh=True)
        value = request.headers.get("X-Sub2api-Session", "")
        if not value and not required:
            return None
        self.available()
        row = db.execute("""SELECT a.* FROM sub2api_sessions s JOIN sub2api_accounts a ON a.id=s.account_id
            WHERE s.token_hash=? AND s.expires_at>? AND a.base_url=?""",
            (digest(value), int(time.time()), self.base_url)).fetchone() if 20 <= len(value) <= 256 else None
        if not row:
            raise self.error("店铺登录已失效，请从 sub2api 重新进入", "sub2api_reauth", 401)
        return row

    def bind_order(self, db, order_id, product):
        account = self.session_account(db, required=False)
        if account is None:
            return
        if account["auth_state"] != "ready" or account["authorized_until"] <= int(time.time()):
            raise self.error("登录已失效，请从 sub2api 重新进入或选择普通购买", "sub2api_reauth", 401)
        if product["sub2api_redeem"]:
            state = db.execute("SELECT paused FROM sub2api_runtime WHERE base_url=?", (self.base_url,)).fetchone()
            if state and state[0]:
                raise self.error("自动兑换对接需要检查，请选择普通购买或联系店主", "sub2api_incompatible", 503)
        db.execute("UPDATE orders SET sub2api_account_id=?,sub2api_auto_redeem=? WHERE id=?",
                   (account["id"], int(bool(product["sub2api_redeem"])), order_id))

    def enqueue(self, db, order_id):
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order["sub2api_auto_redeem"] or not order["sub2api_account_id"] or order["status"] != "paid":
            return
        db.execute("""INSERT OR IGNORE INTO sub2api_tasks(order_id,card_id,account_id,updated_at)
            SELECT oc.order_id,oc.card_id,?,? FROM order_cards oc WHERE oc.order_id=?""",
            (order["sub2api_account_id"], int(time.time()), order_id))

    def describe(self, db, order):
        if not order["sub2api_account_id"]:
            return None
        account = db.execute("SELECT user_id,email,base_url FROM sub2api_accounts WHERE id=?", (order["sub2api_account_id"],)).fetchone()
        tasks = db.execute("""SELECT t.*,oc.position FROM sub2api_tasks t JOIN order_cards oc
            ON oc.order_id=t.order_id AND oc.card_id=t.card_id WHERE t.order_id=? ORDER BY oc.position""", (order["id"],)).fetchall()
        states = {t["state"] for t in tasks}
        state = "not_requested" if not order["sub2api_auto_redeem"] else "awaiting_delivery"
        if order["status"] == "cancelled":
            state = "cancelled"
        if tasks:
            if states == {"succeeded"}:
                state = "succeeded"
            elif "succeeded" in states:
                state = "partial"
            else:
                state = next((s for s in ("incompatible", "reauth", "failed", "processing", "retry", "pending") if s in states), "pending")
        active = bool(states.intersection({"pending", "processing", "retry"}))
        items = [{"position": t["position"], "status": t["state"], "message": t["last_error"],
                  "redeemed_at": t["redeemed_at"]} for t in tasks]
        return {"status": state, "auto": bool(order["sub2api_auto_redeem"]),
                "target_user": {"id": account["user_id"], "email": account["email"]},
                "items": items, "active": active,
                "can_retry": bool(states.intersection({"failed", "incompatible", "reauth"}))}

    def diagnostics(self, db):
        config = self.read_config(db)
        row = db.execute("SELECT * FROM sub2api_runtime WHERE base_url=?", (config["base_url"],)).fetchone()
        return {"enabled": bool(config["enabled"]), "base_url": config["base_url"], "relay_confirmed": bool(config["relay_confirmed"]),
                "revision": config["revision"], "key_configured": True,
                "worker_revision": config["worker_revision"],
                "paused": bool(row and row["paused"]), "message": row["message"] if row else "",
                "worker_seen": config["worker_seen"]}

    def retry_order(self, db, order_id, administrator=False):
        self.snapshot(db, refresh=True)
        self.available()
        now = int(time.time())
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or not order["sub2api_auto_redeem"] or order["status"] != "paid":
            raise self.error("该订单没有可重试的自动兑换任务", "sub2api_no_tasks", 409)
        account = db.execute("SELECT * FROM sub2api_accounts WHERE id=?", (order["sub2api_account_id"],)).fetchone()
        if account["base_url"] != self.base_url:
            raise self.error("订单属于另一 sub2api 实例，不能重新绑定", "sub2api_instance_mismatch", 409)
        if account["auth_state"] != "ready" or account["authorized_until"] <= now or not account["token_cipher"]:
            raise self.error("需由原账户从 sub2api 重新进入授权", "sub2api_reauth", 409)
        runtime = db.execute("SELECT paused FROM sub2api_runtime WHERE base_url=?", (self.base_url,)).fetchone()
        if runtime[0] and not administrator:
            raise self.error("对接已暂停，请联系店主检查", "sub2api_incompatible", 503)
        if administrator:
            db.execute("UPDATE sub2api_runtime SET paused=0,message='' WHERE base_url=?", (self.base_url,))
        db.execute("""UPDATE sub2api_tasks SET state='retry',next_attempt_at=?,last_error='',updated_at=?
            WHERE order_id=? AND state IN ('failed','incompatible','reauth')""", (now, now, order_id))
        db.execute("INSERT INTO audit(created_at,action,target,note) VALUES (?,?,?,?)",
                   (now, "sub2api_retry", order_id, "管理员重试" if administrator else "原账户重试"))

    def process_once(self):
        marker = self._operation.set(None)
        try:
            return self._process_once()
        finally:
            self._operation.reset(marker)

    def _process_once(self):
        now = int(time.time())
        with self.transaction() as db:
            config = self.read_config(db)
            self._operation.set(config)
            db.execute("UPDATE sub2api_config SET worker_seen=?,worker_revision=? WHERE id=1", (now, config["revision"]))
            if not self.enabled:
                return False
            db.execute("UPDATE sub2api_runtime SET worker_seen=? WHERE base_url=?", (now, self.base_url))
            db.execute("DELETE FROM sub2api_sessions WHERE expires_at<=?", (now,))
            db.execute("UPDATE sub2api_accounts SET token_cipher='',auth_state='reauth' WHERE authorized_until<=? AND token_cipher<>''", (now,))
            # Recover crashed leases without trusting the previous request's outcome.
            db.execute("""UPDATE sub2api_tasks SET state='retry',uncertain=1,lease_token='',lease_until=0
                WHERE state='processing' AND lease_until<=?""", (now,))
            runtime = db.execute("SELECT paused FROM sub2api_runtime WHERE base_url=?", (self.base_url,)).fetchone()
            if runtime[0]:
                return False
            job = db.execute("""SELECT t.*,c.code,a.user_id,a.token_cipher,a.authorized_until,a.credential_version,a.auth_state
                FROM sub2api_tasks t JOIN cards c ON c.id=t.card_id JOIN sub2api_accounts a ON a.id=t.account_id
                JOIN orders o ON o.id=t.order_id WHERE a.base_url=? AND o.status='paid'
                AND t.state IN ('pending','retry') AND t.next_attempt_at<=? ORDER BY t.next_attempt_at,t.id LIMIT 1""",
                (self.base_url, now)).fetchone()
            if not job:
                return False
            job = dict(job)
            lease = secrets.token_hex(16)
            db.execute("UPDATE sub2api_tasks SET state='processing',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE id=?",
                       (lease, now + 180, now, job["id"]))
        result, failure = None, None
        try:
            if job["authorized_until"] <= now or job["auth_state"] != "ready" or not job["token_cipher"]:
                raise IntegrationError("reauth", "登录已过期，请原账户从 sub2api 重新进入")
            try:
                token = self.cipher.decrypt(job["token_cipher"].encode()).decode()
            except (InvalidToken, ValueError, UnicodeError):
                raise IntegrationError("reauth", "授权凭据无法读取，请原账户重新进入") from None
            if job["attempts"] > 0 or job["uncertain"]:
                result = self.client.history_receipt(token, job["code"], job["user_id"])
            if result is None:
                try:
                    result = self.client.redeem(token, job["code"], job["user_id"])
                except IntegrationError as exc:
                    if exc.kind != "used":
                        raise
                    result = self.client.history_receipt(token, job["code"], job["user_id"])
                    if result is None:
                        raise IntegrationError("failed", "兑换码已使用，但未在原账户记录中确认到账，请联系店主", uncertain=True)
        except IntegrationError as exc:
            failure = exc
        except Exception:
            # Protocol/programming surprises must never leak bearer tokens or card values.
            failure = IntegrationError("incompatible", "兑换处理异常，已保留任务，请管理员检查", uncertain=True)
        finished = int(time.time())
        with self.transaction() as db:
            live = db.execute("SELECT lease_token,state FROM sub2api_tasks WHERE id=?", (job["id"],)).fetchone()
            if live["state"] != "processing" or live["lease_token"] != lease:
                return True
            state, message, redeemed_at = "succeeded", "", None
            next_at, uncertain = 0, job["uncertain"]
            if failure:
                state, message = failure.kind, failure.message
                uncertain = int(bool(uncertain or failure.uncertain))
                if state == "retry":
                    next_at = finished + min(300, 5 * 2 ** min(job["attempts"], 6))
                if state == "reauth":
                    current = db.execute("SELECT credential_version FROM sub2api_accounts WHERE id=?", (job["account_id"],)).fetchone()
                    if current[0] != job["credential_version"]:
                        state, message, next_at = "retry", "已收到新授权，将继续核实", finished
                    else:
                        db.execute("UPDATE sub2api_accounts SET auth_state='reauth',token_cipher='' WHERE id=?", (job["account_id"],))
                        db.execute("""UPDATE sub2api_tasks SET state='reauth',last_error=?,updated_at=?
                            WHERE account_id=? AND state IN ('pending','retry')""", (message, finished, job["account_id"]))
                if state == "incompatible":
                    db.execute("UPDATE sub2api_runtime SET paused=1,message=? WHERE base_url=?", (message, self.base_url))
                    db.execute("""UPDATE sub2api_tasks SET state='incompatible',last_error=?,updated_at=?
                        WHERE state IN ('pending','retry') AND account_id IN
                        (SELECT id FROM sub2api_accounts WHERE base_url=?)""", (message, finished, self.base_url))
            else:
                redeemed_at = result["redeemed_at"]
            db.execute("""UPDATE sub2api_tasks SET state=?,last_error=?,next_attempt_at=?,uncertain=?,redeemed_at=?,
                lease_until=0,lease_token='',updated_at=? WHERE id=?""",
                (state, message, next_at, uncertain, redeemed_at, finished, job["id"]))
            db.execute("INSERT INTO audit(created_at,action,target,note) VALUES (?,?,?,?)",
                       (finished, "sub2api_" + state, job["order_id"], "任务 " + str(job["id"]) + "；用户 " + str(job["user_id"])))
        return True

    def register_routes(self, *, order_dict, expire, payload, page_offset, ip_hash, admin_required):
        app = self.app

        @app.post("/api/sub2api/session")
        def create_session():
            self.available()
            revision = self.snapshot()["revision"]
            data = payload()
            token, expected = data.get("token"), data.get("user_id")
            if not isinstance(token, str) or not 10 <= len(token) <= 16384 or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
                raise self.error("登录凭据格式不正确", "sub2api_reauth", 401)
            if expected is not None and (type(expected) is not int or expected <= 0):
                raise self.error("用户编号格式不正确")
            now = int(time.time())
            with self.transaction() as db:
                db.execute("DELETE FROM sub2api_attempts WHERE created_at<=?", (now - 300,))
                if db.execute("SELECT count(*) FROM sub2api_attempts WHERE ip_hash=?", (ip_hash(),)).fetchone()[0] >= 20:
                    raise self.error("身份验证过于频繁，请稍后再试", "rate_limited", 429)
                db.execute("INSERT INTO sub2api_attempts VALUES (?,?)", (ip_hash(), now))
            try:
                identity = self.client.identity(token)
            except IntegrationError as exc:
                raise self.api_error(exc) from None
            if expected is not None and expected != identity["id"]:
                raise self.error("链接用户与登录账户不一致，请从 sub2api 重新进入", "sub2api_identity_mismatch", 401)
            deadline = credential_deadline(token, now)
            if deadline <= now:
                raise self.error("登录已过期，请从 sub2api 重新进入", "sub2api_reauth", 401)
            handle, expiry = secrets.token_urlsafe(32), min(now + 8 * 3600, deadline)
            with self.transaction() as db:
                if self.read_config(db)["revision"] != revision:
                    raise self.error("对接配置已更新，请从 sub2api 重新进入", "sub2api_config_changed", 409)
                db.execute("""INSERT INTO sub2api_accounts(base_url,user_id,email,token_cipher,authorized_until,updated_at)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(base_url,user_id) DO UPDATE SET email=excluded.email,
                    token_cipher=excluded.token_cipher,authorized_until=excluded.authorized_until,
                    credential_version=sub2api_accounts.credential_version+1,auth_state='ready',updated_at=excluded.updated_at""",
                    (self.base_url, identity["id"], identity["email"], self.cipher.encrypt(token.encode()).decode(), deadline, now))
                account = db.execute("SELECT id FROM sub2api_accounts WHERE base_url=? AND user_id=?", (self.base_url, identity["id"])).fetchone()[0]
                db.execute("DELETE FROM sub2api_sessions WHERE expires_at<=?", (now,))
                db.execute("INSERT INTO sub2api_sessions VALUES (?,?,?)", (digest(handle), account, expiry))
                db.execute("""UPDATE sub2api_tasks SET state='retry',next_attempt_at=?,last_error='',updated_at=?
                    WHERE account_id=? AND state='reauth'""", (now, now, account))
            return jsonify(session_token=handle, expires_at=expiry, user=identity)

        @app.get("/api/sub2api/session")
        def read_session():
            with self.transaction() as db:
                account = self.session_account(db)
                return jsonify(user={"id": account["user_id"], "email": account["email"]}, needs_reauth=account["auth_state"] != "ready")

        @app.get("/api/sub2api/orders")
        def my_orders():
            offset = page_offset(request.args.get("offset", "0"), query=True)
            with self.transaction() as db:
                account = self.session_account(db)
                expire(db)
                rows = db.execute("SELECT * FROM orders WHERE sub2api_account_id=? ORDER BY created_at DESC,rowid DESC LIMIT 50 OFFSET ?", (account["id"], offset)).fetchall()
                total = db.execute("SELECT count(*) FROM orders WHERE sub2api_account_id=?", (account["id"],)).fetchone()[0]
                return jsonify(orders=[order_dict(db, r) for r in rows], total=total, has_more=offset + len(rows) < total)

        @app.post("/api/sub2api/orders/<order_id>/retry")
        def buyer_retry(order_id):
            with self.transaction() as db:
                account = self.session_account(db)
                row = db.execute("SELECT * FROM orders WHERE id=? AND sub2api_account_id=?", (order_id, account["id"])).fetchone()
                if not row:
                    raise self.error("订单不存在", "order_not_found", 404)
                self.retry_order(db, order_id)
                return jsonify(order=order_dict(db, row))

        @app.post("/api/admin/orders/<order_id>/redeem-retry")
        @admin_required
        def admin_retry(order_id):
            with self.transaction() as db:
                self.retry_order(db, order_id, administrator=True)
                row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
                return jsonify(order=order_dict(db, row))

        @app.get("/api/admin/sub2api")
        @admin_required
        def admin_diagnostics():
            with self.transaction() as db:
                return jsonify(sub2api=self.diagnostics(db))

        def config_url(data):
            value = data.get("base_url")
            if not isinstance(value, str) or len(value) > 2048:
                raise self.error("请填写 HTTPS 站点地址")
            try:
                return normalize_base_url(value.strip())
            except ValueError:
                raise self.error("地址必须是 HTTPS 站点首页，不能包含路径、参数或登录凭据") from None

        @app.post("/api/admin/sub2api")
        @admin_required
        def save_config():
            data = payload()
            base = config_url(data)
            enabled, relay = data.get("enabled"), data.get("relay_confirmed")
            if type(enabled) is not bool or type(relay) is not bool or type(data.get("revision")) is not int:
                raise self.error("配置格式不正确，请刷新管理页")
            if enabled and (not base or not relay):
                raise self.error("启用前请填写地址，并确认该站点允许服务器代验和代兑")
            with self.transaction() as db:
                old = self.read_config(db)
                if data["revision"] != old["revision"]:
                    raise self.error("配置已被其他页面修改，请重新加载配置后再保存", "sub2api_config_changed", 409)
                if base != old["base_url"]:
                    pending = db.execute("""SELECT 1 FROM orders o WHERE o.sub2api_auto_redeem=1 AND
                        (o.status IN ('pending','expired') OR (o.status='paid' AND
                        (NOT EXISTS(SELECT 1 FROM sub2api_tasks t WHERE t.order_id=o.id) OR
                         EXISTS(SELECT 1 FROM sub2api_tasks t WHERE t.order_id=o.id AND t.state<>'succeeded')))) LIMIT 1""").fetchone()
                    if pending:
                        raise self.error("仍有绑定账户的未完成订单或兑换任务，请处理完成后再更换站点", "sub2api_pending_tasks", 409)
                    db.execute("DELETE FROM sub2api_sessions")
                now = int(time.time())
                db.execute("UPDATE sub2api_config SET base_url=?,enabled=?,relay_confirmed=?,revision=revision+1,updated_at=? WHERE id=1",
                           (base, int(enabled), int(relay), now))
                if base:
                    db.execute("INSERT OR IGNORE INTO sub2api_runtime(base_url) VALUES (?)", (base,))
                db.execute("INSERT INTO audit(created_at,action,target,note) VALUES (?,?,?,?)",
                           (now, "sub2api_config", "1", "更新自动兑换配置；启用=" + str(enabled)))
                self.snapshot(db, refresh=True)
                return jsonify(sub2api=self.diagnostics(db))

        @app.post("/api/admin/sub2api/check")
        @admin_required
        def check_config():
            base = config_url(payload())
            if not base:
                raise self.error("请先填写 sub2api 站点地址")
            try:
                result = Sub2APIClient(base).check_connection()
            except IntegrationError as exc:
                raise self.error("连接检查未通过：" + exc.message, "sub2api_check_failed", 503) from None
            return jsonify(**result)
