"""Small card shop with administrator-confirmed payment and card delivery."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException


CHANNELS = {"alipay": "支付宝", "wechat": "微信支付"}
PAGE_SIZE = 50
MAX_PAYMENT_ADJUSTMENT_CENTS = 10
LEGACY_SUPPORT_NOTICE = "请保存订单号和查询密钥；订单过期后请勿付款。付款异常请联系店主。"
PACKAGES = {"alipay": "com.eg.android.AlipayGphone", "wechat": "com.tencent.mm"}

EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, device_id TEXT NOT NULL,
 channel TEXT NOT NULL, amount_cents INTEGER NOT NULL, occurred_at INTEGER NOT NULL,
 received_at INTEGER NOT NULL, source_package TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, order_id TEXT REFERENCES orders(id), resolved_note TEXT);
"""
EVENT_COLUMNS = (
    "id,fingerprint,device_id,channel,amount_cents,occurred_at,received_at,"
    "source_package,title,text,status,reason,order_id,resolved_note"
)


def parse_receipt(channel, title, text):
    """Recognize a narrow set of receipt notices, never generic money mentions."""
    titles = {"wechat": {"微信支付"}, "alipay": {"支付宝", "收款通知", "支付宝收款", "支付宝到账通知"}}
    if title not in titles.get(channel, set()):
        return None
    prefix = "微信支付收款" if channel == "wechat" else "(?:你已成功收款|支付宝成功收款|支付宝收款|成功收款)"
    match = re.fullmatch(prefix + r"\s*[¥￥]?\s*(\d{1,7})(?:\.(\d{1,2}))?\s*元[。！!]?", text.strip())
    if not match:
        return None
    cents = int(match[1]) * 100 + int((match[2] or "").ljust(2, "0"))
    return cents if 1 <= cents <= 1_000_000 else None
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 price_cents INTEGER NOT NULL CHECK(price_cents > 0), active INTEGER NOT NULL DEFAULT 1,
 created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cards (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
 code TEXT NOT NULL, code_hash TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','reserved','sold')),
 created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id),
 product_name TEXT NOT NULL, price_cents INTEGER NOT NULL, channel TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','paid','expired','cancelled')),
 card_id INTEGER REFERENCES cards(id), created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 paid_at INTEGER, delivery_mode TEXT, admin_note TEXT, client_ip_hash TEXT NOT NULL,
 contact TEXT NOT NULL DEFAULT '', contact_normalized TEXT NOT NULL DEFAULT '',
 quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity BETWEEN 1 AND 100),
 unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents > 0));
CREATE INDEX IF NOT EXISTS orders_recent ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS cards_available ON cards(product_id,status);
CREATE TABLE IF NOT EXISTS order_cards (
 order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 card_id INTEGER NOT NULL UNIQUE REFERENCES cards(id),
 position INTEGER NOT NULL CHECK(position >= 0),
 PRIMARY KEY(order_id,position));
CREATE TABLE IF NOT EXISTS lanes (
 channel TEXT NOT NULL, amount_cents INTEGER NOT NULL, blocked_until INTEGER NOT NULL,
 reason TEXT NOT NULL, PRIMARY KEY(channel,amount_cents));
CREATE TABLE IF NOT EXISTS devices (
 id TEXT PRIMARY KEY, last_seen INTEGER NOT NULL, listener_connected INTEGER NOT NULL);
""" + EVENTS_TABLE + """
CREATE INDEX IF NOT EXISTS events_recent ON events(received_at DESC);
CREATE INDEX IF NOT EXISTS events_fingerprint ON events(fingerprint);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, action TEXT NOT NULL,
 target TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS login_attempts (ip_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS login_attempt_time ON login_attempts(ip_hash,created_at);
CREATE TABLE IF NOT EXISTS admin_auth (
 id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL,
 session_version INTEGER NOT NULL CHECK(session_version > 0));
CREATE TABLE IF NOT EXISTS lookup_attempts (ip_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS lookup_attempt_time ON lookup_attempts(ip_hash,created_at);
"""


def normalize_contact(value):
    value = value.strip()
    return value.lower() if "@" in value else value


def initialize_admin_auth(db, bootstrap_hash):
    """Seed credentials once; existing database credentials always win."""
    db.execute("BEGIN IMMEDIATE")
    try:
        if not db.execute("SELECT 1 FROM admin_auth WHERE id=1").fetchone():
            if not isinstance(bootstrap_hash, str) or not bootstrap_hash.startswith(("scrypt:", "pbkdf2:sha256:")):
                raise RuntimeError("请配置 ADMIN_PASSWORD_HASH；运行 scripts/setup.py 创建管理员凭据。")
            db.execute("INSERT INTO admin_auth(id,password_hash,session_version) VALUES (1,?,?)",
                       (bootstrap_hash, secrets.randbits(62) + 1))
        db.commit()
    except Exception:
        db.rollback()
        raise


def migrate_shop_schema(db):
    """Keep existing orders and serialize repeatable upgrades across workers."""
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
        for name in ("contact", "contact_normalized"):
            if name not in columns:
                db.execute(f"ALTER TABLE orders ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        for row in db.execute("SELECT id,contact FROM orders WHERE contact<>'' AND contact_normalized=''").fetchall():
            db.execute("UPDATE orders SET contact_normalized=? WHERE id=?", (normalize_contact(row[1]), row[0]))
        db.execute("CREATE INDEX IF NOT EXISTS orders_contact ON orders(contact_normalized,created_at DESC)")
        if "quantity" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity BETWEEN 1 AND 100)")
        if "unit_price_cents" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN unit_price_cents INTEGER")
        db.execute("UPDATE orders SET unit_price_cents=price_cents WHERE unit_price_cents IS NULL")
        db.execute("""INSERT INTO order_cards(order_id,card_id,position)
            SELECT o.id,o.card_id,0 FROM orders o
            WHERE o.quantity=1 AND o.status IN ('pending','paid') AND o.card_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM order_cards oc WHERE oc.order_id=o.id)""")
        db.execute("DROP INDEX IF EXISTS one_pending_amount")
        # Non-unique so upgrades preserve previously created equal-amount orders.
        db.execute("CREATE INDEX IF NOT EXISTS pending_payment_amount ON orders(channel,price_cents) WHERE status='pending'")
        db.execute("INSERT INTO settings(key,value) VALUES ('auto_delivery','false') ON CONFLICT(key) DO UPDATE SET value='false'")
        notice = db.execute("SELECT value FROM settings WHERE key='support_notice'").fetchone()
        if notice and json.loads(notice[0]) == LEGACY_SUPPORT_NOTICE:
            db.execute("UPDATE settings SET value=? WHERE key='support_notice'", (json.dumps(""),))
        db.commit()
    except Exception:
        db.rollback()
        raise


def migrate_event_schema(db):
    """Remove the old fingerprint uniqueness constraint without losing receipts.

    BEGIN IMMEDIATE also serializes startup migration across Gunicorn workers.
    Exact event IDs remain unique; equal receipt text is only a review signal.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        unique_fingerprint = False
        for index in db.execute("PRAGMA index_list(events)").fetchall():
            if not index[2]:
                continue
            quoted_name = '"' + index[1].replace('"', '""') + '"'
            columns = [column[2] for column in db.execute("PRAGMA index_info(" + quoted_name + ")")]
            if columns == ["fingerprint"]:
                unique_fingerprint = True
                break
        if unique_fingerprint:
            db.execute(EVENTS_TABLE.replace("IF NOT EXISTS events", "events_migrating"))
            db.execute("INSERT INTO events_migrating (" + EVENT_COLUMNS + ") SELECT " + EVENT_COLUMNS + " FROM events ORDER BY rowid")
            db.execute("DROP TABLE events")
            db.execute("ALTER TABLE events_migrating RENAME TO events")
        db.execute("CREATE INDEX IF NOT EXISTS events_recent ON events(received_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS events_fingerprint ON events(fingerprint)")
        # Repair historical links that are ruled out by the recorded timestamps.
        # Preserve the receipt, status and notes; never issue cards during migration.
        repaired = db.execute("""UPDATE events SET order_id=NULL
            WHERE order_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM orders o WHERE o.id=events.order_id
                AND (events.occurred_at<o.created_at OR events.occurred_at>=o.expires_at))""").rowcount
        if repaired:
            db.execute("INSERT INTO audit(created_at,action,target,note) VALUES (?,?,?,?)",
                       (int(time.time()), "invalid_event_links_cleared", "events", str(repaired)))
        db.commit()
    except Exception:
        db.rollback()
        raise


class APIError(Exception):
    def __init__(self, message, code="invalid_request", status=400):
        self.message, self.code, self.status = message, code, status


def create_app(test_config=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(
        DATA_DIR=os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")),
        SECRET_KEY=os.environ.get("SECRET_KEY", ""),
        ADMIN_PASSWORD_HASH=os.environ.get("ADMIN_PASSWORD_HASH", ""),
        DEVICE_TOKEN=os.environ.get("DEVICE_TOKEN", ""),
        HERMES_API_TOKEN=os.environ.get("HERMES_API_TOKEN", ""),
        SUB2API_BASE_URL=os.environ.get("SUB2API_BASE_URL", ""),
        SUB2API_ENABLED=os.environ.get("SUB2API_ENABLED", "0") == "1",
        SUB2API_RELAY_CONFIRMED=os.environ.get("SUB2API_RELAY_CONFIRMED", "0") == "1",
        SUB2API_TOKEN_KEY=os.environ.get("SUB2API_TOKEN_KEY", ""),
        SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES", "true").lower() == "true",
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=8 * 3600,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        ORDER_TTL_SECONDS=int(os.environ.get("ORDER_TTL_SECONDS", "1800")),
        SHOP_NAME=os.environ.get("SHOP_NAME", "自用小店"),
        AUTO_DELIVERY=False,
        TRUST_PROXY=os.environ.get("TRUST_PROXY", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)
    app.config["AUTO_DELIVERY"] = False
    for key in ("SECRET_KEY",):
        if len(app.config[key]) < 32:
            raise RuntimeError(f"{key} 必须配置为至少32个字符的随机密钥。请先运行 scripts/setup.py。")
    if not 60 <= app.config["ORDER_TTL_SECONDS"] <= 1800:
        raise RuntimeError("ORDER_TTL_SECONDS 必须在60至1800秒之间。")
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_prefix=0)
    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (data_dir / "qr").mkdir(mode=0o700, exist_ok=True)
    app.config["DATABASE"] = str(data_dir / "shop.sqlite3")

    def connect():
        db = sqlite3.connect(app.config["DATABASE"], timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def transaction():
        db = connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with closing(connect()) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        initialize_admin_auth(db, app.config["ADMIN_PASSWORD_HASH"])
        migrate_shop_schema(db)
        migrate_event_schema(db)
        defaults = {"shop_name": app.config["SHOP_NAME"], "auto_delivery": False, "support_notice": ""}
        for key, value in defaults.items():
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
    try:
        os.chmod(app.config["DATABASE"], 0o600)
    except OSError:
        pass

    from lightcard_sub2api import Sub2APIIntegration
    sub2api = Sub2APIIntegration(app, transaction, APIError)
    app.extensions["sub2api"] = sub2api

    def now():
        return int(time.time())

    def settings(db):
        result = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}
        result["auto_delivery"] = False
        return result

    def audit(db, action, target="", note=""):
        db.execute("INSERT INTO audit(created_at,action,target,note) VALUES (?,?,?,?)", (now(), action, str(target), note))

    def ip_hash():
        return hmac.new(app.secret_key.encode(), (request.remote_addr or "unknown").encode(), hashlib.sha256).hexdigest()

    def release_order_cards(db, row):
        db.execute("""UPDATE cards SET status='available' WHERE status='reserved' AND id IN
                      (SELECT card_id FROM order_cards WHERE order_id=?)""", (row["id"],))
        db.execute("DELETE FROM order_cards WHERE order_id=?", (row["id"],))
        db.execute("UPDATE orders SET card_id=NULL WHERE id=?", (row["id"],))

    def expire(db):
        for row in db.execute("SELECT * FROM orders WHERE status='pending' AND expires_at<=?", (now(),)).fetchall():
            release_order_cards(db, row)
            db.execute("UPDATE orders SET status='expired' WHERE id=?", (row["id"],))
            audit(db, "order_expired", row["id"])

    def qr_path(channel):
        for ext in ("png", "jpg", "webp"):
            path = data_dir / "qr" / f"{channel}.{ext}"
            if path.is_file():
                return path
        return None

    def channels():
        return [{"id": key, "label": label, "enabled": qr_path(key) is not None} for key, label in CHANNELS.items()]

    def product_rows(db, active=False):
        query = """SELECT p.*, (SELECT count(*) FROM cards c WHERE c.product_id=p.id AND c.status='available') stock
                   FROM products p""" + (" WHERE p.active=1" if active else "") + " ORDER BY p.id"
        return [dict(r, active=bool(r["active"]), sub2api_redeem=bool(r["sub2api_redeem"])) for r in db.execute(query)]

    def order_dict(db, row):
        keys = ("id", "product_name", "channel", "price_cents", "unit_price_cents", "quantity", "status", "created_at", "expires_at", "paid_at", "delivery_mode", "contact")
        result = {key: row[key] for key in keys}
        result["base_price_cents"] = row["unit_price_cents"] * row["quantity"]
        result["payment_adjustment_cents"] = row["price_cents"] - result["base_price_cents"]
        result["delivery_mode"] = row["delivery_mode"] or "manual"
        result["qr_url"] = f"/qr/{row['channel']}"
        result["redemption"] = sub2api.describe(db, row)
        if row["status"] == "paid":
            codes = [card["code"] for card in db.execute("""SELECT c.code FROM order_cards oc
                     JOIN cards c ON c.id=oc.card_id WHERE oc.order_id=? ORDER BY oc.position""", (row["id"],))]
            result["card_codes"] = codes
            result["card_code"] = "\n".join(codes)
        return result

    def get_order(db, order_id, buyer=False):
        row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if buyer:
            authorization = request.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            valid = row and hmac.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            if not valid:
                raise APIError("订单号或查询密钥不正确", "order_not_found", 404)
        elif not row:
            raise APIError("订单不存在", "order_not_found", 404)
        return row

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise APIError("请求必须是JSON对象")
        return value

    def string(value, label, maximum, required=True):
        if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
            raise APIError(f"{label}不能为空且不得超过{maximum}个字符")
        return value.strip()

    def amount(value):
        if type(value) is not int or not 1 <= value <= 1_000_000:
            raise APIError("金额必须是1至1000000之间的整数分")
        return value

    def contact_value(value):
        value = string(value, "联系方式", 128)
        if len(value) < 3:
            raise APIError("联系方式至少需要3个字符")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise APIError("联系方式不能包含换行或控制字符")
        return value

    def page_offset(value=0, query=False):
        if query and isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
            value = int(value)
        if type(value) is not int or not 0 <= value <= 1_000_000_000:
            raise APIError("分页位置不正确")
        return value

    def card_dict(row):
        return {key: row[key] for key in ("id", "product_id", "code", "status", "created_at")}

    def current_admin_session(db=None):
        version = session.get("session_version")
        if session.get("admin") is not True or type(version) is not int or not session.get("csrf"):
            return False
        if db is None:
            with closing(connect()) as connection:
                row = connection.execute("SELECT session_version FROM admin_auth WHERE id=1").fetchone()
        else:
            row = db.execute("SELECT session_version FROM admin_auth WHERE id=1").fetchone()
        return bool(row and row["session_version"] == version)

    def record_password_attempt(address):
        # Commit before password verification so failed requests still count.
        with transaction() as db:
            db.execute("DELETE FROM login_attempts WHERE created_at<?", (now()-900,))
            if db.execute("SELECT count(*) FROM login_attempts WHERE ip_hash=?", (address,)).fetchone()[0] >= 10:
                raise APIError("验证失败次数过多，请15分钟后再试", "rate_limited", 429)
            db.execute("INSERT INTO login_attempts VALUES (?,?)", (address, now()))

    def admin_required(func):
        @wraps(func)
        def inner(*args, **kwargs):
            if not current_admin_session():
                session.clear()
                raise APIError("请先登录管理后台", "unauthorized", 401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                supplied = request.headers.get("X-CSRF-Token", "")
                if not supplied or not hmac.compare_digest(supplied, session.get("csrf", "")):
                    raise APIError("会话校验失败，请重新登录", "csrf_failed", 403)
            return func(*args, **kwargs)
        return inner

    def deliver(db, row, mode, note=""):
        if row["status"] == "paid":
            return
        if row["status"] == "cancelled":
            raise APIError("已取消订单不能发卡，请人工处理退款或补发", "order_cancelled", 409)
        cards = []
        if row["status"] == "pending":
            cards = db.execute("""SELECT c.id FROM order_cards oc JOIN cards c ON c.id=oc.card_id
                WHERE oc.order_id=? AND c.status='reserved' AND c.product_id=? ORDER BY oc.position""",
                (row["id"], row["product_id"])).fetchall()
        missing = row["quantity"] - len(cards)
        if missing > 0:
            cards.extend(db.execute("SELECT id FROM cards WHERE product_id=? AND status='available' ORDER BY id LIMIT ?",
                                    (row["product_id"], missing)).fetchall())
        if len(cards) != row["quantity"]:
            raise APIError("库存不足，请补充卡密后再次确认，或线下退款", "out_of_stock", 409)
        db.execute("DELETE FROM order_cards WHERE order_id=?", (row["id"],))
        db.executemany("INSERT INTO order_cards(order_id,card_id,position) VALUES (?,?,?)",
                       [(row["id"], card["id"], position) for position, card in enumerate(cards)])
        db.executemany("UPDATE cards SET status='sold' WHERE id=?", [(card["id"],) for card in cards])
        db.execute("UPDATE orders SET status='paid',card_id=?,paid_at=?,delivery_mode=?,admin_note=? WHERE id=?",
                   (cards[0]["id"], now(), mode, note, row["id"]))
        audit(db, "order_delivered_" + mode, row["id"], note)
        sub2api.enqueue(db, row["id"])

    def cancel(db, row, note, allow_expired=False):
        if allow_expired and row["status"] == "cancelled":
            return
        if row["status"] != "pending" and not (allow_expired and row["status"] == "expired"):
            raise APIError("该订单不可取消", "not_pending", 409)
        release_order_cards(db, row)
        db.execute("UPDATE orders SET status='cancelled',card_id=NULL,admin_note=? WHERE id=?", (note, row["id"]))
        audit(db, "order_cancelled", row["id"], note)

    @app.errorhandler(APIError)
    def api_error(exc):
        return jsonify(error={"code": exc.code, "message": exc.message}), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error={"code": str(exc.code), "message": "请求过大" if exc.code == 413 else exc.description}), exc.code

    @app.errorhandler(sqlite3.OperationalError)
    def database_busy(exc):
        app.logger.error("Database operation failed: %s", type(exc).__name__)
        return jsonify(error={"code": "database_unavailable", "message": "服务暂忙，请稍后再试"}), 503

    @app.after_request
    def secure_response(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.path == "/" and sub2api.base_url:
            response.headers.pop("X-Frame-Options", None)
            response.headers["Content-Security-Policy"] = response.headers["Content-Security-Policy"].replace(
                "frame-ancestors 'none'", "frame-ancestors " + sub2api.base_url)
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.path.startswith("/api/") or request.path == "/admin":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def storefront():
        return app.send_static_file("index.html")

    @app.get("/admin")
    def admin_page():
        return app.send_static_file("admin.html")

    @app.get("/healthz")
    def health():
        with closing(connect()) as db:
            db.execute("SELECT 1").fetchone()
        return jsonify(ok=True)

    @app.get("/qr/<channel>")
    def serve_qr(channel):
        if channel not in CHANNELS or not qr_path(channel):
            raise APIError("尚未配置收款码", "qr_not_configured", 404)
        response = send_file(qr_path(channel), conditional=True)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/products")
    def public_products():
        with transaction() as db:
            expire(db)
            config = settings(db)
            return jsonify(products=product_rows(db, active=True), channels=channels(),
                           sub2api_enabled=sub2api.enabled,
                           order_ttl_seconds=app.config["ORDER_TTL_SECONDS"], **config)

    @app.post("/api/orders")
    def create_order():
        data = payload()
        if type(data.get("product_id")) is not int or data.get("channel") not in CHANNELS:
            raise APIError("请选择商品和支付方式")
        contact = contact_value(data.get("contact"))
        quantity = data.get("quantity", 1)
        if type(quantity) is not int or not 1 <= quantity <= 100:
            raise APIError("购买数量必须是1至100的整数", "invalid_quantity")
        channel = data["channel"]
        if qr_path(channel) is None:
            raise APIError("店主尚未上传此渠道收款码", "qr_not_configured", 409)
        with transaction() as db:
            expire(db)
            product = db.execute("SELECT * FROM products WHERE id=? AND active=1", (data["product_id"],)).fetchone()
            if not product:
                raise APIError("商品不存在或已下架", "product_not_found", 404)
            if db.execute("SELECT count(*) FROM orders WHERE client_ip_hash=? AND created_at>?", (ip_hash(), now()-900)).fetchone()[0] >= 20:
                raise APIError("下单过于频繁，请稍后再试", "rate_limited", 429)
            cards = db.execute("SELECT id FROM cards WHERE product_id=? AND status='available' ORDER BY id LIMIT ?", (product["id"], quantity)).fetchall()
            if len(cards) != quantity:
                raise APIError("库存不足", "out_of_stock", 409)
            base_price = product["price_cents"] * quantity
            # BEGIN IMMEDIATE serializes allocation and reservation across workers.
            occupied = {row[0] for row in db.execute("""SELECT price_cents FROM orders
                WHERE channel=? AND status='pending' AND price_cents BETWEEN ? AND ?""",
                (channel, base_price, base_price + MAX_PAYMENT_ADJUSTMENT_CENTS))}
            payable = next((base_price + offset for offset in range(MAX_PAYMENT_ADJUSTMENT_CENTS + 1)
                            if base_price + offset not in occupied), None)
            if payable is None:
                raise APIError("当前金额的待付款订单较多，请稍后重新下单（最多加价0.10元）", "payment_amount_busy", 409)
            order_id, token = secrets.token_hex(12), secrets.token_urlsafe(32)
            created = now()
            db.execute("""INSERT INTO orders(id,token_hash,product_id,product_name,price_cents,channel,status,card_id,
                created_at,expires_at,client_ip_hash,contact,contact_normalized,quantity,unit_price_cents)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (order_id, hashlib.sha256(token.encode()).hexdigest(), product["id"], product["name"], payable, channel,
                        "pending", cards[0]["id"], created, created + app.config["ORDER_TTL_SECONDS"], ip_hash(), contact, normalize_contact(contact), quantity, product["price_cents"]))
            sub2api.bind_order(db, order_id, product)
            db.executemany("INSERT INTO order_cards(order_id,card_id,position) VALUES (?,?,?)",
                           [(order_id, card["id"], position) for position, card in enumerate(cards)])
            db.executemany("UPDATE cards SET status='reserved' WHERE id=?", [(card["id"],) for card in cards])
            if app.config["HERMES_API_TOKEN"]:
                db.execute("INSERT INTO hermes_notifications(order_id,created_at) VALUES (?,?)", (order_id, created))
            return jsonify(order=order_dict(db, get_order(db, order_id)), token=token), 201

    @app.post("/api/orders/lookup")
    def lookup_orders():
        data = payload()
        contact = normalize_contact(contact_value(data.get("contact")))
        offset = page_offset(data.get("offset", 0))
        with transaction() as db:
            address, timestamp = ip_hash(), now()
            db.execute("DELETE FROM lookup_attempts WHERE created_at<=?", (timestamp - 60,))
            if db.execute("SELECT count(*) FROM lookup_attempts WHERE ip_hash=?", (address,)).fetchone()[0] >= 30:
                raise APIError("查询过于频繁，请稍后再试", "rate_limited", 429)
            db.execute("INSERT INTO lookup_attempts VALUES (?,?)", (address, timestamp))
            expire(db)
            total = db.execute("SELECT count(*) FROM orders WHERE contact_normalized=?", (contact,)).fetchone()[0]
            rows = db.execute("SELECT * FROM orders WHERE contact_normalized=? ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                              (contact, PAGE_SIZE, offset)).fetchall()
            return jsonify(orders=[order_dict(db, row) for row in rows], total=total, has_more=offset + len(rows) < total)

    @app.get("/api/orders/<order_id>")
    def read_order(order_id):
        with transaction() as db:
            expire(db)
            return jsonify(order=order_dict(db, get_order(db, order_id, buyer=True)))

    @app.post("/api/orders/<order_id>/cancel")
    def buyer_cancel(order_id):
        with transaction() as db:
            expire(db)
            row = get_order(db, order_id, buyer=True)
            cancel(db, row, "买家主动取消")
            return jsonify(order=order_dict(db, get_order(db, order_id)))

    @app.post("/api/admin/login")
    def login():
        data = payload()
        password = data.get("password", "")
        if not isinstance(password, str) or len(password) > 1024:
            raise APIError("密码格式不正确")
        address = ip_hash()
        record_password_attempt(address)
        with transaction() as db:
            credentials = db.execute("SELECT password_hash,session_version FROM admin_auth WHERE id=1").fetchone()
            if not credentials or not check_password_hash(credentials["password_hash"], password):
                raise APIError("管理员密码不正确", "invalid_credentials", 401)
            version = credentials["session_version"]
            db.execute("DELETE FROM login_attempts WHERE ip_hash=?", (address,))
            audit(db, "admin_login")
        session.clear()
        session["admin"], session["csrf"], session["session_version"] = True, secrets.token_urlsafe(32), version
        session.permanent = True
        return jsonify(csrf_token=session["csrf"])

    @app.get("/api/admin/session")
    def admin_session():
        if current_admin_session():
            return jsonify(authenticated=True, csrf_token=session["csrf"])
        session.clear()
        return jsonify(authenticated=False)

    @app.post("/api/admin/password")
    @admin_required
    def change_admin_password():
        data = payload()
        current = data.get("current_password")
        new = data.get("new_password")
        confirmation = data.get("confirm_password")
        if not isinstance(current, str) or not current or len(current) > 1024:
            raise APIError("请输入当前密码")
        if not isinstance(new, str) or not 12 <= len(new) <= 256 or not new.strip():
            raise APIError("新密码须为12至256个字符且不能全为空白", "invalid_new_password")
        if not isinstance(confirmation, str) or new != confirmation:
            raise APIError("两次输入的新密码不一致", "password_mismatch")
        address = ip_hash()
        record_password_attempt(address)
        with transaction() as db:
            # Recheck after acquiring the write lock; another worker may have
            # changed the password since the request's initial session check.
            if not current_admin_session(db):
                session.clear()
                raise APIError("请重新登录管理后台", "unauthorized", 401)
            credentials = db.execute("SELECT password_hash FROM admin_auth WHERE id=1").fetchone()
            if not check_password_hash(credentials["password_hash"], current):
                raise APIError("当前密码不正确", "invalid_current_password")
            password_hash = generate_password_hash(new)
            db.execute("UPDATE admin_auth SET password_hash=?,session_version=session_version+1 WHERE id=1", (password_hash,))
            db.execute("DELETE FROM login_attempts WHERE ip_hash=?", (address,))
            audit(db, "admin_password_changed")
        session.clear()
        return jsonify(ok=True)

    @app.post("/api/admin/logout")
    @admin_required
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/admin/overview")
    @admin_required
    def overview():
        with transaction() as db:
            expire(db)
            stats = {state: db.execute("SELECT count(*) FROM orders WHERE status=?", (state,)).fetchone()[0] for state in ("pending", "paid")}
            stats["stock"] = db.execute("SELECT count(*) FROM cards WHERE status='available'").fetchone()[0]
            return jsonify(stats=stats, settings=settings(db), channels=channels(),
                           sub2api=sub2api.diagnostics(db),
                           orders=[order_dict(db, r) for r in db.execute("SELECT * FROM orders ORDER BY (status='pending') DESC,created_at DESC,rowid DESC LIMIT 50")])

    @app.get("/api/admin/orders")
    @admin_required
    def admin_orders():
        query = string(request.args.get("query", ""), "查询内容", 128, required=False)
        status = request.args.get("status", "all")
        if status not in ("all", "pending", "paid", "expired", "cancelled"):
            raise APIError("订单状态不正确")
        offset = page_offset(request.args.get("offset", "0"), query=True)
        clauses, params = [], []
        if query:
            clauses.append("(id=? OR contact_normalized=?)")
            params.extend((query, normalize_contact(query)))
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with transaction() as db:
            expire(db)
            total = db.execute("SELECT count(*) FROM orders" + where, params).fetchone()[0]
            rows = db.execute("SELECT * FROM orders" + where + " ORDER BY (status='pending') DESC,created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                              (*params, PAGE_SIZE, offset)).fetchall()
            return jsonify(orders=[order_dict(db, row) for row in rows], total=total, has_more=offset + len(rows) < total)

    @app.get("/api/admin/orders/<order_id>")
    @admin_required
    def admin_order_lookup(order_id):
        with transaction() as db:
            expire(db)
            return jsonify(order=order_dict(db, get_order(db, order_id)))

    @app.get("/api/admin/products")
    @admin_required
    def admin_products():
        with transaction() as db:
            expire(db)
            return jsonify(products=product_rows(db))

    def product_data(data, partial=False):
        result = {}
        for key, limit in (("name", 100), ("description", 1000)):
            if key in data or not partial:
                result[key] = string(data.get(key, ""), "商品名称" if key == "name" else "描述", limit, required=key == "name")
        if "price_cents" in data or not partial:
            result["price_cents"] = amount(data.get("price_cents"))
        if "active" in data or not partial:
            value = data.get("active", True)
            if type(value) is not bool:
                raise APIError("active必须为布尔值")
            result["active"] = int(value)
        if "sub2api_redeem" in data or not partial:
            value = data.get("sub2api_redeem", False)
            if type(value) is not bool:
                raise APIError("sub2api_redeem必须为布尔值")
            result["sub2api_redeem"] = int(value)
        return result

    @app.post("/api/admin/products")
    @admin_required
    def add_product():
        fields = product_data(payload())
        with transaction() as db:
            cur = db.execute("INSERT INTO products(name,description,price_cents,active,sub2api_redeem,created_at) VALUES (?,?,?,?,?,?)",
                             (fields["name"], fields["description"], fields["price_cents"], fields["active"], fields["sub2api_redeem"], now()))
            audit(db, "product_created", cur.lastrowid)
            return jsonify(product=next(p for p in product_rows(db) if p["id"] == cur.lastrowid)), 201

    @app.patch("/api/admin/products/<int:product_id>")
    @admin_required
    def update_product(product_id):
        fields = product_data(payload(), partial=True)
        if not fields:
            raise APIError("没有可更新的字段")
        with transaction() as db:
            if not db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                raise APIError("商品不存在", "product_not_found", 404)
            # Keys come exclusively from the static product_data whitelist.
            db.execute("UPDATE products SET " + ",".join(key+"=?" for key in fields) + " WHERE id=?", (*fields.values(), product_id))
            audit(db, "product_updated", product_id)
            return jsonify(product=next(p for p in product_rows(db) if p["id"] == product_id))

    @app.post("/api/admin/products/<int:product_id>/cards")
    @admin_required
    def import_cards(product_id):
        codes = payload().get("codes")
        if not isinstance(codes, str) or len(codes) > 1_000_000:
            raise APIError("请按每行一张卡填写，单次最多1MB")
        lines = [line.strip() for line in codes.splitlines() if line.strip()]
        if not lines or len(lines) > 10000 or any(len(line) > 4000 for line in lines):
            raise APIError("单次需导入1至10000张卡，每行不得超过4000字符")
        inserted = 0
        with transaction() as db:
            if not db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                raise APIError("商品不存在", "product_not_found", 404)
            for line in lines:
                cur = db.execute("INSERT OR IGNORE INTO cards(product_id,code,code_hash,created_at) VALUES (?,?,?,?)",
                                 (product_id, line, hashlib.sha256(line.encode()).hexdigest(), now()))
                inserted += cur.rowcount
            audit(db, "cards_imported", product_id, f"导入{inserted}张，重复{len(lines)-inserted}张")
        return jsonify(inserted=inserted, duplicates=len(lines)-inserted)

    @app.get("/api/admin/products/<int:product_id>/cards")
    @admin_required
    def list_cards(product_id):
        status = request.args.get("status", "all")
        if status not in ("all", "available", "reserved", "sold"):
            raise APIError("库存状态不正确")
        offset = page_offset(request.args.get("offset", "0"), query=True)
        where = " WHERE product_id=?" + (" AND status=?" if status != "all" else "")
        params = (product_id, status) if status != "all" else (product_id,)
        with transaction() as db:
            expire(db)
            if not db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                raise APIError("商品不存在", "product_not_found", 404)
            total = db.execute("SELECT count(*) FROM cards" + where, params).fetchone()[0]
            rows = db.execute("SELECT * FROM cards" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", (*params, PAGE_SIZE, offset)).fetchall()
            return jsonify(cards=[card_dict(row) for row in rows], total=total, has_more=offset + len(rows) < total)

    def available_card(db, card_id):
        row = db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not row:
            raise APIError("卡密不存在", "card_not_found", 404)
        if row["status"] != "available":
            raise APIError("仅未售出的可用卡密可以修改或删除", "card_not_available", 409)
        return row

    @app.patch("/api/admin/cards/<int:card_id>")
    @admin_required
    def update_card(card_id):
        code = string(payload().get("code"), "卡密", 4000)
        if "\n" in code or "\r" in code:
            raise APIError("每张卡密只能占一行")
        digest = hashlib.sha256(code.encode()).hexdigest()
        with transaction() as db:
            expire(db)
            available_card(db, card_id)
            if db.execute("SELECT 1 FROM cards WHERE code_hash=? AND id<>?", (digest, card_id)).fetchone():
                raise APIError("卡密已存在", "duplicate_card", 409)
            db.execute("UPDATE cards SET code=?,code_hash=? WHERE id=?", (code, digest, card_id))
            audit(db, "card_updated", card_id)
            return jsonify(card=card_dict(db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()))

    @app.delete("/api/admin/cards/<int:card_id>")
    @admin_required
    def delete_card(card_id):
        with transaction() as db:
            expire(db)
            available_card(db, card_id)
            db.execute("DELETE FROM cards WHERE id=?", (card_id,))
            audit(db, "card_deleted", card_id)
            return jsonify(ok=True)

    @app.post("/api/admin/orders/<order_id>/confirm")
    @admin_required
    def confirm_order(order_id):
        with transaction() as db:
            expire(db)
            deliver(db, get_order(db, order_id), "manual", "管理员确认发卡")
            return jsonify(order=order_dict(db, get_order(db, order_id)))

    @app.post("/api/admin/orders/<order_id>/cancel")
    @admin_required
    def cancel_order(order_id):
        data = payload() if request.get_data() else {}
        note = string(data.get("note", ""), "取消备注", 500, required=False) or "管理员取消订单"
        with transaction() as db:
            expire(db)
            cancel(db, get_order(db, order_id), note, allow_expired=True)
            return jsonify(order=order_dict(db, get_order(db, order_id)))

    @app.post("/api/admin/events/<event_id>/resolve")
    @app.post("/api/admin/lanes/unlock")
    @admin_required
    def retired_payment_admin(event_id=None):
        raise APIError("自动收款功能已停用", "feature_retired", 410)

    @app.post("/api/admin/settings")
    @admin_required
    def update_settings():
        data = payload()
        fields = {}
        for key, maxlen in (("shop_name", 80), ("support_notice", 1000)):
            if key in data:
                fields[key] = string(data[key], key, maxlen, required=key == "shop_name")
        if "auto_delivery" in data:
            if type(data["auto_delivery"]) is not bool:
                raise APIError("auto_delivery必须为布尔值")
            if data["auto_delivery"]:
                raise APIError("仅支持后台确认发卡", "manual_delivery_only", 409)
            fields["auto_delivery"] = False
        with transaction() as db:
            for key, value in fields.items():
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value, ensure_ascii=False), key))
            audit(db, "settings_updated", note=",".join(fields))
            return jsonify(settings=settings(db))

    @app.post("/api/admin/qr/<channel>")
    @admin_required
    def upload_qr(channel):
        if channel not in CHANNELS:
            raise APIError("支付方式不正确")
        file = request.files.get("file")
        if not file:
            raise APIError("请选择图片文件")
        content = file.read()
        # Allow raster image formats only; do not use the submitted filename/path.
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif content.startswith(b"\xff\xd8\xff"):
            ext = "jpg"
        elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            ext = "webp"
        else:
            raise APIError("仅接受PNG、JPEG、WebP图片")
        with transaction() as db:
            expire(db)
            if db.execute("SELECT 1 FROM orders WHERE channel=? AND status='pending'", (channel,)).fetchone():
                raise APIError("该渠道仍有待付款订单，请等待完成后再更换收款码", "channel_busy", 409)
            target = data_dir / "qr" / f"{channel}.{ext}"
            tmp = data_dir / "qr" / (secrets.token_hex(12) + ".tmp")
            try:
                with open(tmp, "xb") as handle:
                    handle.write(content)
                os.chmod(tmp, 0o600)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            for other in ("png", "jpg", "webp"):
                if other != ext:
                    (data_dir / "qr" / f"{channel}.{other}").unlink(missing_ok=True)
            audit(db, "qr_updated", channel)
        return jsonify(ok=True)

    @app.post("/api/device/heartbeat")
    @app.post("/api/device/events")
    def retired_device_endpoint():
        raise APIError("手机监听功能已停用，请在后台确认发卡", "feature_retired", 410)

    from lightcard_hermes import register_hermes_routes
    sub2api.register_routes(order_dict=order_dict, expire=expire, payload=payload,
        page_offset=page_offset, ip_hash=ip_hash, admin_required=admin_required)
    register_hermes_routes(app, transaction=transaction, expire=expire, product_rows=product_rows,
        get_order=get_order, order_dict=order_dict, deliver=deliver, audit=audit,
        import_cards=import_cards.__wrapped__, error=APIError)
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=False)
