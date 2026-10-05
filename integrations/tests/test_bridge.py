"""Exercise the actual Node -> Python MCP client -> core MCP -> HTTP route chain."""

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("node"), "Node required for bridge test")
class BridgeTests(unittest.TestCase):
    def test_real_stdio_bridge_forwards_auth_and_backend_result(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                payload = json.dumps(
                    {"id": "11111111-1111-4111-8111-111111111111", "name": "Fixture"}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ.copy()
            env.update(
                {
                    "EVIDENCEHARBOR_PYTHON": sys.executable,
                    "EVIDENCEHARBOR_API_URL": f"http://127.0.0.1:{server.server_port}",
                    "EVIDENCEHARBOR_API_TOKEN": "unit-test-fixture-not-a-credential",
                }
            )
            module = (ROOT / "integrations/openclaw/dist/bridge.js").as_uri()
            result = subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    (
                        f"import {{callMcp}} from {json.dumps(module)}; console.log(JSON.stringify(await "
                        "callMcp('get_project_context', {project_id:'11111111-1111-4111-8111-111111111111'})));"
                    ),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertFalse(data.get("isError", False))
            self.assertIn("11111111-1111-4111-8111-111111111111", json.dumps(data))
            self.assertEqual(
                requests,
                [
                    (
                        "/v1/projects/11111111-1111-4111-8111-111111111111/context?offset=0&limit=20",
                        "Bearer unit-test-fixture-not-a-credential",
                    )
                ],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
