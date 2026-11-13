import unittest
import test_app


class HermesIntegrationTests(unittest.TestCase):
    def setUp(self):
        test_app.ShopTests.setUpClass()
        self.shop = test_app.ShopTests(methodName="runTest")
        self.shop.setUp()
        self.addCleanup(self.shop.doCleanups)
        self.client = self.shop.client
        self.shop.app.config["HERMES_API_TOKEN"] = "h" * 48
        self.auth = {"Authorization": "Bearer " + "h" * 48}

    def test_private_auth_cannot_use_admin_cookie_or_token_for_admin(self):
        self.assertEqual(self.client.get("/_hermes/inventory").status_code, 401)
        anonymous = self.shop.app.test_client()
        self.assertEqual(anonymous.get("/api/admin/products", headers=self.auth).status_code, 401)
        self.shop.app.config["HERMES_API_TOKEN"] = ""
        self.assertEqual(self.client.get("/_hermes/inventory", headers=self.auth).status_code, 401)

    def test_inventory_matches_reservations_without_card_secrets(self):
        self.shop.order()
        result = self.client.get("/_hermes/inventory", headers=self.auth)
        self.assertEqual(result.json["products"][0]["inventory"], {"available": 2, "reserved": 1, "sold": 0})
        self.assertNotIn("CARD-ONE", result.get_data(as_text=True))
        self.assertEqual(result.headers["Cache-Control"], "no-store")

    def test_import_deduplicates_and_validates(self):
        path = f"/_hermes/products/{self.shop.pid}/cards"
        result = self.client.post(path, headers=self.auth, json={"codes": "NEW-CARD\nCARD-ONE"})
        self.assertEqual(result.json, {"inserted": 1, "duplicates": 1})
        self.assertEqual(self.client.post(path, headers=self.auth, json={"codes": ""}).status_code, 400)

    def test_delivery_requires_explicit_confirmation_and_matching_order(self):
        order = self.shop.order().json["order"]
        path = f"/_hermes/orders/{order['id']}/confirm"
        for data in ({}, {"payment_verified": "true"}, {"payment_verified": True, "expected_total_cents": 1, "expected_quantity": 1}):
            self.assertEqual(self.client.post(path, headers=self.auth, json=data).status_code, 409)
        self.assertEqual(self.shop.sql("SELECT status FROM orders")[0][0], "pending")

    def test_delivery_is_atomic_idempotent_and_redacts_cards(self):
        order = self.shop.order(quantity=2).json["order"]
        path = f"/_hermes/orders/{order['id']}/confirm"
        data = {"payment_verified": True, "expected_total_cents": order["price_cents"], "expected_quantity": 2}
        for _ in range(2):
            result = self.client.post(path, headers=self.auth, json=data)
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json["order"]["status"], "paid")
            self.assertNotIn("card_codes", result.json["order"])
        self.assertEqual(self.shop.sql("SELECT count(*) FROM cards WHERE status='sold'")[0][0], 2)
        result = self.client.get("/_hermes/orders?status=all", headers=self.auth)
        self.assertNotIn("CARD-ONE", result.get_data(as_text=True))

    def test_notifications_are_durable_and_retries_are_scheduled(self):
        self.shop.order()
        result = self.client.get("/_hermes/notifications", headers=self.auth).json
        self.assertEqual(len(result["notifications"]), 1)
        nid = result["notifications"][0]["id"]
        path = f"/_hermes/notifications/{nid}/result"
        self.assertEqual(self.client.post(path, headers=self.auth, json={"delivered": False}).status_code, 200)
        self.assertEqual(self.client.get("/_hermes/notifications", headers=self.auth).json["notifications"], [])
        self.shop.sql("UPDATE hermes_notifications SET next_attempt_at=0")
        self.assertEqual(len(self.client.get("/_hermes/notifications", headers=self.auth).json["notifications"]), 1)
        self.client.post(path, headers=self.auth, json={"delivered": True})
        self.client.post(path, headers=self.auth, json={"delivered": False})
        self.assertEqual(self.shop.sql("SELECT state FROM hermes_notifications")[0][0], "sent")

    def test_completed_orders_do_not_send_stale_reminders(self):
        created = self.shop.order()
        self.shop.confirm(created)
        self.assertEqual(self.client.get("/_hermes/notifications", headers=self.auth).json["notifications"], [])
        self.assertEqual(self.shop.sql("SELECT state FROM hermes_notifications")[0][0], "skipped")

    def test_integration_disabled_does_not_queue(self):
        self.shop.app.config["HERMES_API_TOKEN"] = ""
        self.shop.order()
        self.assertEqual(self.shop.sql("SELECT count(*) FROM hermes_notifications")[0][0], 0)
