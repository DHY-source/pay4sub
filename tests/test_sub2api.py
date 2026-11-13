import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from app import create_app
from sub2api_adapter import IntegrationError, Sub2APIClient, normalize_base_url


class FakeSub2API:
    def __init__(self):
        self.users = {"token-user-one": {"id": 1, "email": "one@example.com"},
                      "token-user-two": {"id": 2, "email": "two@example.com"}}
        self.receipts, self.calls, self.failures = {}, [], {}

    def identity(self, token):
        if token not in self.users:
            raise IntegrationError("reauth", "登录失效")
        return self.users[token]

    def history_receipt(self, token, code, user_id):
        return self.receipts.get((user_id, code))

    def redeem(self, token, code, user_id):
        self.calls.append((user_id, code))
        assert self.identity(token)["id"] == user_id
        failure = self.failures.get(code)
        if failure:
            raise failure
        receipt = {"redeemed_at": int(time.time())}
        self.receipts[user_id, code] = receipt
        return receipt


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.upstream = FakeSub2API()
        self.config = {"TESTING": True, "DATA_DIR": self.tmp.name, "SECRET_KEY": "s" * 48,
                       "ADMIN_PASSWORD_HASH": generate_password_hash("test-password-strong", method="pbkdf2:sha256:1000"),
                       "SESSION_COOKIE_SECURE": False, "TRUST_PROXY": False, "HERMES_API_TOKEN": "",
                       "SUB2API_BASE_URL": "https://sub2api.example.com", "SUB2API_ENABLED": True,
                       "SUB2API_RELAY_CONFIRMED": True, "SUB2API_TOKEN_KEY": Fernet.generate_key().decode(),
                       "SUB2API_CLIENT": self.upstream}
        self.app = create_app(self.config)
        self.service = self.app.extensions["sub2api"]
        self.client = self.app.test_client()
        response = self.client.post("/api/admin/login", json={"password": "test-password-strong"})
        self.admin = {"X-CSRF-Token": response.json["csrf_token"]}
        Path(self.tmp.name, "qr", "alipay.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        product = self.client.post("/api/admin/products", headers=self.admin, json={
            "name": "充值卡", "price_cents": 100, "active": True, "sub2api_redeem": True})
        self.pid = product.json["product"]["id"]
        self.client.post(f"/api/admin/products/{self.pid}/cards", headers=self.admin, json={"codes": "ONE\nTWO\nTHREE"})

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            with db:
                return db.execute(query, args).fetchall()

    def session(self, token="token-user-one", user_id=1):
        result = self.client.post("/api/sub2api/session", json={"token": token, "user_id": user_id})
        self.assertEqual(result.status_code, 200, result.json)
        return {"X-Sub2api-Session": result.json["session_token"]}

    def order(self, headers=None, quantity=1):
        r = self.client.post("/api/orders", headers=headers or {}, json={
            "product_id": self.pid, "channel": "alipay", "contact": "buyer@example.com", "quantity": quantity})
        self.assertEqual(r.status_code, 201, r.json)
        return r.json

    def confirm(self, order):
        r = self.client.post("/api/admin/orders/" + order["order"]["id"] + "/confirm", headers=self.admin, json={})
        self.assertEqual(r.status_code, 200, r.json)
        return r.json["order"]

    def read(self, order):
        return self.client.get("/api/orders/" + order["order"]["id"], headers={"Authorization": "Bearer " + order["token"]}).json["order"]

    def test_guest_delivery_and_existing_order_lookup_never_bind_or_redeem(self):
        order = self.order()
        self.confirm(order)
        self.session()
        found = self.client.post("/api/orders/lookup", json={"contact": "buyer@example.com"}).json["orders"][0]
        self.assertIsNone(found["redemption"])
        self.assertEqual(found["card_codes"], ["ONE"])
        self.assertFalse(self.service.process_once())
        self.assertEqual(self.upstream.calls, [])

    def test_verified_bulk_order_survives_closed_page_restart_and_repeated_confirmation(self):
        headers = self.session()
        order = self.order(headers, 3)
        self.assertEqual(order["order"]["redemption"]["status"], "awaiting_delivery")
        self.assertNotIn("card_codes", order["order"])
        self.assertFalse(self.service.process_once())
        self.confirm(order)
        self.confirm(order)
        self.assertEqual(self.sql("SELECT count(*) FROM sub2api_tasks")[0][0], 3)
        self.service = create_app(self.config).extensions["sub2api"]
        for _ in range(3):
            self.assertTrue(self.service.process_once())
        self.assertFalse(self.service.process_once())
        read = self.read(order)
        self.assertEqual(read["redemption"]["status"], "succeeded")
        self.assertEqual(read["card_codes"], ["ONE", "TWO", "THREE"])
        self.assertTrue(all(x["redeemed_at"] for x in read["redemption"]["items"]))
        self.assertEqual(len(self.upstream.calls), 3)

    def test_tampered_identity_and_expired_local_session_reserve_nothing(self):
        r = self.client.post("/api/sub2api/session", json={"token": "token-user-one", "user_id": 2})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.sql("SELECT count(*) FROM sub2api_accounts")[0][0], 0)
        headers = self.session()
        self.sql("UPDATE sub2api_sessions SET expires_at=0")
        r = self.client.post("/api/orders", headers=headers, json={"product_id": self.pid, "channel": "alipay", "contact": "buyer@example.com"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.sql("SELECT count(*) FROM orders")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM cards WHERE status='available'")[0][0], 3)

    def test_account_switch_isolated_query_and_retry_and_immutable_target(self):
        first = self.order(self.session())
        second_session = self.session("token-user-two", 2)
        second = self.order(second_session)
        mine = self.client.get("/api/sub2api/orders?offset=0", headers=second_session)
        self.assertEqual(mine.status_code, 200, mine.json)
        self.assertEqual([o["id"] for o in mine.json["orders"]], [second["order"]["id"]])
        self.assertEqual(self.client.get("/api/sub2api/orders?offset=-1", headers=second_session).status_code, 400)
        self.assertEqual(self.client.post("/api/sub2api/orders/" + first["order"]["id"] + "/retry", headers=second_session, json={}).status_code, 404)
        self.confirm(first)
        self.service.process_once()
        self.assertEqual(self.upstream.calls, [(1, "ONE")])

    def test_tokens_encrypted_not_returned_and_expired_credentials_cleared(self):
        headers = self.session()
        encrypted = self.sql("SELECT token_cipher FROM sub2api_accounts")[0][0]
        self.assertNotIn("token-user-one", encrypted)
        self.assertEqual(Fernet(self.config["SUB2API_TOKEN_KEY"].encode()).decrypt(encrypted.encode()), b"token-user-one")
        order = self.order(headers)
        for response in (self.client.get("/api/products"), self.client.get("/api/admin/overview"),
                         self.client.get("/api/sub2api/orders?offset=0", headers=headers)):
            self.assertNotIn("token-user-one", response.get_data(as_text=True))
            self.assertNotIn(encrypted, response.get_data(as_text=True))
        self.confirm(order)
        self.sql("UPDATE sub2api_accounts SET authorized_until=0")
        self.service.process_once()
        self.assertEqual(self.read(order)["redemption"]["status"], "reauth")
        self.assertEqual(self.sql("SELECT token_cipher FROM sub2api_accounts")[0][0], "")
        self.assertFalse(self.upstream.calls)
        self.session("token-user-two", 2)
        self.assertFalse(self.service.process_once())
        self.session()
        self.service.process_once()
        self.assertEqual(self.read(order)["redemption"]["status"], "succeeded")

    def test_partial_failure_retry_only_failed_card(self):
        order = self.order(self.session(), 2)
        self.upstream.failures["TWO"] = IntegrationError("failed", "无效兑换码")
        self.confirm(order)
        self.service.process_once()
        self.service.process_once()
        self.assertEqual(self.read(order)["redemption"]["status"], "partial")
        del self.upstream.failures["TWO"]
        path = "/api/admin/orders/" + order["order"]["id"] + "/redeem-retry"
        self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(self.client.post(path, headers=self.admin, json={}).status_code, 200)
        self.service.process_once()
        self.assertEqual(self.upstream.calls.count((1, "ONE")), 1)
        self.assertEqual(self.read(order)["redemption"]["status"], "succeeded")

    def test_timeout_after_upstream_commit_reconciles_without_second_redemption(self):
        order = self.order(self.session())
        self.confirm(order)
        original = self.upstream.redeem
        def ambiguous(token, code, user):
            original(token, code, user)
            raise IntegrationError("retry", "响应超时", uncertain=True)
        self.upstream.redeem = ambiguous
        self.service.process_once()
        self.assertEqual(self.read(order)["redemption"]["status"], "retry")
        self.sql("UPDATE sub2api_tasks SET next_attempt_at=0")
        self.service.process_once()
        self.assertEqual(self.read(order)["redemption"]["status"], "succeeded")
        self.assertEqual(len(self.upstream.calls), 1)

    def test_already_used_code_requires_same_account_history(self):
        order = self.order(self.session(), 2)
        self.confirm(order)
        self.upstream.failures["ONE"] = IntegrationError("used", "已使用", uncertain=True)
        self.upstream.failures["TWO"] = IntegrationError("used", "已使用", uncertain=True)
        self.upstream.receipts[1, "ONE"] = {"redeemed_at": int(time.time())}
        self.upstream.receipts[2, "TWO"] = {"redeemed_at": int(time.time())}
        self.service.process_once()
        self.service.process_once()
        states = [x["status"] for x in self.read(order)["redemption"]["items"]]
        self.assertEqual(states, ["succeeded", "failed"])

    def test_expired_delivery_failure_rolls_back_task_creation_until_full_stock_available(self):
        order = self.order(self.session(), 3)
        self.sql("UPDATE orders SET expires_at=0 WHERE id=?", (order["order"]["id"],))
        self.client.get("/api/products")  # Releases expired reservations.
        self.order(quantity=1)  # Another buyer reserves one card.
        result = self.client.post("/api/admin/orders/" + order["order"]["id"] + "/confirm", headers=self.admin, json={})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.sql("SELECT count(*) FROM sub2api_tasks")[0][0], 0)
        self.client.post(f"/api/admin/products/{self.pid}/cards", headers=self.admin, json={"codes": "FOUR"})
        self.confirm(order)
        self.assertEqual(self.sql("SELECT count(*) FROM sub2api_tasks")[0][0], 3)

    def test_expired_lease_checks_history_and_retains_sold_card(self):
        order = self.order(self.session())
        self.confirm(order)
        self.sql("UPDATE sub2api_tasks SET state='processing',lease_token='crashed',lease_until=0,attempts=1")
        self.upstream.receipts[1, "ONE"] = {"redeemed_at": int(time.time())}
        self.service = create_app(self.config).extensions["sub2api"]
        self.service.process_once()
        self.assertFalse(self.upstream.calls)
        self.assertEqual(self.read(order)["redemption"]["status"], "succeeded")
        self.assertEqual(self.sql("SELECT status FROM cards WHERE code='ONE'")[0][0], "sold")

    def test_concurrent_workers_claim_once_and_do_network_outside_transaction(self):
        order = self.order(self.session())
        self.confirm(order)
        entered, release = threading.Event(), threading.Event()
        original = self.upstream.redeem
        def blocking(*args):
            # A separate writer can acquire the SQLite lock during this network call.
            self.sql("INSERT INTO audit(created_at,action,target) VALUES (0,'network_check','test')")
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        self.upstream.redeem = blocking
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service.process_once)
            self.assertTrue(entered.wait(5))
            try:
                self.assertFalse(self.service.process_once())
            finally:
                release.set()
            self.assertTrue(first.result())
        self.assertEqual(len(self.upstream.calls), 1)

    def test_interface_failure_pauses_other_tasks_and_admin_retry_recovers(self):
        order = self.order(self.session(), 2)
        self.confirm(order)
        self.upstream.failures["ONE"] = IntegrationError("incompatible", "接口变化", uncertain=True)
        self.service.process_once()
        self.assertFalse(self.service.process_once())
        read = self.read(order)
        self.assertEqual({x["status"] for x in read["redemption"]["items"]}, {"incompatible"})
        self.assertEqual(read["card_codes"], ["ONE", "TWO"])
        self.assertTrue(self.client.get("/api/admin/sub2api").json["sub2api"]["paused"])
        self.upstream.failures.clear()
        self.client.post("/api/admin/orders/" + order["order"]["id"] + "/redeem-retry", headers=self.admin, json={})
        self.service.process_once()
        self.service.process_once()
        self.assertEqual(self.read(order)["redemption"]["status"], "succeeded")

    def test_wrong_instance_cannot_rebind_or_process_existing_jobs(self):
        order = self.order(self.session())
        self.confirm(order)
        result = self.save_config(base_url="https://other.example.com")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json["error"]["code"], "sub2api_pending_tasks")
        # Environment changes on restart must not override the saved site.
        other = create_app({**self.config, "SUB2API_BASE_URL": "https://other.example.com"}).extensions["sub2api"]
        self.assertEqual(other.base_url, self.service.base_url)
        self.assertTrue(other.process_once())
        self.assertEqual(self.upstream.calls, [(1, "ONE")])

    def test_revoked_token_reauthorization_race_does_not_discard_new_credentials(self):
        order = self.order(self.session())
        self.confirm(order)
        def revoked(*_):
            self.session()
            raise IntegrationError("reauth", "旧授权失效")
        with patch.object(self.upstream, "redeem", side_effect=revoked):
            self.service.process_once()
        self.assertEqual(self.read(order)["redemption"]["status"], "retry")
        self.service.process_once()
        self.assertEqual(self.read(order)["redemption"]["status"], "succeeded")

    def test_non_redeem_product_is_bound_for_queries_but_never_automatically_redeemed(self):
        self.client.patch(f"/api/admin/products/{self.pid}", headers=self.admin, json={"sub2api_redeem": False})
        order = self.order(self.session())
        self.confirm(order)
        self.assertEqual(self.read(order)["redemption"]["status"], "not_requested")
        self.assertFalse(self.service.process_once())

    def test_snapshot_survives_product_toggle_cancel_releases_without_tasks(self):
        order = self.order(self.session())
        self.client.patch(f"/api/admin/products/{self.pid}", headers=self.admin, json={"sub2api_redeem": False})
        self.confirm(order)
        self.assertEqual(self.sql("SELECT count(*) FROM sub2api_tasks")[0][0], 1)
        second = self.order(self.session())
        self.client.post("/api/orders/" + second["order"]["id"] + "/cancel", headers={"Authorization": "Bearer " + second["token"]})
        self.assertEqual(self.sql("SELECT count(*) FROM sub2api_tasks")[0][0], 1)

    def test_product_default_and_selective_frame_headers(self):
        result = self.client.post("/api/admin/products", headers=self.admin, json={"name": "普通卡", "price_cents": 100})
        self.assertFalse(result.json["product"]["sub2api_redeem"])
        self.assertEqual(self.client.patch(f"/api/admin/products/{self.pid}", headers=self.admin, json={"sub2api_redeem": "yes"}).status_code, 400)
        home = self.client.get("/")
        self.addCleanup(home.close)
        self.assertNotIn("X-Frame-Options", home.headers)
        self.assertIn("frame-ancestors https://sub2api.example.com", home.headers["Content-Security-Policy"])
        for path in ("/admin", "/static/admin.html"):
            with self.client.get(path) as response:
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def save_config(self, **changes):
        config = self.client.get("/api/admin/sub2api").json["sub2api"]
        data = {k: config[k] for k in ("base_url", "enabled", "relay_confirmed", "revision")}
        return self.client.post("/api/admin/sub2api", headers=self.admin, json={**data, **changes})

    def test_saved_config_is_authoritative_on_restart_and_key_is_preserved(self):
        self.session()
        encrypted = self.sql("SELECT token_cipher FROM sub2api_accounts")[0][0]
        self.assertEqual(self.save_config(enabled=False).status_code, 200)
        restarted = create_app({**self.config, "SUB2API_TOKEN_KEY": "invalid", "SUB2API_BASE_URL": "invalid"}).extensions["sub2api"]
        self.assertFalse(restarted.enabled)
        self.assertEqual(restarted.cipher.decrypt(encrypted.encode()), b"token-user-one")

    def test_generated_key_wrapped_at_rest_and_not_in_public_or_admin_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({**self.config, "DATA_DIR": directory, "SUB2API_TOKEN_KEY": ""})
            service = app.extensions["sub2api"]
            key = service._master.decrypt(service.read_config()["token_key_cipher"].encode())
            cipher = service.cipher.encrypt(b"secret-token")
            restarted = create_app({**self.config, "DATA_DIR": directory, "SUB2API_TOKEN_KEY": ""}).extensions["sub2api"]
            self.assertEqual(restarted.cipher.decrypt(cipher), b"secret-token")
            self.assertNotIn(key, Path(app.config["DATABASE"]).read_bytes())
        for path in ("/api/products", "/api/admin/sub2api", "/api/admin/overview"):
            content = self.client.get(path).data
            self.assertNotIn(self.config["SUB2API_TOKEN_KEY"].encode(), content)
            self.assertNotIn(b"token_key_cipher", content)
            self.assertNotIn(b"SECRET_KEY", content)

    def test_config_requires_admin_csrf_and_strict_fields(self):
        guest = self.app.test_client()
        self.assertEqual(guest.get("/api/admin/sub2api").status_code, 401)
        for path in ("/api/admin/sub2api", "/api/admin/sub2api/check"):
            self.assertEqual(guest.post(path, json={}).status_code, 401)
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
        for changes in ({"enabled": "yes"}, {"relay_confirmed": False}, {"base_url": ""},
                        {"base_url": "https://example.com/?token=private"}, {"base_url": "http://example.com"},
                        {"base_url": "https://u:p@example.com"}, {"revision": True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.save_config(**changes).status_code, 400)
        self.assertEqual(self.save_config(revision=0).status_code, 409)

    def test_independent_worker_applies_disable_resume_and_heartbeat_without_restart(self):
        worker = create_app(self.config).extensions["sub2api"]
        order = self.order(self.session())
        self.confirm(order)
        self.assertEqual(self.save_config(enabled=False).status_code, 200)
        self.assertFalse(worker.process_once())
        self.assertEqual(self.read(order)["redemption"]["status"], "pending")
        diag = self.client.get("/api/admin/sub2api").json["sub2api"]
        self.assertEqual(diag["worker_revision"], diag["revision"])
        self.assertGreater(diag["worker_seen"], 0)
        self.assertFalse(self.client.get("/api/products").json["sub2api_enabled"])
        self.assertEqual(self.save_config(enabled=True).status_code, 200)
        self.assertTrue(worker.process_once())
        self.assertFalse(worker.process_once())
        self.assertEqual(self.read(order)["redemption"]["status"], "succeeded")
        self.assertEqual(len(self.upstream.calls), 1)

    def test_origin_change_blocks_unfulfilled_orders_including_expired(self):
        order = self.order(self.session())
        for status in ("pending", "expired"):
            self.sql("UPDATE orders SET status=? WHERE id=?", (status, order["order"]["id"]))
            self.assertEqual(self.save_config(base_url="https://other.example.com").status_code, 409)
        self.sql("UPDATE orders SET status='cancelled' WHERE id=?", (order["order"]["id"],))
        self.assertEqual(self.save_config(base_url="https://other.example.com", enabled=False, relay_confirmed=False).status_code, 200)
        self.assertEqual(self.sql("SELECT count(*) FROM sub2api_sessions")[0][0], 0)
        with self.client.get("/") as response:
            self.assertIn("frame-ancestors https://other.example.com", response.headers["Content-Security-Policy"])

    def test_identity_result_discarded_if_configuration_changes_during_network_request(self):
        def identity(_):
            self.sql("UPDATE sub2api_config SET enabled=0,revision=revision+1")
            return {"id": 1, "email": "one@example.com"}
        with patch.object(self.upstream, "identity", side_effect=identity):
            result = self.client.post("/api/sub2api/session", json={"token": "token-user-one", "user_id": 1})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.sql("SELECT count(*) FROM sub2api_accounts")[0][0], 0)

    def test_disable_during_sent_redemption_finishes_only_current_task(self):
        order = self.order(self.session(), 2)
        self.confirm(order)
        original = self.upstream.redeem
        def redeem(*args):
            self.sql("UPDATE sub2api_config SET enabled=0,revision=revision+1")
            return original(*args)
        with patch.object(self.upstream, "redeem", side_effect=redeem):
            self.assertTrue(self.service.process_once())
        self.assertFalse(self.service.process_once())
        self.assertEqual(self.read(order)["redemption"]["status"], "partial")
        self.assertEqual(len(self.upstream.calls), 1)

    def test_connection_check_does_not_save_config_or_send_user_credentials(self):
        before = self.sql("SELECT base_url,enabled,revision FROM sub2api_config")
        with patch.object(Sub2APIClient, "check_connection", return_value={"site_name": "Test", "message": "ok"}):
            response = self.client.post("/api/admin/sub2api/check", headers=self.admin, json={"base_url": "https://new.example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(before, self.sql("SELECT base_url,enabled,revision FROM sub2api_config"))
        with patch.object(Sub2APIClient, "check_connection", side_effect=IntegrationError("incompatible", "接口不兼容")):
            self.assertEqual(self.client.post("/api/admin/sub2api/check", headers=self.admin, json={"base_url": self.service.base_url}).status_code, 503)

    def test_concurrent_startup_uses_one_generated_key(self):
        # Tables already exist; simulate the first config migration on two processes.
        self.sql("DROP TABLE sub2api_config")
        def startup(_):
            return create_app({**self.config, "SUB2API_TOKEN_KEY": ""}).extensions["sub2api"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(startup, range(2)))
        self.assertEqual(second.cipher.decrypt(first.cipher.encrypt(b"stable")), b"stable")

    def test_first_bootstrap_requires_relay_but_generates_missing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                create_app({**self.config, "DATA_DIR": directory, "SUB2API_RELAY_CONFIRMED": False})
            with self.assertRaises(RuntimeError):
                create_app({**self.config, "DATA_DIR": directory, "SUB2API_TOKEN_KEY": "invalid"})


class Response:
    def __init__(self, data, status=200):
        self.status, self.raw = status, json.dumps(data).encode()
    def read(self, limit):
        return self.raw[:limit]
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


class SequenceOpener:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class AdapterTests(unittest.TestCase):
    def client(self, *data):
        opener = SequenceOpener(data)
        return Sub2APIClient("https://sub2api.example.com", opener=opener), opener

    def response(self, data, **extra):
        return Response({"code": 0, "data": data, **extra})

    def receipt(self, **extra):
        return {"code": "CODE", "status": "used", "used_by": 1, "used_at": "2026-11-13T10:00:00Z", **extra}

    def http_error(self, status, body):
        return HTTPError("https://sub2api.example.com", status, "upstream", {}, io.BytesIO(json.dumps(body).encode()))

    def test_public_connection_probe_is_credential_free_and_tolerates_added_fields(self):
        client, opener = self.client(self.response({"site_name": "Test", "future_field": 42, "version": "99.0"}))
        result = client.check_connection()
        self.assertEqual(result["site_name"], "Test")
        self.assertNotIn("future_field", result)
        self.assertIsNone(opener.requests[0].get_header("Authorization"))
        self.assertEqual(opener.requests[0].full_url, "https://sub2api.example.com/api/v1/settings/public")
        for data in ({}, [], {"site_name": 1}):
            client, _ = self.client(self.response(data))
            with self.assertRaises(IntegrationError):
                client.check_connection()

    def test_added_fields_and_version_changes_are_ignored(self):
        for version in ("0.2.20", "0.3.0", "9.0"):
            client, opener = self.client(self.response({"id": 1, "email": "x@example.com", "new_field": True}, version=version))
            self.assertEqual(client.identity("secret-token"), {"id": 1, "email": "x@example.com"})
            self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer secret-token")
            self.assertNotIn("secret-token", opener.requests[0].full_url)

    def test_missing_fields_changed_format_and_unknown_errors_stop(self):
        for data in ({"code": 0, "data": {}}, {"code": False, "data": {"id": 1}},
                     {"success": True, "result": {"id": 1}}, {"code": 0, "data": {"id": True}}):
            client, _ = self.client(Response(data))
            with self.subTest(data=data), self.assertRaises(IntegrationError) as result:
                client.identity("token")
            self.assertEqual(result.exception.kind, "incompatible")
        for status in (400, 404, 405, 302):
            client, _ = self.client(self.http_error(status, {"new_error": "secret-token"}))
            with self.assertRaises(IntegrationError) as result:
                client.identity("token")
            self.assertEqual(result.exception.kind, "incompatible")
            self.assertNotIn("secret-token", str(result.exception))

    def test_redirects_never_forward_bearer_credentials(self):
        from sub2api_adapter import NoRedirect
        from urllib.request import Request
        req = Request("https://sub2api.example.com/api/v1/auth/me", headers={"Authorization": "Bearer secret"})
        self.assertIsNone(NoRedirect().redirect_request(req, None, 302, "redirect", {}, "https://elsewhere.example/"))

    def test_known_auth_rate_limit_and_transient_errors(self):
        for status, kind in ((401, "reauth"), (403, "reauth"), (429, "retry"), (502, "retry")):
            client, _ = self.client(self.http_error(status, {"code": "NEW_CODE", "message": "secret-token"}))
            with self.assertRaises(IntegrationError) as caught:
                client.redeem("token", "CODE", 1)
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn("secret-token", caught.exception.message)
        client, _ = self.client(URLError("token-must-not-leak"))
        with self.assertRaises(IntegrationError) as caught:
            client.redeem("token", "CODE", 1)
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("token-must-not-leak", caught.exception.message)

    def test_success_receipt_and_summary_history_fallback(self):
        client, _ = self.client(self.response(self.receipt(extra_field=True)))
        self.assertGreater(client.redeem("token", "CODE", 1)["redeemed_at"], 0)
        client, _ = self.client(self.response({"message": "success", "new_balance": 12}),
                                self.response({"items": [self.receipt()], "total": 1}))
        self.assertGreater(client.redeem("token", "CODE", 1)["redeemed_at"], 0)

    def test_history_pagination_legacy_array_and_wrong_recipient(self):
        client, opener = self.client(self.response({"items": [self.receipt(code="OTHER")], "total": 101}),
                                     self.response({"items": [self.receipt()], "total": 101}))
        self.assertTrue(client.history_receipt("token", "CODE", 1))
        self.assertIn("page=2", opener.requests[-1].full_url)
        client, _ = self.client(self.response([self.receipt()]))
        self.assertTrue(client.history_receipt("token", "CODE", 1))
        client, _ = self.client(self.response([self.receipt(used_by=2)]))
        with self.assertRaises(IntegrationError) as result:
            client.history_receipt("token", "CODE", 1)
        self.assertEqual(result.exception.kind, "failed")

    def test_invalid_receipt_timestamp_and_unknown_success_never_report_success(self):
        client, _ = self.client(self.response(self.receipt(used_at=None)))
        with self.assertRaises(IntegrationError):
            client.redeem("token", "CODE", 1)
        client, _ = self.client(self.response({"message": "OK"}), self.response({"items": [], "total": 0}))
        with self.assertRaises(IntegrationError) as result:
            client.redeem("token", "CODE", 1)
        self.assertTrue(result.exception.uncertain)

    def test_fixed_https_origin_validation(self):
        for value in ("http://example.com", "https://user:pass@example.com", "https://example.com/api",
                      "https://example.com?token=x", "https://example.com;evil", "https://example.com\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_base_url(value)
