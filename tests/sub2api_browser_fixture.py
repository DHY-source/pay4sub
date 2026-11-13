"""Local test shop with a fake Sub2API and an actual background queue worker."""
import io
import json
import secrets
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server
from app import create_app
from browser_fixture import fixture_png
from test_sub2api import FakeSub2API
from sub2api_adapter import IntegrationError


def main():
    with tempfile.TemporaryDirectory(prefix="sub2api-browser-") as directory:
        upstream = FakeSub2API()
        upstream.failures["FAIL-TWO"] = IntegrationError("failed", "测试兑换码无效，请联系店主")
        password = secrets.token_urlsafe(24)
        application = create_app({
            "DATA_DIR": directory, "SECRET_KEY": secrets.token_hex(32),
            "ADMIN_PASSWORD_HASH": generate_password_hash(password),
            "SESSION_COOKIE_SECURE": False, "SHOP_NAME": "自动兑换 · 本地验收",
            "HERMES_API_TOKEN": "", "SUB2API_BASE_URL": "https://sub2api.example.com",
            "SUB2API_ENABLED": True, "SUB2API_RELAY_CONFIRMED": True,
            "SUB2API_TOKEN_KEY": Fernet.generate_key().decode(), "SUB2API_CLIENT": upstream,
        })
        client = application.test_client()
        login = client.post("/api/admin/login", json={"password": password}).json
        headers = {"X-CSRF-Token": login["csrf_token"]}
        client.post("/api/admin/qr/alipay", headers=headers, data={"file": (io.BytesIO(fixture_png()), "test.png")})
        ids = []
        for name, codes in (("余额充值卡", "\n".join("CODE-" + str(i) for i in range(1, 11))),
                            ("异常测试卡", "FAIL-ONE\nFAIL-TWO")):
            r = client.post("/api/admin/products", headers=headers, json={
                "name": name, "price_cents": 100, "sub2api_redeem": True})
            assert r.status_code == 201, r.json
            pid = r.json["product"]["id"]
            client.post(f"/api/admin/products/{pid}/cards", headers=headers, json={"codes": codes})
            ids.append(pid)
        stop = threading.Event()
        service = application.extensions["sub2api"]
        def work():
            while not stop.is_set():
                service.process_once()
                stop.wait(0.1)
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        server = make_server("127.0.0.1", 0, application, threaded=True)
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "password": password, "products": ids}), flush=True)
        def parent_exit():
            sys.stdin.read()
            stop.set()
            server.shutdown()
        threading.Thread(target=parent_exit, daemon=True).start()
        try:
            server.serve_forever()
        finally:
            stop.set()
            worker.join(10)
            server.server_close()


if __name__ == "__main__":
    main()
