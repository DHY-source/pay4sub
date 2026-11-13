"""Compile, test, package and debug-sign using bootstrap_windows.py's local tools."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from test_upload_worker import run_regression

ROOT = Path(__file__).resolve().parents[2]
ANDROID = ROOT / "android"
TOOLS = ROOT / ".build-tools"
BUILD = ANDROID / "app" / "build" / "portable"
OUTPUT = ANDROID / "output"


def one(directory, pattern):
    paths = list(directory.rglob(pattern))
    if len(paths) != 1:
        raise RuntimeError(f"Expected exactly one {pattern} in {directory}, found {len(paths)}")
    return paths[0]


def run(arguments, capture=False):
    # Paths/arguments are passed as a list, never interpolated into a shell command.
    result = subprocess.run([str(arg) for arg in arguments], cwd=ANDROID, text=True,
                            encoding="utf-8", errors="replace", capture_output=capture)
    if capture:
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
    result.check_returncode()
    return result


def certificate_digest(output):
    match = re.search(r"Signer #1 certificate SHA-256 digest:\s*([a-fA-F0-9]+)", output)
    if not match:
        raise RuntimeError("APK signing certificate was not reported by apksigner.")
    return match.group(1).lower()


def clean_build_directory():
    # Never follow an unexpected reparse point outside the known generated tree.
    expected = ROOT.resolve() / "android" / "app" / "build" / "portable"
    resolved = BUILD.resolve()
    if resolved != expected or not resolved.is_relative_to(ROOT.resolve()):
        raise RuntimeError("Refusing to clean an unexpected build directory: " + str(resolved))
    if BUILD.is_symlink():
        raise RuntimeError("Refusing to clean a symlinked build directory.")
    print("Reset generated build directory only: " + str(resolved))
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def main():
    if os.name != "nt":
        raise RuntimeError("This optional portable build script targets Windows. Use Gradle on other hosts.")
    java = one(TOOLS / "jre", "java.exe")
    keytool = one(TOOLS / "jre", "keytool.exe")
    android_jar = one(TOOLS / "platform", "android.jar")
    aapt = one(TOOLS / "build-tools", "aapt2.exe")
    zipalign = one(TOOLS / "build-tools", "zipalign.exe")
    d8 = one(TOOLS / "build-tools", "d8.jar")
    lambda_stubs = one(TOOLS / "build-tools", "core-lambda-stubs.jar")
    signer = one(TOOLS / "build-tools", "apksigner.jar")
    apk = OUTPUT / "card-listener-debug.apk"
    previous_signer = certificate_digest(run([java, "-jar", signer, "verify", "--print-certs", apk], capture=True).stdout) if apk.exists() else None
    gradle = (ANDROID / "app/build.gradle").read_text(encoding="utf-8")
    version_code = int(re.search(r"\bversionCode\s+(\d+)", gradle).group(1))
    version_name = re.search(r"\bversionName\s+['\"]([^'\"]+)['\"]", gradle).group(1)
    ecj = TOOLS / "downloads" / "ecj.jar"
    junit = TOOLS / "downloads" / "junit.jar"
    hamcrest = TOOLS / "downloads" / "hamcrest.jar"
    clean_build_directory()
    classes = BUILD / "classes"
    tests = BUILD / "tests"
    dex = BUILD / "dex"
    generated = BUILD / "generated"
    for path in (classes, tests, dex, generated, OUTPUT):
        path.mkdir(parents=True, exist_ok=True)
    namespace = "http://schemas.android.com/apk/res/android"
    ET.register_namespace("android", namespace)
    manifest = ET.parse(ANDROID / "app" / "src" / "main" / "AndroidManifest.xml")
    root = manifest.getroot()
    root.set("package", "cn.privatecard.listener")
    root.set("{" + namespace + "}versionCode", str(version_code))
    root.set("{" + namespace + "}versionName", version_name)
    ET.SubElement(root, "uses-sdk", {"{" + namespace + "}minSdkVersion": "26", "{" + namespace + "}targetSdkVersion": "35"})
    root.find("application").set("{" + namespace + "}debuggable", "true")
    manifest_path = BUILD / "AndroidManifest.xml"
    manifest.write(manifest_path, encoding="utf-8", xml_declaration=True)
    resource_dir = ANDROID / "app" / "src" / "main" / "res"
    compiled_resources = BUILD / "compiled-res.zip"
    # AAPT2's Windows directory scanner does not reliably handle non-ASCII
    # absolute paths. Relative arguments also keep resources portable here.
    run([aapt, "compile", "--dir", resource_dir.relative_to(ANDROID),
         "-o", compiled_resources.relative_to(ANDROID)])
    unsigned = BUILD / "unsigned.apk"
    run([aapt, "link", "--manifest", manifest_path.relative_to(ANDROID),
         "-I", os.path.relpath(android_jar, ANDROID), "--debug-mode",
         "--java", generated.relative_to(ANDROID), "-o", unsigned.relative_to(ANDROID),
         compiled_resources.relative_to(ANDROID)])
    main_sources = sorted((ANDROID / "app" / "src" / "main" / "java").rglob("*.java"))
    generated_sources = sorted(generated.rglob("*.java"))
    test_sources = sorted((ANDROID / "app" / "src" / "test" / "java").rglob("*.java"))
    run([java, "-version"])
    # Android's SDK jar supplies java.* stubs. Use Java 8 bootclasspath mode to
    # avoid combining those stubs with the host JRE's Java 9+ module system.
    # The source uses only Java 8 syntax; Gradle's Java 17 path remains supported.
    run([java, "-jar", ecj, "-1.8", "-encoding", "UTF-8", "-proc:none", "-maxProblems", "30",
         "-bootclasspath", os.pathsep.join(map(str, [android_jar, lambda_stubs])),
         "-d", classes, *main_sources, *generated_sources])
    test_cp = os.pathsep.join(map(str, [classes, junit, hamcrest]))
    run([java, "-jar", ecj, "-17", "-encoding", "UTF-8", "-proc:none", "-classpath", test_cp,
         "-d", tests, *test_sources])
    junit_classes = [path.relative_to(ANDROID / "app/src/test/java").with_suffix("").as_posix().replace("/", ".")
                     for path in test_sources if path.name.endswith("Test.java")]
    junit_result = run([java, "-cp", os.pathsep.join([str(tests), test_cp]),
                        "org.junit.runner.JUnitCore", *junit_classes], capture=True)
    junit_summary = re.search(r"OK \((\d+) tests?\)", junit_result.stdout)
    if not junit_summary:
        raise RuntimeError("JUnit did not report a successful test count.")
    regressions = run_regression(java=java, ecj=ecj)
    class_jar = BUILD / "classes.jar"
    with zipfile.ZipFile(class_jar, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(classes.rglob("*.class")):
            archive.write(path, path.relative_to(classes).as_posix())
    run([java, "-cp", d8, "com.android.tools.r8.D8", "--debug", "--lib", android_jar,
         "--min-api", "26", "--output", dex, class_jar])
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(dex.glob("classes*.dex")):
            archive.write(path, path.name)
    aligned = BUILD / "aligned.apk"
    run([zipalign, "-f", "-p", "4", unsigned, aligned])
    key = TOOLS / "debug.keystore"
    if not key.exists():
        run([keytool, "-genkeypair", "-keystore", key, "-alias", "androiddebugkey", "-storepass", "android",
             "-keypass", "android", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-dname", "CN=Android Debug,O=Android,C=US"])
    run([java, "-jar", signer, "sign", "--ks", key, "--ks-key-alias", "androiddebugkey",
         "--ks-pass", "pass:android", "--key-pass", "pass:android", "--out", apk, aligned])
    verified = run([java, "-jar", signer, "verify", "--verbose", "--print-certs", apk], capture=True)
    current_signer = certificate_digest(verified.stdout)
    if previous_signer is not None and current_signer != previous_signer:
        raise RuntimeError("APK signer changed; the previous app cannot be upgraded in place.")
    run([zipalign, "-c", "-v", "4", apk])
    run([aapt, "dump", "badging", apk])
    sha256 = hashlib.sha256(apk.read_bytes()).hexdigest()
    (OUTPUT / "card-listener-debug.apk.sha256").write_text(sha256 + "  card-listener-debug.apk\n", encoding="ascii")
    report = {"apk": str(apk.relative_to(ROOT)), "bytes": apk.stat().st_size, "sha256": sha256,
              "version_code": version_code, "version_name": version_name,
              "resources_compiled": len(list(resource_dir.rglob("*.xml"))),
              "java_sources_compiled": len(main_sources), "junit_tests": int(junit_summary.group(1)),
              "worker_regression_tests": regressions["worker"]["passed"],
              "transport_acknowledgement_tests": regressions["acknowledgement"]["passed"],
              "slow_batch_max_heartbeat_gap_ms": regressions["worker"]["slow_batch_max_heartbeat_gap_ms"],
              "slow_batch_stale_events": regressions["worker"]["slow_batch_stale_events"],
              "signer_certificate_sha256": current_signer,
              "same_signer_as_previous_apk": current_signer == previous_signer if previous_signer else None,
              "min_sdk": 26, "target_sdk": 35, "device_execution": "not performed"}
    (OUTPUT / "build-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
