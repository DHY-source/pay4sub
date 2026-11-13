"""Run the actual UploadWorker against deterministic clock/network/Android doubles."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ANDROID = ROOT / "android"


def one(directory, pattern):
    paths = list(directory.rglob(pattern))
    if len(paths) != 1:
        raise RuntimeError(f"Expected one {pattern} in {directory}; run bootstrap_windows.py first.")
    return paths[0]


def run_fixture(java, ecj, sources, main_class, prefix):
    build = ANDROID / "app" / "build"
    build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=build) as folder:
        subprocess.run([str(java), "-jar", str(ecj), "-17", "-encoding", "UTF-8",
                        "-proc:none", "-d", folder, *map(str, sources)], check=True)
        completed = subprocess.run([str(java), "-cp", folder, main_class],
                                   text=True, encoding="utf-8", capture_output=True)
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        completed.check_returncode()
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        if report["passed"] < 1:
            raise RuntimeError("Regression did not execute any checks.")
        return report


def run_regression(java=None, ecj=None):
    java = java or one(ROOT / ".build-tools" / "jre", "java.exe")
    ecj = ecj or ROOT / ".build-tools" / "downloads" / "ecj.jar"
    source = ANDROID / "app/src/main/java/cn/privatecard/listener/UploadWorker.java"
    doubles = sorted((ANDROID / "tools/worker_regression").rglob("*.java"))
    worker = run_fixture(java, ecj, [source, *doubles],
                         "cn.privatecard.listener.UploadWorkerRegression", "worker-regression-")
    production = ANDROID / "app/src/main/java/cn/privatecard/listener"
    reply_sources = [production / "Transport.java", production / "Protocol.java",
                     ANDROID / "tools/worker_regression/org/json/JSONObject.java",
                     *sorted((ANDROID / "tools/transport_regression").rglob("*.java"))]
    acknowledgement = run_fixture(java, ecj, reply_sources,
                                  "cn.privatecard.listener.TransportReplyRegression", "reply-regression-")
    return {"worker": worker, "acknowledgement": acknowledgement}


if __name__ == "__main__":
    run_regression()
