"""Private, narrowly scoped Hermes integration; nginx must deny /_hermes/."""
import hmac
import time
from functools import wraps

from flask import jsonify, request


def register_hermes_routes(app, *, transaction, expire, product_rows, get_order,
                           order_dict, deliver, audit, import_cards, error):
    with transaction() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS hermes_notifications (
            id INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
            created_at INTEGER NOT NULL, sent_at INTEGER,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')""")
        db.execute("CREATE INDEX IF NOT EXISTS hermes_notifications_due ON hermes_notifications(state,next_attempt_at)")

    def require_token(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            expected = app.config.get("HERMES_API_TOKEN", "")
            supplied = request.headers.get("Authorization", "")
            if len(expected) < 32 or not hmac.compare_digest(supplied, "Bearer " + expected):
                raise error("机器人接口未授权", "unauthorized", 401)
            return fn(*args, **kwargs)
        return wrapped

    def safe_order(db, row):
        # Never return card secrets to the language model or notification worker.
        result = order_dict(db, row)
        result.pop("card_codes", None)
        result.pop("card_code", None)
        return result

    @app.after_request
    def private_headers(response):
        if request.path.startswith("/_hermes/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/_hermes/inventory")
    @require_token
    def hermes_inventory():
        with transaction() as db:
            expire(db)
            products = product_rows(db)
            counts = {(r[0], r[1]): r[2] for r in db.execute(
                "SELECT product_id,status,count(*) FROM cards GROUP BY product_id,status")}
            for product in products:
                product["inventory"] = {s: counts.get((product["id"], s), 0)
                                        for s in ("available", "reserved", "sold")}
            return jsonify(products=products)

    @app.get("/_hermes/orders")
    @require_token
    def hermes_orders():
        query, status = request.args.get("query", "").strip(), request.args.get("status", "pending")
        if len(query) > 128 or status not in ("all", "pending", "paid", "expired", "cancelled"):
            raise error("查询条件不正确")
        try:
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            raise error("分页位置不正确")
        if not 0 <= offset <= 1_000_000_000:
            raise error("分页位置不正确")
        conditions, params = [], []
        if query:
            conditions.append("(id=? OR contact_normalized=?)")
            params.extend((query, query.lower() if "@" in query else query))
        if status != "all":
            conditions.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with transaction() as db:
            expire(db)
            total = db.execute("SELECT count(*) FROM orders" + where, params).fetchone()[0]
            rows = db.execute("SELECT * FROM orders" + where +
                " ORDER BY created_at DESC,rowid DESC LIMIT 50 OFFSET ?", (*params, offset)).fetchall()
            return jsonify(orders=[safe_order(db, row) for row in rows], total=total,
                           has_more=offset + len(rows) < total)

    @app.post("/_hermes/products/<int:product_id>/cards")
    @require_token
    def hermes_import_cards(product_id):
        # Reuse the shop's validation, deduplication and transaction.
        return import_cards(product_id)

    @app.post("/_hermes/orders/<order_id>/confirm")
    @require_token
    def hermes_confirm(order_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get("payment_verified") is not True:
            raise error("请先人工核实真实到账，再明确确认发卡", "payment_confirmation_required", 409)
        with transaction() as db:
            expire(db)
            row = get_order(db, order_id)
            for key, field in (("expected_total_cents", "price_cents"), ("expected_quantity", "quantity")):
                if type(data.get(key)) is not int or data[key] != row[field]:
                    raise error("金额或数量与订单不符，请重新查询核对", "order_mismatch", 409)
            deliver(db, row, "manual", "店主通过微信明确确认已核实到账，由Hermes执行发卡")
            return jsonify(order=safe_order(db, get_order(db, order_id)))

    @app.get("/_hermes/notifications")
    @require_token
    def hermes_notifications():
        with transaction() as db:
            expire(db)
            db.execute("""UPDATE hermes_notifications SET state='skipped' WHERE state='pending'
                AND order_id IN (SELECT id FROM orders WHERE status<>'pending')""")
            rows = db.execute("""SELECT n.id notification_id,o.* FROM hermes_notifications n
                JOIN orders o ON o.id=n.order_id WHERE n.state='pending' AND n.next_attempt_at<=?
                ORDER BY n.id LIMIT 20""", (int(time.time()),)).fetchall()
            return jsonify(notifications=[{"id": r["notification_id"], "order": safe_order(db, r)} for r in rows])

    @app.post("/_hermes/notifications/<int:notification_id>/result")
    @require_token
    def hermes_notification_result(notification_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or type(data.get("delivered")) is not bool:
            raise error("通知结果格式错误")
        with transaction() as db:
            row = db.execute("SELECT * FROM hermes_notifications WHERE id=?", (notification_id,)).fetchone()
            if not row:
                raise error("通知不存在", "not_found", 404)
            if row["state"] != "pending":
                return jsonify(ok=True)
            if data["delivered"]:
                db.execute("UPDATE hermes_notifications SET state='sent',sent_at=?,last_error='' WHERE id=?",
                           (int(time.time()), notification_id))
            else:
                # Store a fixed code only, never API response bodies or credentials.
                wait = min(300, 15 * 2 ** min(row["attempts"], 5))
                db.execute("""UPDATE hermes_notifications SET attempts=attempts+1,
                    next_attempt_at=?,last_error='wechat_delivery_failed' WHERE id=?""",
                    (int(time.time()) + wait, notification_id))
        return jsonify(ok=True)
