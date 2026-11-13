"""Package the manual card shop source, excluding data and secrets."""
from pathlib import Path
import hashlib
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "app.py", "lightcard_hermes.py", "lightcard_sub2api.py", "sub2api_adapter.py", "requirements.txt", "Dockerfile", "compose.yaml", "compose.local.yaml",
    "Caddyfile", ".dockerignore", ".gitignore", ".env.example", "README.md",
    "API.md", "VERIFICATION.md", "LICENSE", "compose.host.yaml", "DEPLOYMENT.md", "HERMES_DEPLOYMENT.md", "SUB2API.md",
)
SOURCE_DIRS = ("static", "scripts", "tests", "integrations")
EXCLUDED_PARTS = {"__pycache__", "build", ".gradle", ".idea", ".git", "node_modules"}


def files():
    for filename in ROOT_FILES:
        yield ROOT / filename
    for directory in SOURCE_DIRS:
        for path in sorted((ROOT / directory).rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.name in {"local.properties", ".env"} or path.suffix in {".pyc", ".log", ".keystore", ".idsig"}:
                continue
            yield path


def main():
    destination = ROOT / "dist" / "card-shop-linux-amd64.tar.gz"
    destination.parent.mkdir(exist_ok=True)
    sources = sorted(files())
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for source in sources:
            info = archive.gettarinfo(str(source), "card-shop/" + source.relative_to(ROOT).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o644
            with source.open("rb") as content:
                archive.addfile(info, content)
    with tarfile.open(destination, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) != len(sources) or any(member.issym() or member.islnk() for member in members):
            raise RuntimeError("Archive validation failed.")
        for source in sources:
            name = "card-shop/" + source.relative_to(ROOT).as_posix()
            if archive.extractfile(name).read() != source.read_bytes():
                raise RuntimeError("Archive content mismatch: " + name)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_name(destination.name + ".sha256").write_text(digest + "  " + destination.name + "\n", encoding="ascii")
    print(f"Packaged {len(sources)} verified files; {destination.stat().st_size} bytes.")
    print(str(destination))
    print("SHA256: " + digest)


if __name__ == "__main__":
    main()
