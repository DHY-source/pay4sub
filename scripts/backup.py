"""Online SQLite snapshot plus QR images. The archive contains private data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile


def create_backup(data_dir: Path, output: Path | None = None) -> Path:
    data_dir = data_dir.resolve()
    database = data_dir / "shop.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (output or data_dir / "backups" / f"lightcard-{stamp}.zip").resolve()
    if output.exists():
        raise FileExistsError(f"不会覆盖已有备份：{output}")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=output.parent) as temp:
        snapshot = Path(temp) / "shop.sqlite3"
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination, pages=256, sleep=0.05)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("数据库完整性检查未通过，备份未生成")
        finally:
            destination.close()
            source.close()
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle, zipfile.ZipFile(
                    handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, "shop.sqlite3")
                qr_dir = data_dir / "qr"
                if qr_dir.is_dir():
                    for item in sorted(qr_dir.iterdir()):
                        if item.is_file() and not item.is_symlink():
                            archive.write(item, f"qr/{item.name}")
                archive.writestr("backup-info.json", json.dumps({
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "database": "shop.sqlite3",
                    "contains_secrets": True,
                    "env_included": False,
                }, ensure_ascii=False, indent=2))
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="备份运行中的 SQLite 数据库和收款码图片。")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    parser.add_argument("--output", type=Path, help="可选 ZIP 路径，不会覆盖已有文件")
    args = parser.parse_args()
    try:
        path = create_backup(args.data_dir, args.output)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        parser.exit(1, f"备份失败：{exc}\n")
    print(f"已生成：{path}\n备份含卡密、订单和收款码，请妥善保管；.env 需单独备份。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
