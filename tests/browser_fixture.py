"""Temporary, local-only server for browser acceptance tests; no production data."""
from __future__ import annotations

import io
import json
from pathlib import Path
import secrets
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server


def fixture_png():
    # Deliberately a checkerboard, never a real receiving QR code.
    size = 132
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            value = 35 if 12 <= x < 120 and 12 <= y < 120 and ((x // 6 + y // 6) % 3 == 0) else 255
            rows.extend((value, value, value))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')


def main():
    with tempfile.TemporaryDirectory(prefix='manual-card-browser-') as directory:
        password = secrets.token_urlsafe(24)
        application = create_app({'DATA_DIR': directory, 'SECRET_KEY': secrets.token_hex(32),
                                  'ADMIN_PASSWORD_HASH': generate_password_hash(password),
                                  'SESSION_COOKIE_SECURE': False, 'ORDER_TTL_SECONDS': 1800,
                                  'SHOP_NAME': '发卡小店 · 本地测试'})
        client = application.test_client()
        login = client.post('/api/admin/login', json={'password': password}).get_json()
        headers = {'X-CSRF-Token': login['csrf_token']}
        for payment in ('alipay', 'wechat'):
            response = client.post('/api/admin/qr/' + payment, headers=headers,
                                   data={'file': (io.BytesIO(fixture_png()), 'fixture.png')})
            assert response.status_code == 200, response.get_json()
        ids = []
        for name, price, description, stock in [('月卡', 990, '有效期 30 天，按商品说明使用。', 65),
                                                ('周卡', 490, '有效期 7 天。', 8),
                                                ('体验卡', 100, '有效期 1 天。', 0)]:
            response = client.post('/api/admin/products', headers=headers, json={
                'name': name, 'price_cents': price, 'description': description, 'active': True})
            assert response.status_code == 201, response.get_json()
            product_id = response.get_json()['product']['id']
            ids.append(product_id)
            if stock:
                response = client.post(f'/api/admin/products/{product_id}/cards', headers=headers,
                                       json={'codes': '\n'.join(f'TEST-{product_id}-{index:04}' for index in range(stock))})
                assert response.status_code == 200, response.get_json()
        response = client.post('/api/orders', json={'product_id': ids[1], 'channel': 'alipay',
                                                    'contact': 'expired.qa@example.com', 'quantity': 2})
        assert response.status_code == 201, response.get_json()
        expired_order = response.get_json()['order']['id']
        with sqlite3.connect(application.config['DATABASE']) as db:
            db.execute('UPDATE orders SET expires_at=? WHERE id=?', (int(time.time()) - 1, expired_order))
        server = make_server('127.0.0.1', 0, application, threaded=True)
        print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'password': password,
                          'products': ids, 'expired_order': expired_order}), flush=True)
        def stop_on_parent_exit():
            sys.stdin.read()
            server.shutdown()
        threading.Thread(target=stop_on_parent_exit, daemon=True).start()
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
