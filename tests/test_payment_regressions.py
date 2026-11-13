"""Regression coverage for migration to manual delivery and retired device calls."""
import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash
import app as app_module
from app import create_app, LEGACY_SUPPORT_NOTICE


class ManualUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("regression-test-only", method="pbkdf2:sha256:1000")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="card-manual-regression-")
        self.addCleanup(self.directory.cleanup)
        self.config = {"TESTING": True, "DATA_DIR": self.directory.name,
                       "SECRET_KEY": "regression-session-key-" * 3, "DEVICE_TOKEN": "",
                       "ADMIN_PASSWORD_HASH": self.password_hash, "SESSION_COOKIE_SECURE": False,
                       "TRUST_PROXY": False, "ORDER_TTL_SECONDS": 1800}
        self.app = create_app(self.config)
        self.database = self.app.config["DATABASE"]
        self.client = self.app.test_client()
        login = self.client.post("/api/admin/login", json={"password": "regression-test-only"})
        self.headers = {"X-CSRF-Token": login.json["csrf_token"]}
        (Path(self.directory.name) / "qr" / "alipay.png").write_bytes(b"\x89PNG\r\n\x1a\nTEST-ONLY")
        product = self.client.post("/api/admin/products", headers=self.headers, json={"name": "测试商品", "price_cents": 1000})
        self.pid = product.json["product"]["id"]
        imported = self.client.post(f"/api/admin/products/{self.pid}/cards", headers=self.headers,
                                    json={"codes": "OLD-CARD-1\nOLD-CARD-2\nOLD-CARD-3"})
        self.assertEqual(imported.status_code, 200)

    def query(self, statement, parameters=()):
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            with db:
                return [dict(row) for row in db.execute(statement, parameters).fetchall()]

    def create_order(self, client=None, contact="original@example.com", quantity=1):
        result = (client or self.client).post("/api/orders", json={"product_id": self.pid, "channel": "alipay", "contact": contact, "quantity": quantity})
        self.assertEqual(result.status_code, 201, result.json)
        return result.json

    def legacy_fixture(self, notice=LEGACY_SUPPORT_NOTICE):
        sold = self.create_order()
        pending = self.create_order()
        result = self.client.post(f"/api/admin/orders/{sold['order']['id']}/confirm", headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.query("UPDATE orders SET delivery_mode='automatic',admin_note='历史发卡备注' WHERE id=?", (sold["order"]["id"],))
        created = sold["order"]["created_at"]
        self.query("""INSERT INTO events(id,fingerprint,device_id,channel,amount_cents,occurred_at,received_at,
                      source_package,title,text,status,reason,order_id,resolved_note)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   ("old-event", "old-fingerprint", "old-phone", "alipay", 1000, created, created,
                    "com.eg.android.AlipayGphone", "支付宝", "历史敏感到账通知", "delivered", "历史说明", sold["order"]["id"], "保留备注"))
        self.query("INSERT INTO lanes VALUES ('alipay',1000,0,'旧金额锁')")
        self.query("INSERT INTO devices VALUES ('old-phone',?,1)", (created,))
        self.query("UPDATE settings SET value='true' WHERE key='auto_delivery'")
        self.query("UPDATE settings SET value=? WHERE key='support_notice'", (json.dumps(notice),))
        # Simulate the previous schema, which had no customer contact columns.
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DROP TABLE order_cards")
            db.execute("ALTER TABLE orders DROP COLUMN quantity")
            db.execute("ALTER TABLE orders DROP COLUMN unit_price_cents")
            db.execute("DROP INDEX orders_contact")
            db.execute("ALTER TABLE orders DROP COLUMN contact_normalized")
            db.execute("ALTER TABLE orders DROP COLUMN contact")
            db.execute("CREATE UNIQUE INDEX one_pending_amount ON orders(channel,price_cents) WHERE status='pending'")
            db.commit()
        before = {table: self.query("SELECT * FROM " + table + " ORDER BY id") for table in ("orders", "cards", "events", "audit", "devices")}
        return before, sold, pending

    def assert_preserved(self, before):
        for table, old_rows in before.items():
            after = {row["id"]: row for row in self.query("SELECT * FROM " + table)}
            self.assertEqual(len(after), len(old_rows), table)
            for old in old_rows:
                actual = {key: after[old["id"]][key] for key in old}
                self.assertEqual(actual, old, table)
        self.assertEqual(self.query("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.query("PRAGMA integrity_check"), [{"integrity_check": "ok"}])

    def test_legacy_orders_cards_and_receipts_survive_upgrade_without_fake_contacts(self):
        before, sold, pending = self.legacy_fixture()
        upgraded = create_app(self.config)
        self.assert_preserved(before)
        contacts = self.query("SELECT contact,contact_normalized FROM orders")
        self.assertEqual(contacts, [{"contact": "", "contact_normalized": ""}] * 2)
        indexes = {row["name"] for row in self.query("PRAGMA index_list(orders)")}
        self.assertNotIn("one_pending_amount", indexes)
        self.assertIn("orders_contact", indexes)
        client = upgraded.test_client()
        result = client.get(f"/api/orders/{sold['order']['id']}", headers={"Authorization": "Bearer " + sold["token"]})
        self.assertEqual(result.json["order"]["card_code"], "OLD-CARD-1")
        self.assertEqual(result.json["order"]["card_codes"], ["OLD-CARD-1"])
        self.assertEqual((result.json["order"]["quantity"], result.json["order"]["unit_price_cents"]), (1, 1000))
        self.assertEqual(self.query("SELECT count(*) AS n FROM order_cards"), [{"n": 2}])
        self.assertEqual(result.json["order"]["contact"], "")
        self.assertEqual(client.post("/api/orders/lookup", json={"contact": "original@example.com"}).json["total"], 0)
        self.assertEqual(client.post("/api/orders/lookup", json={"contact": ""}).status_code, 400)
        self.assertEqual(self.create_order(client=client)["order"]["status"], "pending")
        read = client.get(f"/api/orders/{pending['order']['id']}", headers={"Authorization": "Bearer " + pending["token"]})
        self.assertNotIn("card_code", read.json["order"])

    def test_migration_is_repeatable_and_forces_manual_settings(self):
        before, _, _ = self.legacy_fixture()
        for _ in range(3):
            instance = create_app({**self.config, "AUTO_DELIVERY": True})
            self.assertFalse(instance.config["AUTO_DELIVERY"])
            self.assert_preserved(before)
            saved = {row["key"]: json.loads(row["value"]) for row in self.query("SELECT * FROM settings")}
            self.assertFalse(saved["auto_delivery"])
            self.assertEqual(saved["support_notice"], "")
        self.assertEqual(self.query("SELECT count(*) AS n FROM lanes"), [{"n": 1}])

    def test_custom_support_notice_is_preserved(self):
        notice = "请联系店主领取使用帮助。"
        self.legacy_fixture(notice=notice)
        instance = create_app(self.config)
        self.assertEqual(instance.test_client().get("/api/products").json["support_notice"], notice)

    def test_concurrent_worker_startup_upgrades_schema_safely(self):
        before, _, _ = self.legacy_fixture()
        barrier = threading.Barrier(4)
        def start(_):
            barrier.wait(timeout=10)
            return create_app(self.config)
        with ThreadPoolExecutor(max_workers=4) as pool:
            workers = list(pool.map(start, range(4)))
        self.assertEqual(len(workers), 4)
        self.assert_preserved(before)
        for worker in workers:
            self.assertFalse(worker.test_client().get("/api/products").json["auto_delivery"])

    def test_existing_contact_is_normalized_without_changing_display(self):
        order = self.create_order(contact="Customer@Example.COM")
        self.query("UPDATE orders SET contact_normalized='' WHERE id=?", (order["order"]["id"],))
        upgraded = create_app(self.config)
        result = upgraded.test_client().post("/api/orders/lookup", json={"contact": "customer@example.com"}).json
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["orders"][0]["contact"], "Customer@Example.COM")
        self.assertEqual(self.query("SELECT contact_normalized FROM orders"), [{"contact_normalized": "customer@example.com"}])

    def test_failed_shop_migration_rolls_back_added_columns_and_keeps_old_index(self):
        before, _, _ = self.legacy_fixture()
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            class FailAtContactIndex:
                def execute(self, statement, parameters=()):
                    if statement.startswith("CREATE INDEX IF NOT EXISTS orders_contact"):
                        raise sqlite3.OperationalError("injected migration failure")
                    return db.execute(statement, parameters)
                def commit(self):
                    db.commit()
                def rollback(self):
                    db.rollback()
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected migration failure"):
                app_module.migrate_shop_schema(FailAtContactIndex())
            self.assertNotIn("contact", {row[1] for row in db.execute("PRAGMA table_info(orders)")})
            self.assertIn("one_pending_amount", {row[1] for row in db.execute("PRAGMA index_list(orders)")})
        self.assert_preserved(before)
        create_app(self.config)
        self.assert_preserved(before)

    def test_old_fingerprint_unique_constraint_is_migrated_without_losing_receipts(self):
        before, _, _ = self.legacy_fixture()
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(app_module.EVENTS_TABLE.replace("IF NOT EXISTS events", "events_legacy").replace("fingerprint TEXT NOT NULL", "fingerprint TEXT NOT NULL UNIQUE"))
            db.execute("INSERT INTO events_legacy SELECT * FROM events")
            db.execute("DROP TABLE events")
            db.execute("ALTER TABLE events_legacy RENAME TO events")
            db.commit()
        create_app(self.config)
        self.assert_preserved(before)
        with closing(sqlite3.connect(self.database)) as db:
            for index in db.execute("PRAGMA index_list(events)").fetchall():
                columns = [row[0] for row in db.execute("SELECT name FROM pragma_index_info(?)", (index[1],))]
                if columns == ["fingerprint"]:
                    self.assertFalse(index[2])

    def test_stale_signed_notifications_and_environment_flags_cannot_issue_cards(self):
        with patch.dict("os.environ", {"AUTO_DELIVERY": "true", "DEVICE_TOKEN": "x" * 64}):
            upgraded = create_app({**self.config, "AUTO_DELIVERY": True})
        order = self.create_order()
        timestamp = str(int(time.time()))
        raw = json.dumps({"device_id": "old-phone", "event_id": "old-event-12345678", "channel": "alipay",
                          "amount_cents": 1000, "occurred_at": int(timestamp), "source_package": "com.eg.android.AlipayGphone",
                          "title": "支付宝", "text": "你已成功收款10.00元"}).encode()
        signature = hmac.new(b"x" * 64, timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        client = upgraded.test_client()
        for _ in range(3):
            result = client.post("/api/device/events", data=raw, content_type="application/json",
                                 headers={"X-Device-Timestamp": timestamp, "X-Device-Signature": signature})
            self.assertEqual(result.status_code, 410)
        self.assertEqual(self.query("SELECT status FROM orders WHERE id=?", (order["order"]["id"],)), [{"status": "pending"}])
        self.assertEqual(self.query("SELECT count(*) AS n FROM cards WHERE status='sold'"), [{"n": 0}])
        self.assertEqual(self.query("SELECT count(*) AS n FROM events"), [{"n": 0}])

    def test_historical_receipts_and_devices_are_not_exposed_in_overview(self):
        self.legacy_fixture()
        upgraded = create_app(self.config)
        client = upgraded.test_client()
        login = client.post("/api/admin/login", json={"password": "regression-test-only"})
        headers = {"X-CSRF-Token": login.json["csrf_token"]}
        result = client.get("/api/admin/overview")
        for text in ("历史敏感到账通知", "old-phone", "旧金额锁"):
            self.assertNotIn(text, result.get_data(as_text=True))
        for endpoint in ("/api/admin/lanes/unlock", "/api/admin/events/old-event/resolve"):
            self.assertEqual(client.post(endpoint, headers=headers, json={}).status_code, 410)
        self.assertEqual(self.query("SELECT resolved_note FROM events"), [{"resolved_note": "保留备注"}])

    def test_restarting_existing_bulk_orders_keeps_quantities_prices_and_card_order(self):
        sold = self.create_order(quantity=3)
        delivered = self.client.post(f"/api/admin/orders/{sold['order']['id']}/confirm", headers=self.headers).json
        self.client.post(f"/api/admin/products/{self.pid}/cards", headers=self.headers, json={"codes": "NEW-CARD-1\nNEW-CARD-2"})
        pending = self.create_order(quantity=2)
        order_rows = self.query("SELECT * FROM orders ORDER BY id")
        relations = self.query("SELECT * FROM order_cards ORDER BY order_id,position")
        for _ in range(3):
            upgraded = create_app(self.config)
            self.assertEqual(self.query("SELECT * FROM orders ORDER BY id"), order_rows)
            self.assertEqual(self.query("SELECT * FROM order_cards ORDER BY order_id,position"), relations)
            client = upgraded.test_client()
            read = client.get(f"/api/orders/{sold['order']['id']}", headers={"Authorization": "Bearer " + sold["token"]}).json
            self.assertEqual(read, delivered)
            pending_read = client.get(f"/api/orders/{pending['order']['id']}", headers={"Authorization": "Bearer " + pending["token"]}).json["order"]
            self.assertEqual((pending_read["quantity"], pending_read["unit_price_cents"], pending_read["price_cents"]), (2, 1000, 2000))
            self.assertNotIn("card_codes", pending_read)
        self.assertEqual(self.query("PRAGMA foreign_key_check"), [])

    def test_changed_password_persists_across_restart_and_ignores_bootstrap_hash(self):
        password = "persistent-regression-password"
        changed = self.client.post("/api/admin/password", headers=self.headers,
            json={"current_password": "regression-test-only", "new_password": password, "confirm_password": password})
        self.assertEqual(changed.status_code, 200)
        credentials = self.query("SELECT * FROM admin_auth")
        for bootstrap in (self.password_hash, ""):
            worker = create_app({**self.config, "ADMIN_PASSWORD_HASH": bootstrap})
            self.assertEqual(self.query("SELECT * FROM admin_auth"), credentials)
            client = worker.test_client()
            self.assertEqual(client.post("/api/admin/login", json={"password": "regression-test-only"}).status_code, 401)
            self.assertEqual(client.post("/api/admin/login", json={"password": password}).status_code, 200)

    def test_new_database_rejects_old_cookie_even_with_same_secret_and_bootstrap(self):
        old_cookie = self.client.get_cookie("session").value
        old_version = self.query("SELECT session_version FROM admin_auth WHERE id=1")[0]["session_version"]
        with tempfile.TemporaryDirectory(prefix="card-new-auth-generation-") as fresh:
            fresh_config = {**self.config, "DATA_DIR": fresh}
            worker = create_app(fresh_config)
            with closing(sqlite3.connect(worker.config["DATABASE"])) as db:
                new_version = db.execute("SELECT session_version FROM admin_auth WHERE id=1").fetchone()[0]
            self.assertGreater(new_version, 0)
            self.assertNotEqual(old_version, new_version)
            replay = worker.test_client()
            replay.set_cookie("session", old_cookie)
            self.assertEqual(replay.get("/api/admin/overview").status_code, 401)
            self.assertEqual(replay.get("/api/admin/session").json, {"authenticated": False})
            self.assertEqual(replay.post("/api/admin/login", json={"password": "regression-test-only"}).status_code, 200)
            create_app(fresh_config)
            with closing(sqlite3.connect(worker.config["DATABASE"])) as db:
                self.assertEqual(db.execute("SELECT session_version FROM admin_auth WHERE id=1").fetchone()[0], new_version)


if __name__ == "__main__":
    unittest.main()
