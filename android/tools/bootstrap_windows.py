"""Fetch a portable, checksum-verified Windows Android toolchain (~173 MB).

No system installation or PATH changes. Run from any directory with Python 3.
Metadata: Azul's official metadata API; Google's repository2-3.xml; Maven Central.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / ".build-tools"
FILES = [
    ("jre.zip", "https://cdn.azul.com/zulu/bin/zulu17.66.19-ca-jre17.0.19-win_x64.zip",
     "sha256", "d6d0802e9bb5da42a61e4891463cde880f00a7bf5fe2bd41a4ff9260e52c4ebb", "jre"),
    ("platform35.zip", "https://dl.google.com/android/repository/platform-35_r02.zip",
     "sha1", "0bb560a90a7a2cbd0dd8348224d518b638fe7949", "platform"),
    ("buildtools35.zip", "https://dl.google.com/android/repository/build-tools_r35_windows.zip",
     "sha1", "af059bb67cf7786f45ee0db85e2d24985df1b4b6", "build-tools"),
    ("ecj.jar", "https://repo.maven.apache.org/maven2/org/eclipse/jdt/ecj/3.40.0/ecj-3.40.0.jar",
     "sha1", "5c26f6a20278196f8038a284d885c3796cd7d422", None),
    ("junit.jar", "https://repo.maven.apache.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar",
     "sha1", "8ac9e16d933b6fb43bc7f576336b8f4d7eb5ba12", None),
    ("hamcrest.jar", "https://repo.maven.apache.org/maven2/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar",
     "sha1", "42a25dc3219429f0e5d060061f71acb49bf010a0", None),
]


def digest(path, algorithm):
    h = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(spec):
    name, url, algorithm, expected, extract_to = spec
    target = TOOLS / "downloads" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or digest(target, algorithm) != expected:
        request = urllib.request.Request(url, headers={"User-Agent": "PrivateCard-Build/1.0"})
        partial = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                output.write(chunk)
        if digest(partial, algorithm) != expected:
            raise RuntimeError("Checksum mismatch: " + name)
        partial.replace(target)
    if extract_to:
        destination = (TOOLS / extract_to).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        marker = destination / ".verified-extracted"
        if not marker.exists() or marker.read_text() != expected:
            with zipfile.ZipFile(target) as archive:
                for member in archive.infolist():
                    path = (destination / member.filename).resolve()
                    if not path.is_relative_to(destination):
                        raise RuntimeError("Unsafe archive member: " + member.filename)
                archive.extractall(destination)
            marker.write_text(expected)
    result = {"name": name, "url": url, "bytes": target.stat().st_size,
              "verified_algorithm": algorithm, "verified_digest": expected,
              "sha256": digest(target, "sha256")}
    print(json.dumps(result), flush=True)
    return result


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(fetch, FILES))
    (TOOLS / "toolchain-manifest.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("Toolchain verified; total downloaded bytes:", sum(row["bytes"] for row in results))
