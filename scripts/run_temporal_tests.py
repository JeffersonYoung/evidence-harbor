#!/usr/bin/env python3
"""Run optional Temporal checks against an official ephemeral dev server (CI helper)."""

import asyncio
import os
import sys
import tempfile

from temporalio.testing import WorkflowEnvironment


async def main():
    with tempfile.TemporaryDirectory(prefix="eh-temporal-ci-") as directory:
        async with await WorkflowEnvironment.start_local(download_dest_dir=directory) as environment:
            env = {
                **os.environ,
                "EVIDENCEHARBOR_TEST_TEMPORAL": environment.client.service_client.config.target_host,
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_postgres_domain.py",
                "tests/test_initial_report_publication.py",
                "tests/test_vector_integration.py",
                "tests/test_temporal_integration.py",
                env=env,
            )
            raise SystemExit(await process.wait())


if __name__ == "__main__":
    asyncio.run(main())
