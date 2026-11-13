"""Create a private Compose .env using only Python's standard library."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
from pathlib import Path
import re
import secrets
import sys


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), 1_000_000
    ).hex()
    return "pbkdf2:sha256:1000000$" + salt + "$" + digest


def validate_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if len(domain) > 253 or "." not in domain:
        raise ValueError("请输入完整域名，例如 cards.example.com；本机预览请用 --local。")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in domain.split(".")):
        raise ValueError("域名不能含协议、路径、端口或空格；国际域名请先转换为 Punycode。")
    if all(part.isdigit() for part in domain.split(".")):
        raise ValueError("请使用解析到服务器的域名，不要使用 IP 地址。")
    return domain


def write_env(path: Path, domain: str, password: str) -> None:
    values = {
        "DOMAIN": domain,
        "SHOP_NAME": "自用发卡小站",
        "ADMIN_PASSWORD_HASH": password_hash(password),
        "SECRET_KEY": secrets.token_hex(32),
        "ORDER_TTL_SECONDS": "1800",
    }
    # Compose single-quoted values preserve '$' in Werkzeug password hashes.
    contents = "# Private configuration. Do not commit or share.\n"
    contents += "\n".join(f"{key}='{value}'" for key, value in values.items()) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(contents)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成后台密码哈希和随机密钥；不会覆盖已有文件。")
    parser.add_argument("--domain", help="已解析到服务器的域名，不含 https://")
    parser.add_argument("--local", action="store_true", help="仅生成 localhost 演示配置")
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--password-stdin", action="store_true",
                        help="自动化用：从标准输入读一行密码；不要把密码放入命令行参数")
    args = parser.parse_args()
    if args.local and args.domain:
        parser.error("--local 和 --domain 只能选择一个")
    if args.output.exists():
        parser.error(f"{args.output} 已存在；为避免丢失密钥，不会覆盖。")
    try:
        if args.local:
            domain = "localhost"
        else:
            supplied_domain = args.domain
            if not supplied_domain:
                if not sys.stdin.isatty():
                    parser.error("非交互运行请传入 --domain 或 --local")
                supplied_domain = input("域名（例如 cards.example.com）：")
            domain = validate_domain(supplied_domain)
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
        else:
            password = getpass.getpass("设置后台密码（至少 12 个字符）：")
            if password != getpass.getpass("再次输入后台密码："):
                raise ValueError("两次密码不一致。")
        if len(password) < 12 or len(password) > 256 or not password.strip():
            raise ValueError("后台密码须为 12–256 个字符。")
        write_env(args.output, domain, password)
    except (ValueError, OSError, EOFError) as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1
    print(f"已生成 {args.output.resolve()}。请保存后台密码，勿公开此配置文件。")
    print("登录后台后添加商品、导入卡密并上传收款码；核实到账后点击发卡。")
    if args.local:
        print("本机启动：docker compose -f compose.local.yaml up -d --build")
        print("本机店铺：http://127.0.0.1:8080；后台：http://127.0.0.1:8080/admin")
    else:
        print("请确认域名解析和 80/443 端口，然后运行：docker compose up -d --build")
        print(f"店铺：https://{domain}；后台：https://{domain}/admin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
