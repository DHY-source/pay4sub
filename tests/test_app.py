import io
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash
from app import create_app


class ShopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("test-password-strong", method="pbkdf2:sha256:1000")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"TESTING": True, "DATA_DIR": self.tmp.name, "SECRET_KEY": "s" * 48,
                       "DEVICE_TOKEN": "", "ADMIN_PASSWORD_HASH": self.password_hash,
                       "SESSION_COOKIE_SECURE": False, "TRUST_PROXY": False, "ORDER_TTL_SECONDS": 1800}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.login(self.client)
        self.qr = b"\x89PNG\r\n\x1a\nTEST-ONLY"
        for channel in ("alipay", "wechat"):
            (Path(self.tmp.name) / "qr" / f"{channel}.png").write_bytes(self.qr)
        self.pid = self.add_product()
        self.import_cards(self.pid, "CARD-ONE\nCARD-TWO\nCARD-THREE")

    def login(self, client, password="test-password-strong"):
        result = client.post("/api/admin/login", json={"password": password})
        self.assertEqual(result.status_code, 200, result.json)
        return {"X-CSRF-Token": result.json["csrf_token"]}

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            with db:
                return db.execute(query, args).fetchall()

    def add_product(self, price=1000):
        result = self.client.post("/api/admin/products", headers=self.csrf,
                                  json={"name": "测试卡", "price_cents": price, "active": True})
        self.assertEqual(result.status_code, 201, result.json)
        return result.json["product"]["id"]

    def import_cards(self, pid, codes):
        return self.client.post(f"/api/admin/products/{pid}/cards", headers=self.csrf, json={"codes": codes})

    def order(self, channel="alipay", pid=None, client=None, contact="buyer@example.com", **extra):
        return (client or self.client).post("/api/orders", json={"product_id": pid or self.pid, "channel": channel, "contact": contact, **extra})

    def buyer(self, created):
        return {"Authorization": "Bearer " + created.json["token"]}

    def confirm(self, created):
        return self.client.post(f"/api/admin/orders/{created.json['order']['id']}/confirm", headers=self.csrf)

    def lookup(self, contact="buyer@example.com", **extra):
        return self.client.post("/api/orders/lookup", json={"contact": contact, **extra})

    def change_password(self, current="test-password-strong", new="changed-test-password-only", confirmation=None, client=None, headers=None):
        return (client or self.client).post("/api/admin/password", headers=self.csrf if headers is None else headers,
            json={"current_password": current, "new_password": new, "confirm_password": new if confirmation is None else confirmation})

    def test_public_catalog_does_not_expose_cards_contacts_or_device_details(self):
        self.order()
        result = self.client.get("/api/products")
        self.assertEqual(result.json["products"][0]["stock"], 2)
        for sensitive in ("CARD-ONE", "buyer@example.com", "DEVICE_TOKEN", "device_online"):
            self.assertNotIn(sensitive, result.get_data(as_text=True))
        self.assertFalse(result.json["auto_delivery"])

    def test_contact_required_and_validated(self):
        for contact in (None, "", "   ", "a", "ab", "  ab  ", 123, "a" * 129, "user\nname", "user\x00name"):
            with self.subTest(contact=contact):
                self.assertEqual(self.order(contact=contact).status_code, 400)
                self.assertEqual(self.lookup(contact).status_code, 400)
        result = self.client.post("/api/orders", json={"product_id": self.pid, "channel": "alipay"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.sql("SELECT count(*) FROM orders")[0][0], 0)
        for contact in ("abc", "a" * 128):
            self.assertEqual(self.order(contact=contact).status_code, 201)
            self.assertEqual(self.lookup(contact).json["total"], 1)

    def test_reservation_keeps_legacy_token_read_private(self):
        created = self.order()
        self.assertEqual(created.status_code, 201, created.json)
        oid = created.json["order"]["id"]
        self.assertEqual(self.client.get(f"/api/orders/{oid}").status_code, 404)
        result = self.client.get(f"/api/orders/{oid}", headers=self.buyer(created))
        self.assertNotIn("card_code", result.json["order"])
        self.assertEqual(result.json["order"]["contact"], "buyer@example.com")
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='reserved'")[0][0], 1)

    def test_default_ttl_is_thirty_minutes(self):
        config = dict(self.config)
        config.pop("ORDER_TTL_SECONDS")
        with patch.dict("os.environ", {}, clear=True):
            instance = create_app(config)
        self.assertEqual(instance.config["ORDER_TTL_SECONDS"], 1800)
        order = self.order().json["order"]
        self.assertEqual(order["expires_at"] - order["created_at"], 1800)

    def test_same_amount_orders_are_allowed_across_products_and_channels(self):
        second = self.add_product()
        self.import_cards(second, "SECOND-PRODUCT-CARD")
        for result, payable in zip((self.order(), self.order(), self.order(pid=second), self.order("wechat")), (1000, 1001, 1002, 1000)):
            self.assertEqual(result.status_code, 201, result.json)
            self.assertEqual(result.json["order"]["price_cents"], payable)
            self.assertEqual(result.json["order"]["base_price_cents"], 1000)
            self.assertEqual(result.json["order"]["payment_adjustment_cents"], payable - 1000)
        self.assertEqual(self.sql("SELECT count(DISTINCT card_id) FROM orders")[0][0], 4)

    def test_payment_amount_capacity_is_atomic_under_concurrency(self):
        self.import_cards(self.pid, "\n".join(f"EXTRA-{i}" for i in range(20)))
        def create(_):
            with self.app.test_client() as client:
                result = self.order(client=client)
                return result.status_code, result.json
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(create, range(15)))
        successes = [data["order"]["price_cents"] for status, data in results if status == 201]
        self.assertEqual(sorted(successes), list(range(1000, 1011)))
        failures = [(status, data["error"]["code"]) for status, data in results if status != 201]
        self.assertEqual(failures, [(409, "payment_amount_busy")] * 4)
        self.assertEqual(self.sql("SELECT count(*) FROM orders"), [(11,)])
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='reserved'"), [(11,)])
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards"), [(11,)])

    def test_payment_amount_avoids_other_product_base_and_quantity_totals(self):
        adjacent = self.add_product(1001)
        bulk = self.add_product(500)
        self.import_cards(adjacent, "ADJACENT")
        self.import_cards(bulk, "BULK-A\nBULK-B")
        self.assertEqual(self.order(pid=adjacent).json["order"]["price_cents"], 1001)
        self.assertEqual(self.order().json["order"]["price_cents"], 1000)
        created = self.order(pid=bulk, quantity=2)
        self.assertEqual(created.json["order"]["price_cents"], 1002)
        self.assertEqual(created.json["order"]["unit_price_cents"], 500)
        self.assertEqual(created.json["order"]["base_price_cents"], 1000)
        self.assertEqual(created.json["order"]["payment_adjustment_cents"], 2)
        self.client.patch(f"/api/admin/products/{bulk}", headers=self.csrf, json={"price_cents": 2000})
        delivered = self.confirm(created).json["order"]
        self.assertEqual(delivered["price_cents"], 1002)
        self.assertEqual(delivered["base_price_cents"], 1000)
        self.assertEqual(delivered["card_codes"], ["BULK-A", "BULK-B"])

    def test_payment_amount_reuses_cancelled_paid_and_expired_slots(self):
        self.import_cards(self.pid, "\n".join(f"REUSE-{i}" for i in range(10)))
        first, second, third = self.order(), self.order(), self.order()
        self.client.post(f"/api/orders/{second.json['order']['id']}/cancel", headers=self.buyer(second))
        self.assertEqual(self.order().json["order"]["price_cents"], 1001)
        self.confirm(first)
        self.assertEqual(self.order().json["order"]["price_cents"], 1000)
        self.sql("UPDATE orders SET expires_at=1 WHERE id=?", (third.json["order"]["id"],))
        self.assertEqual(self.order().json["order"]["price_cents"], 1002)
        self.assertEqual(self.sql("SELECT status FROM orders WHERE id=?", (third.json["order"]["id"],)), [("expired",)])

    def test_upgrade_preserves_legacy_duplicates_and_skips_their_amount(self):
        first, second = self.order(), self.order()
        self.sql("UPDATE orders SET price_cents=1000 WHERE id=?", (second.json["order"]["id"],))
        restarted = create_app(self.config).test_client()
        self.assertEqual(self.sql("SELECT price_cents FROM orders ORDER BY rowid"), [(1000,), (1000,)])
        self.assertEqual(self.order(client=restarted).json["order"]["price_cents"], 1001)

    def test_concurrent_orders_cannot_oversell(self):
        def create(_):
            with self.app.test_client() as client:
                return self.order(client=client).status_code
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(create, range(6)))
        self.assertEqual(statuses.count(201), 3, statuses)
        self.assertEqual(statuses.count(409), 3, statuses)
        self.assertEqual(self.sql("SELECT count(DISTINCT card_id) FROM orders")[0][0], 3)

    def test_manual_confirmation_is_idempotent_without_note(self):
        created = self.order()
        first, second = self.confirm(created), self.confirm(created)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json, second.json)
        self.assertEqual(first.json["order"]["card_code"], "CARD-ONE")
        self.assertEqual(first.json["order"]["delivery_mode"], "manual")
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='sold'")[0][0], 1)
        self.assertEqual(self.sql("SELECT note FROM audit WHERE action='order_delivered_manual'"), [("管理员确认发卡",)])
        self.assertEqual(self.order().status_code, 201)
        self.assertEqual(self.sql("SELECT count(*) FROM lanes")[0][0], 0)

    def test_concurrent_confirmations_issue_one_card(self):
        oid = self.order(quantity=3).json["order"]["id"]
        credentials = []
        for _ in range(6):
            client = self.app.test_client()
            credentials.append((client, self.login(client)))
        def confirm(pair):
            client, headers = pair
            result = client.post(f"/api/admin/orders/{oid}/confirm", headers=headers)
            return result.status_code, result.json
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(confirm, credentials))
        self.assertTrue(all(status == 200 for status, _ in results), results)
        self.assertEqual({tuple(result["order"]["card_codes"]) for _, result in results}, {("CARD-ONE", "CARD-TWO", "CARD-THREE")})
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='sold'")[0][0], 3)
        self.assertEqual(self.sql("SELECT count(*) FROM audit WHERE action='order_delivered_manual'")[0][0], 1)

    def test_expiry_releases_stock_without_amount_lock(self):
        created = self.order()
        self.sql("UPDATE orders SET expires_at=?", (int(time.time()) - 1,))
        result = self.client.get(f"/api/orders/{created.json['order']['id']}", headers=self.buyer(created))
        self.assertEqual(result.json["order"]["status"], "expired")
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='available'")[0][0], 3)
        self.assertEqual(self.order().status_code, 201)
        self.assertEqual(self.sql("SELECT count(*) FROM lanes")[0][0], 0)

    def test_concurrent_bulk_confirm_and_cancel_have_one_complete_outcome(self):
        created = self.order(quantity=3)
        oid = created.json["order"]["id"]
        requests = []
        for action in ("confirm", "cancel"):
            client = self.app.test_client()
            requests.append((action, client, self.login(client)))
        barrier = threading.Barrier(2)

        def act(item):
            action, client, headers = item
            barrier.wait(timeout=10)
            response = client.post(f"/api/admin/orders/{oid}/{action}", headers=headers)
            return action, response.status_code, response.json

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = {action: (status, body) for action, status, body in pool.map(act, requests)}
        self.assertEqual(sorted(status for status, _ in results.values()), [200, 409], results)
        status, first_card = self.sql("SELECT status,card_id FROM orders WHERE id=?", (oid,))[0]
        relations = self.sql("SELECT card_id,position FROM order_cards WHERE order_id=? ORDER BY position", (oid,))
        inventory = self.sql("SELECT status,count(*) FROM cards GROUP BY status")
        if status == "paid":
            self.assertEqual(results["confirm"][0], 200)
            self.assertEqual(results["cancel"][1]["error"]["code"], "not_pending")
            self.assertEqual(inventory, [("sold", 3)])
            self.assertEqual([position for _, position in relations], [0, 1, 2])
            self.assertEqual(len({card_id for card_id, _ in relations}), 3)
            self.assertEqual(first_card, relations[0][0])
            self.assertEqual(results["confirm"][1]["order"]["card_codes"], ["CARD-ONE", "CARD-TWO", "CARD-THREE"])
            action = "order_delivered_manual"
        else:
            self.assertEqual(status, "cancelled")
            self.assertEqual(results["cancel"][0], 200)
            self.assertEqual(results["confirm"][1]["error"]["code"], "order_cancelled")
            self.assertEqual(inventory, [("available", 3)])
            self.assertEqual(relations, [])
            self.assertIsNone(first_card)
            action = "order_cancelled"
        self.assertEqual(self.sql("SELECT action FROM audit WHERE target=? AND action IN ('order_delivered_manual','order_cancelled')", (oid,)), [(action,)])
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])

    def test_buyer_cancel_releases_stock_and_prevents_delivery(self):
        created = self.order()
        oid = created.json["order"]["id"]
        result = self.client.post(f"/api/orders/{oid}/cancel", headers=self.buyer(created))
        self.assertEqual(result.json["order"]["status"], "cancelled")
        self.assertEqual(self.confirm(created).json["error"]["code"], "order_cancelled")
        self.assertEqual(self.order().status_code, 201)
        self.assertEqual(self.sql("SELECT count(*) FROM lanes")[0][0], 0)

    def test_admin_cancel_note_optional_and_paid_cannot_be_cancelled(self):
        created = self.order()
        result = self.client.post(f"/api/admin/orders/{created.json['order']['id']}/cancel", headers=self.csrf)
        self.assertEqual(result.status_code, 200)
        created = self.order()
        self.confirm(created)
        result = self.client.post(f"/api/admin/orders/{created.json['order']['id']}/cancel", headers=self.csrf, json={})
        self.assertEqual(result.status_code, 409)

    def test_expired_confirmation_uses_only_available_stock(self):
        old = self.order()
        self.sql("UPDATE orders SET expires_at=?", (int(time.time()) - 1,))
        self.client.get("/api/products")
        self.order()
        result = self.confirm(old)
        self.assertEqual(result.json["order"]["card_code"], "CARD-TWO")
        self.assertEqual(self.sql("SELECT status FROM cards WHERE code='CARD-ONE'")[0][0], "reserved")

    def test_out_of_stock_confirmation_does_not_change_expired_order(self):
        self.sql("DELETE FROM cards WHERE code!='CARD-ONE'")
        old = self.order()
        self.sql("UPDATE orders SET expires_at=?", (int(time.time()) - 1,))
        self.client.get("/api/products")
        new = self.order()
        result = self.confirm(old)
        self.assertEqual((result.status_code, result.json["error"]["code"]), (409, "out_of_stock"))
        self.assertEqual(self.sql("SELECT status,card_id FROM orders WHERE id=?", (old.json["order"]["id"],)), [("expired", None)])
        self.assertEqual(self.confirm(new).status_code, 200)

    def test_contact_lookup_normalizes_email_and_returns_only_matching_orders(self):
        first = self.order(contact="  Buyer@Example.COM  ")
        second = self.order(contact="buyer@example.com")
        self.order(contact="other@example.com")
        self.confirm(first)
        result = self.lookup(" BUYER@example.com ").json
        self.assertEqual((result["total"], result["has_more"]), (2, False))
        self.assertEqual([row["id"] for row in result["orders"]], [second.json["order"]["id"], first.json["order"]["id"]])
        self.assertNotIn("card_code", result["orders"][0])
        self.assertEqual(result["orders"][1]["card_code"], "CARD-ONE")
        self.assertEqual(self.lookup("buyer@exam").json["orders"], [])

    def test_non_email_contact_is_exact_case_sensitive_and_parameterized(self):
        self.order(contact="QQ_AbC123")
        self.order(contact="' OR 1=1 --")
        self.assertEqual(self.lookup(" QQ_AbC123 ").json["total"], 1)
        self.assertEqual(self.lookup("qq_abc123").json["total"], 0)
        self.assertEqual(self.lookup("' OR 1=1 --").json["total"], 1)

    def test_lookup_sql_rate_limit_is_shared_by_workers_and_expires(self):
        for _ in range(30):
            self.assertEqual(self.lookup("unknown").status_code, 200)
        other = create_app(self.config).test_client()
        result = other.post("/api/orders/lookup", json={"contact": "different"})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.sql("UPDATE lookup_attempts SET created_at=?", (int(time.time()) - 61,))
        self.assertEqual(self.lookup().status_code, 200)

    def test_order_creation_is_still_rate_limited(self):
        self.import_cards(self.pid, "\n".join(f"EXTRA-{i}" for i in range(25)))
        for _ in range(20):
            created = self.order()
            self.assertEqual(created.status_code, 201)
            self.client.post(f"/api/orders/{created.json['order']['id']}/cancel", headers=self.buyer(created))
        self.assertEqual(self.order().status_code, 429)

    def populate_orders(self, count=55):
        first = self.order(contact="paged@example.com").json["order"]
        for i in range(1, count):
            self.sql("""INSERT INTO orders(id,token_hash,product_id,product_name,price_cents,channel,status,created_at,expires_at,client_ip_hash,contact,contact_normalized,unit_price_cents)
                        VALUES (?,?,?,?,?,?,'expired',?,?,?,?,?,1000)""",
                     (f"page-{i:04d}", "unused", self.pid, "测试卡", 1000, "alipay", first["created_at"] - i, 1, "fixture", "paged@example.com", "paged@example.com"))
        return first

    def test_lookup_and_admin_pagination_search_and_ordering(self):
        first = self.populate_orders()
        a = self.lookup("paged@example.com").json
        b = self.lookup("paged@example.com", offset=50).json
        self.assertEqual((len(a["orders"]), a["total"], a["has_more"]), (50, 55, True))
        self.assertEqual((len(b["orders"]), b["total"], b["has_more"]), (5, 55, False))
        self.assertFalse({row["id"] for row in a["orders"]} & {row["id"] for row in b["orders"]})
        result = self.client.get("/api/admin/orders", query_string={"query": "PAGED@EXAMPLE.COM"}).json
        self.assertEqual(result["total"], 55)
        self.assertEqual(result["orders"][0]["id"], first["id"])
        filtered = self.client.get("/api/admin/orders?status=expired&offset=50").json
        self.assertEqual((len(filtered["orders"]), filtered["total"], filtered["has_more"]), (4, 54, False))
        exact = self.client.get("/api/admin/orders", query_string={"query": first["id"]}).json
        self.assertEqual(exact["total"], 1)
        self.assertEqual(self.client.get("/api/admin/orders?query=paged").json["total"], 0)

    def test_invalid_pagination_and_status_are_rejected(self):
        for offset in (-1, True, "0", 1.2, 10**30):
            self.assertEqual(self.lookup(offset=offset).status_code, 400)
        for suffix in ("offset=-1", "offset=abc", "offset=1.2", "status=unknown"):
            self.assertEqual(self.client.get("/api/admin/orders?" + suffix).status_code, 400)
            self.assertEqual(self.client.get(f"/api/admin/products/{self.pid}/cards?" + suffix).status_code, 400)

    def test_available_inventory_can_be_listed_edited_and_deleted(self):
        listing = self.client.get(f"/api/admin/products/{self.pid}/cards").json
        self.assertEqual(listing["total"], 3)
        card = listing["cards"][0]
        self.assertEqual(set(card), {"id", "product_id", "code", "status", "created_at"})
        result = self.client.patch(f"/api/admin/cards/{card['id']}", headers=self.csrf, json={"code": "  EDITED-CARD  "})
        self.assertEqual(result.json["card"]["code"], "EDITED-CARD")
        self.assertEqual(self.import_cards(self.pid, "EDITED-CARD").json, {"inserted": 0, "duplicates": 1})
        self.assertEqual(self.client.delete(f"/api/admin/cards/{card['id']}", headers=self.csrf).json, {"ok": True})
        self.assertEqual(self.client.get("/api/products").json["products"][0]["stock"], 2)

    def test_reserved_and_sold_inventory_is_read_only(self):
        created = self.order()
        card_id = self.sql("SELECT card_id FROM orders")[0][0]
        for status in ("reserved", "sold"):
            if status == "sold":
                self.confirm(created)
            rows = self.client.get(f"/api/admin/products/{self.pid}/cards?status={status}").json
            self.assertEqual(rows["cards"][0]["code"], "CARD-ONE")
            for result in (self.client.patch(f"/api/admin/cards/{card_id}", headers=self.csrf, json={"code": "CHANGED"}),
                           self.client.delete(f"/api/admin/cards/{card_id}", headers=self.csrf)):
                self.assertEqual((result.status_code, result.json["error"]["code"]), (409, "card_not_available"))
        self.assertEqual(self.lookup().json["orders"][0]["card_code"], "CARD-ONE")

    def test_inventory_rejects_duplicates_invalid_codes_and_missing_records(self):
        cards = self.client.get(f"/api/admin/products/{self.pid}/cards").json["cards"]
        cid = cards[0]["id"]
        conflict = self.client.patch(f"/api/admin/cards/{cid}", headers=self.csrf, json={"code": cards[1]["code"]})
        self.assertEqual((conflict.status_code, conflict.json["error"]["code"]), (409, "duplicate_card"))
        for code in ("", "  ", "a\nb", 123, "x" * 4001):
            self.assertEqual(self.client.patch(f"/api/admin/cards/{cid}", headers=self.csrf, json={"code": code}).status_code, 400)
        self.assertEqual(self.client.patch("/api/admin/cards/999", headers=self.csrf, json={"code": "valid"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/admin/cards/999", headers=self.csrf).status_code, 404)
        self.assertEqual(self.client.get("/api/admin/products/999/cards").status_code, 404)

    def test_inventory_pagination_and_batch_duplicate_detection(self):
        self.import_cards(self.pid, "\n".join(f"PAGE-CARD-{i}" for i in range(52)))
        first = self.client.get(f"/api/admin/products/{self.pid}/cards").json
        second = self.client.get(f"/api/admin/products/{self.pid}/cards?offset=50").json
        self.assertEqual((len(first["cards"]), first["total"], first["has_more"]), (50, 55, True))
        self.assertEqual((len(second["cards"]), second["total"], second["has_more"]), (5, 55, False))
        self.assertEqual(self.import_cards(self.pid, "CARD-ONE\nNEW-CARD\nNEW-CARD").json, {"inserted": 1, "duplicates": 2})

    def test_retired_device_endpoints_and_rejected_automatic_delivery(self):
        created = self.order()
        for endpoint in ("/api/device/heartbeat", "/api/device/events"):
            result = self.client.post(endpoint, json={"listener_connected": True})
            self.assertEqual((result.status_code, result.json["error"]["code"]), (410, "feature_retired"))
        result = self.client.post("/api/admin/settings", headers=self.csrf, json={"auto_delivery": True})
        self.assertEqual((result.status_code, result.json["error"]["code"]), (409, "manual_delivery_only"))
        self.assertEqual(self.sql("SELECT status FROM orders WHERE id=?", (created.json["order"]["id"],)), [("pending",)])
        self.assertEqual(self.sql("SELECT count(*) FROM events")[0][0], 0)
        self.assertEqual(self.client.post("/api/admin/settings", headers=self.csrf, json={"auto_delivery": False}).status_code, 200)

    def test_legacy_locks_and_auto_setting_cannot_block_or_automatically_deliver(self):
        self.sql("INSERT INTO lanes VALUES ('alipay',1000,0,'历史锁')")
        self.sql("UPDATE settings SET value='true' WHERE key='auto_delivery'")
        self.assertEqual(self.order().status_code, 201)
        self.assertFalse(self.client.get("/api/products").json["auto_delivery"])
        overview = self.client.get("/api/admin/overview").json
        self.assertFalse(overview["settings"]["auto_delivery"])
        for field in ("device", "events", "lanes"):
            self.assertNotIn(field, overview)

    def test_admin_apis_require_session_and_writes_require_csrf(self):
        anonymous = self.app.test_client()
        for path in ("/api/admin/overview", "/api/admin/orders", f"/api/admin/products/{self.pid}/cards"):
            self.assertEqual(anonymous.get(path).status_code, 401)
        cid = self.sql("SELECT id FROM cards LIMIT 1")[0][0]
        self.assertEqual(self.client.patch(f"/api/admin/cards/{cid}", json={"code": "bad"}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/admin/cards/{cid}").status_code, 403)
        oid = self.order().json["order"]["id"]
        self.assertEqual(anonymous.post(f"/api/admin/orders/{oid}/confirm").status_code, 401)
        self.assertEqual(self.client.post(f"/api/admin/orders/{oid}/confirm").status_code, 403)

    def test_static_entry_points_and_security_headers(self):
        for path in ("/", "/admin", "/static/common.js", "/static/store.js", "/static/admin.js", "/static/styles.css"):
            with self.client.get(path) as result:
                self.assertEqual(result.status_code, 200, path)
        for result in (self.client.get("/api/products"), self.lookup(), self.client.get(f"/api/admin/products/{self.pid}/cards")):
            self.assertEqual(result.headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", result.headers["Content-Security-Policy"])

    def test_qr_upload_rejects_svg_and_pending_payment_change(self):
        bad = self.client.post("/api/admin/qr/alipay", headers=self.csrf, data={"file": (io.BytesIO(b"<svg></svg>"), "qr.svg")})
        self.assertEqual(bad.status_code, 400)
        self.order()
        conflict = self.client.post("/api/admin/qr/alipay", headers=self.csrf, data={"file": (io.BytesIO(self.qr), "qr.png")})
        self.assertEqual(conflict.status_code, 409)

    def test_login_rate_limit_and_product_validation(self):
        anonymous = self.app.test_client()
        for _ in range(10):
            self.assertEqual(anonymous.post("/api/admin/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(anonymous.post("/api/admin/login", json={"password": "wrong"}).status_code, 429)
        result = self.client.post("/api/admin/products", headers=self.csrf, json={"name": "bad", "price_cents": True})
        self.assertEqual(result.status_code, 400)

    def test_three_card_order_totals_and_full_delivery_are_stable(self):
        created = self.order(quantity=3)
        self.assertEqual(created.status_code, 201, created.json)
        order = created.json["order"]
        self.assertEqual((order["quantity"], order["unit_price_cents"], order["price_cents"]), (3, 1000, 3000))
        for key in ("card_code", "card_codes"):
            self.assertNotIn(key, order)
            self.assertNotIn(key, self.lookup().json["orders"][0])
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards")[0][0], 3)
        self.assertEqual(self.client.get("/api/products").json["products"][0]["stock"], 0)
        self.client.patch(f"/api/admin/products/{self.pid}", headers=self.csrf, json={"price_cents": 2000})
        delivered = self.confirm(created)
        expected = ["CARD-ONE", "CARD-TWO", "CARD-THREE"]
        self.assertEqual(delivered.json["order"]["card_codes"], expected)
        self.assertEqual(delivered.json["order"]["card_code"], "\n".join(expected))
        self.assertEqual(delivered.json["order"]["price_cents"], 3000)
        self.assertEqual(self.confirm(created).json, delivered.json)
        token_read = self.client.get(f"/api/orders/{order['id']}", headers=self.buyer(created))
        self.assertEqual(token_read.json["order"]["card_codes"], expected)
        self.assertEqual(self.lookup().json["orders"][0]["card_codes"], expected)
        first_id = self.sql("SELECT card_id FROM order_cards WHERE order_id=? ORDER BY position", (order["id"],))[0][0]
        self.assertEqual(self.sql("SELECT card_id FROM orders WHERE id=?", (order["id"],))[0][0], first_id)

    def test_quantity_defaults_to_one_and_rejects_non_integer_or_out_of_range(self):
        for quantity in (None, True, False, 0, -1, 101, 1.0, 1.5, "2", [], {}):
            with self.subTest(quantity=quantity):
                result = self.order(quantity=quantity)
                self.assertEqual((result.status_code, result.json["error"]["code"]), (400, "invalid_quantity"))
        self.assertEqual(self.sql("SELECT count(*) FROM orders")[0][0], 0)
        order = self.order().json["order"]
        self.assertEqual((order["quantity"], order["unit_price_cents"], order["price_cents"]), (1, 1000, 1000))
        self.import_cards(self.pid, "\n".join(f"LIMIT-{i}" for i in range(98)))
        limit = self.order(quantity=100)
        self.assertEqual(limit.status_code, 201, limit.json)
        self.assertEqual(limit.json["order"]["price_cents"], 100000)
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards WHERE order_id=?", (limit.json["order"]["id"],))[0][0], 100)

    def test_bulk_order_insufficient_stock_reserves_nothing(self):
        result = self.order(quantity=4)
        self.assertEqual((result.status_code, result.json["error"]["code"]), (409, "out_of_stock"))
        self.assertEqual(self.sql("SELECT count(*) FROM orders")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='available'")[0][0], 3)
        self.assertEqual(self.order(quantity=3).status_code, 201)

    def test_two_bulk_buyers_cannot_oversell_remaining_inventory(self):
        def create(_):
            with self.app.test_client() as client:
                result = self.order(client=client, quantity=2)
                return result.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(create, range(2)))
        self.assertEqual(sorted(statuses), [201, 409])
        self.assertEqual(self.sql("SELECT count(*) FROM orders")[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards")[0][0], 2)
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='available'")[0][0], 1)

    def test_bulk_admin_cancel_is_idempotent_and_releases_all_links(self):
        created = self.order(quantity=3)
        oid = created.json["order"]["id"]
        first = self.client.post(f"/api/admin/orders/{oid}/cancel", headers=self.csrf)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["order"]["status"], "cancelled")
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='available'")[0][0], 3)
        card_id = self.sql("SELECT id FROM cards WHERE code='CARD-THREE'")[0][0]
        self.assertEqual(self.client.delete(f"/api/admin/cards/{card_id}", headers=self.csrf).status_code, 200)
        self.import_cards(self.pid, "CARD-THREE")
        self.assertEqual(self.order(quantity=3).status_code, 201)
        repeated = self.client.post(f"/api/admin/orders/{oid}/cancel", headers=self.csrf)
        self.assertEqual(repeated.json, first.json)
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='reserved'")[0][0], 3)
        self.assertEqual(self.sql("SELECT count(*) FROM audit WHERE action='order_cancelled' AND target=?", (oid,))[0][0], 1)
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])

    def test_bulk_buyer_cancel_releases_every_card(self):
        created = self.order(quantity=3)
        result = self.client.post(f"/api/orders/{created.json['order']['id']}/cancel", headers=self.buyer(created))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards")[0][0], 0)
        self.assertEqual(self.order(quantity=3).status_code, 201)

    def test_expired_bulk_order_can_be_cancelled_without_releasing_new_reservations(self):
        old = self.order(quantity=3)
        oid = old.json["order"]["id"]
        self.sql("UPDATE orders SET expires_at=?", (int(time.time()) - 1,))
        self.client.get("/api/products")
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards")[0][0], 0)
        self.assertEqual(self.order(quantity=3).status_code, 201)
        self.assertEqual(self.client.post(f"/api/orders/{oid}/cancel", headers=self.buyer(old)).status_code, 409)
        result = self.client.post(f"/api/admin/orders/{oid}/cancel", headers=self.csrf)
        self.assertEqual((result.status_code, result.json["order"]["status"]), (200, "cancelled"))
        self.assertEqual(self.client.post(f"/api/admin/orders/{oid}/cancel", headers=self.csrf).status_code, 200)
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='reserved'")[0][0], 3)
        self.assertEqual(self.sql("SELECT count(*) FROM lanes")[0][0], 0)

    def test_expired_bulk_delivery_is_atomic_and_can_retry_after_restock(self):
        old = self.order(quantity=3)
        oid = old.json["order"]["id"]
        self.sql("UPDATE orders SET expires_at=?", (int(time.time()) - 1,))
        self.client.get("/api/products")
        new = self.order(quantity=2)
        failed = self.confirm(old)
        self.assertEqual((failed.status_code, failed.json["error"]["code"]), (409, "out_of_stock"))
        self.assertEqual(self.sql("SELECT status,card_id FROM orders WHERE id=?", (oid,)), [("expired", None)])
        self.assertEqual(self.sql("SELECT count(*) FROM order_cards WHERE order_id=?", (oid,))[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='sold'")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='available'")[0][0], 1)
        self.import_cards(self.pid, "RESTOCK-1\nRESTOCK-2")
        delivered = self.confirm(old)
        self.assertEqual(delivered.json["order"]["card_codes"], ["CARD-THREE", "RESTOCK-1", "RESTOCK-2"])
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='reserved'")[0][0], 2)
        self.assertEqual(self.confirm(new).json["order"]["card_codes"], ["CARD-ONE", "CARD-TWO"])
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])

    def test_password_change_replaces_hash_revokes_session_and_changes_login(self):
        before = self.sql("SELECT password_hash,session_version FROM admin_auth WHERE id=1")[0]
        changed = self.change_password()
        self.assertEqual((changed.status_code, changed.json), (200, {"ok": True}))
        after = self.sql("SELECT password_hash,session_version FROM admin_auth WHERE id=1")[0]
        self.assertNotEqual(after[0], before[0])
        self.assertTrue(check_password_hash(after[0], "changed-test-password-only"))
        self.assertEqual(after[1], before[1] + 1)
        self.assertEqual(self.client.get("/api/admin/session").json, {"authenticated": False})
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)
        old = self.client.post("/api/admin/login", json={"password": "test-password-strong"})
        self.assertEqual(old.status_code, 401)
        self.login(self.client, "changed-test-password-only")
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 200)
        self.assertEqual(self.sql("SELECT target,note FROM audit WHERE action='admin_password_changed'"), [("", "")])

    def test_password_change_revokes_other_worker_sessions_and_replayed_cookies(self):
        worker = create_app(self.config)
        peer = worker.test_client()
        peer_headers = self.login(peer)
        old_cookie = peer.get_cookie("session").value
        self.assertEqual(self.change_password().status_code, 200)
        self.assertEqual(peer.get("/api/admin/overview").status_code, 401)
        self.assertEqual(peer.post("/api/admin/settings", headers=peer_headers, json={"shop_name": "不可修改"}).status_code, 401)
        self.assertEqual(peer.get("/api/admin/session").json, {"authenticated": False})
        replay = worker.test_client()
        replay.set_cookie("session", old_cookie)
        self.assertEqual(replay.get("/api/admin/session").json, {"authenticated": False})
        self.assertNotEqual(self.client.get("/api/products").json["shop_name"], "不可修改")

    def test_legacy_admin_cookie_without_session_version_requires_login(self):
        with self.client.session_transaction() as old_session:
            old_session.pop("session_version")
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/session").json, {"authenticated": False})

    def test_password_validation_errors_do_not_mutate_credentials(self):
        before = self.sql("SELECT * FROM admin_auth")
        cases = [
            ({"current_password": "incorrect-current-secret", "new_password": "valid-new-test-secret", "confirm_password": "valid-new-test-secret"}, "invalid_current_password"),
            ({"current_password": "test-password-strong", "new_password": "valid-new-test-secret", "confirm_password": "different-test-secret"}, "password_mismatch"),
        ]
        for new in ("short", "a" * 11, "a" * 257, " " * 12, "\t" * 12, 123, None):
            cases.append(({"current_password": "test-password-strong", "new_password": new, "confirm_password": new}, "invalid_new_password"))
        for payload, code in cases:
            with self.subTest(code=code):
                result = self.client.post("/api/admin/password", headers=self.csrf, json=payload)
                self.assertEqual((result.status_code, result.json["error"]["code"]), (400, code))
                self.assertEqual(self.sql("SELECT * FROM admin_auth"), before)
                self.assertTrue(self.client.get("/api/admin/session").json["authenticated"])
                for secret in ("incorrect-current-secret", "valid-new-test-secret", "different-test-secret"):
                    self.assertNotIn(secret, result.get_data(as_text=True))
        self.assertEqual(self.sql("SELECT count(*) FROM audit WHERE action='admin_password_changed'")[0][0], 0)

    def test_password_change_requires_authenticated_session_and_csrf(self):
        before = self.sql("SELECT * FROM admin_auth")
        anonymous = self.app.test_client()
        self.assertEqual(self.change_password(client=anonymous).status_code, 401)
        self.assertEqual(self.change_password(headers={}).status_code, 403)
        self.assertEqual(self.change_password(headers={"X-CSRF-Token": "incorrect-token"}).status_code, 403)
        self.assertEqual(self.sql("SELECT * FROM admin_auth"), before)
        self.assertEqual(self.sql("SELECT count(*) FROM login_attempts")[0][0], 0)

    def test_current_password_failures_use_shared_sql_limit(self):
        peer = create_app(self.config).test_client()
        peer_headers = self.login(peer)
        before = self.sql("SELECT * FROM admin_auth")
        for i in range(10):
            client, headers = (self.client, self.csrf) if i % 2 == 0 else (peer, peer_headers)
            result = self.change_password(current="wrong-current-password", client=client, headers=headers)
            self.assertEqual((result.status_code, result.json["error"]["code"]), (400, "invalid_current_password"))
        self.assertEqual(self.change_password(current="wrong-current-password").status_code, 429)
        self.assertEqual(self.app.test_client().post("/api/admin/login", json={"password": "wrong"}).status_code, 429)
        self.assertEqual(self.sql("SELECT * FROM admin_auth"), before)
        self.sql("UPDATE login_attempts SET created_at=?", (int(time.time()) - 901,))
        self.assertEqual(self.change_password().status_code, 200)

    def test_password_hashes_stay_private_from_settings_and_api_responses(self):
        changed = self.change_password()
        self.csrf = self.login(self.client, "changed-test-password-only")
        stored_hash = self.sql("SELECT password_hash FROM admin_auth WHERE id=1")[0][0]
        responses = [changed, self.client.get("/api/products"), self.client.get("/api/admin/overview"),
                     self.client.get("/api/admin/session"), self.client.post("/api/admin/settings", headers=self.csrf, json={})]
        for response in responses:
            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
            for secret in (stored_hash, self.password_hash, "changed-test-password-only", "password_hash", "admin_auth"):
                self.assertNotIn(secret, text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse({row[0] for row in self.sql("SELECT key FROM settings")} & {"password_hash", "admin_auth", "session_version"})

    def test_password_length_boundaries_preserve_exact_characters(self):
        first = " aB3" * 3
        self.assertEqual(len(first), 12)
        self.assertEqual(self.change_password(new=first).status_code, 200)
        self.csrf = self.login(self.client, first)
        last = "x" * 256
        self.assertEqual(self.change_password(current=first, new=last).status_code, 200)
        self.login(self.client, last)

    def test_concurrent_password_changes_allow_only_one_new_credential(self):
        requests = []
        for index in range(2):
            client = self.app.test_client()
            requests.append((client, self.login(client), f"concurrent-new-secret-{index}"))
        before_version = self.sql("SELECT session_version FROM admin_auth WHERE id=1")[0][0]
        barrier = threading.Barrier(2)
        def change(item):
            client, headers, password = item
            barrier.wait(timeout=10)
            result = self.change_password(client=client, headers=headers, new=password)
            return password, result.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, requests))
        self.assertEqual(sorted(status for _, status in results), [200, 401], results)
        self.assertEqual(self.sql("SELECT session_version FROM admin_auth WHERE id=1")[0][0], before_version + 1)
        for password, status in results:
            result = self.app.test_client().post("/api/admin/login", json={"password": password})
            self.assertEqual(result.status_code, 200 if status == 200 else 401)
        self.assertEqual(self.sql("SELECT count(*) FROM audit WHERE action='admin_password_changed'")[0][0], 1)


if __name__ == "__main__":
    unittest.main()
