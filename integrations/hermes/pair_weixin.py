"""Pair the owner using the upstream iLink flow; credentials never go to stdout."""
import asyncio
import os
from pathlib import Path

import qrcode
from dotenv import set_key
from plugins.platforms.weixin import adapter

home = Path(os.environ.get("HERMES_HOME", "/opt/data"))


def render_qr(value, url, **kwargs):
    target = home / "weixin-login.png"
    qrcode.make(url or value).save(target)
    os.chmod(target, 0o600)
    print("QR_READY", flush=True)


async def main():
    adapter._print_qr = render_qr
    credentials = await adapter.qr_login(str(home), timeout_seconds=600)
    if not credentials:
        raise SystemExit("PAIRING_NOT_COMPLETED")
    owner = credentials.get("user_id", "")
    if not owner:
        raise SystemExit("PAIRING_NEEDS_OWNER_ID: no allowlist was enabled")
    env_file = home / ".env"
    values = {
        "WEIXIN_ACCOUNT_ID": credentials["account_id"],
        "WEIXIN_TOKEN": credentials["token"],
        "WEIXIN_BASE_URL": credentials["base_url"],
        "WEIXIN_DM_POLICY": "allowlist",
        "WEIXIN_ALLOWED_USERS": owner,
        "WEIXIN_ALLOW_ALL_USERS": "false",
        "WEIXIN_GROUP_POLICY": "disabled",
        "WEIXIN_HOME_CHANNEL": owner,
        "WEIXIN_HOME_CHANNEL_NAME": "店主微信",
    }
    for key, value in values.items():
        set_key(str(env_file), key, str(value))
    os.chmod(env_file, 0o600)
    (home / "pairing-complete").write_text("Owner-only Weixin pairing complete\n")
    print("PAIRING_COMPLETE_OWNER_ONLY", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
