#!/usr/bin/env python3
"""Run true PostgreSQL/pgvector + Temporal tests in one process/network namespace.

Prerequisites: local PostgreSQL distribution (including pgvector), Temporal CLI
or permission for the SDK to fetch its official dev-server. Does not use Docker,
change system settings, provision credentials or make model/search calls.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ready(port, process, timeout=30):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError("PostgreSQL exited before startup; inspect integration logs")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("PostgreSQL did not start")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres-bin", type=Path, required=True)
    parser.add_argument("--temporal-cli", type=Path)
    parser.add_argument("--download-dir", type=Path, default=Path("/tmp/evidenceharbor-temporal"))
    args = parser.parse_args()
    from temporalio.testing import WorkflowEnvironment

    root = Path(__file__).resolve().parents[1]
    args.download_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evidenceharbor-integration-") as directory:
        work = Path(directory)
        subprocess.run(
            [
                str(args.postgres_bin / "initdb"),
                "-D",
                str(work / "pg"),
                "-A",
                "trust",
                "--no-locale",
                "-E",
                "UTF8",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        port = available_port()
        with (work / "postgres.log").open("w") as log:
            pg = subprocess.Popen(
                [
                    str(args.postgres_bin / "postgres"),
                    "-D",
                    str(work / "pg"),
                    "-p",
                    str(port),
                    "-h",
                    "127.0.0.1",
                    "-k",
                    "",
                ],
                stdout=log,
                stderr=log,
            )
            temporal = None
            try:
                ready(port, pg)
                subprocess.run(
                    [str(args.postgres_bin / "createdb"), "-h", "127.0.0.1", "-p", str(port), "research"],
                    check=True,
                )
                subprocess.run(
                    [
                        str(args.postgres_bin / "psql"),
                        "-h",
                        "127.0.0.1",
                        "-p",
                        str(port),
                        "research",
                        "-c",
                        "CREATE EXTENSION vector",
                    ],
                    check=True,
                )
                temporal = await WorkflowEnvironment.start_local(
                    port=available_port(),
                    download_dest_dir=str(args.download_dir),
                    dev_server_existing_path=str(args.temporal_cli) if args.temporal_cli else None,
                    dev_server_database_filename=str(work / "temporal.sqlite"),
                )
                env = {
                    **os.environ,
                    "EVIDENCEHARBOR_TEST_POSTGRES": f"postgresql+psycopg://{os.getenv('USER', 'agent')}@127.0.0.1:{port}/research",
                    "EVIDENCEHARBOR_TEST_TEMPORAL": temporal.client.service_client.config.target_host,
                }
                process = await asyncio.create_subprocess_exec(
                    str(root / ".venv/bin/python"),
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_postgres_domain.py",
                    "tests/test_initial_report_publication.py",
                    "tests/test_vector_integration.py",
                    "tests/test_temporal_integration.py",
                    cwd=str(root),
                    env=env,
                )
                code = await process.wait()
                if code:
                    print((work / "postgres.log").read_text()[-5000:])
                    raise SystemExit(code)
            finally:
                if temporal:
                    await temporal.shutdown()
                pg.terminate()
                try:
                    pg.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pg.kill()
                    pg.wait()


if __name__ == "__main__":
    asyncio.run(main())
