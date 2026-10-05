#!/usr/bin/env python3
"""Run S3 integration against an operator-supplied official MinIO binary.

Uses disposable local test credentials, bucket and process; never connects to cloud
S3 or changes credentials in an existing deployment.
"""

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minio-binary", type=Path, required=True)
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="eh-s3-integration-") as directory:
        path = Path(directory)
        access = secrets.token_hex(12)
        secret = secrets.token_urlsafe(40)
        endpoint = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "MINIO_ROOT_USER": access,
            "MINIO_ROOT_PASSWORD": secret,
            "MINIO_BROWSER": "off",
            "MINIO_UPDATE": "off",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "NO_PROXY": "127.0.0.1,localhost",
            "EVIDENCEHARBOR_TEST_S3": endpoint,
            "S3_ACCESS_KEY_ID": access,
            "S3_SECRET_ACCESS_KEY": secret,
        }
        with (path / "minio.log").open("w") as log:
            process = subprocess.Popen(
                [str(args.minio_binary), "server", str(path / "data"), "--address", f"127.0.0.1:{port}"],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(
                            "MinIO exited before startup: "
                            + (path / "minio.log")
                            .read_text()[-2500:]
                            .replace(access, "[test-user]")
                            .replace(secret, "[test-secret]")
                        )
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise TimeoutError("MinIO startup timed out")
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "tests/test_s3_integration.py"],
                    env=env,
                    check=False,
                )
                raise SystemExit(result.returncode)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
