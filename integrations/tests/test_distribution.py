import asyncio
import importlib.util
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare", ROOT / "prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class DistributionTests(unittest.TestCase):
    def test_generated_marketplaces_resolve_and_do_not_embed_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = prepare.prepare(Path(temporary) / "bundle", sys.executable)
            for client, folder in (("codex", ".agents/plugins"), ("claude", ".claude-plugin")):
                root = output / client
                catalog = json.loads((root / folder / "marketplace.json").read_text())
                source = catalog["plugins"][0]["source"]
                source = source["path"] if isinstance(source, dict) else source
                plugin = root / source
                config = json.loads((plugin / ".mcp.json").read_text())
                server = config["mcpServers"]["evidenceharbor"]
                self.assertEqual(server["command"], str(Path(sys.executable).absolute()))
                self.assertEqual(server["args"], ["-m", "backend.mcp_server"])
                self.assertTrue((plugin / "skills/evidenceharbor/SKILL.md").is_file())
                if client == "codex":
                    self.assertIn("EVIDENCEHARBOR_API_TOKEN", server["env_vars"])
                    self.assertNotIn("env", server)
                else:
                    self.assertEqual(server["env"]["EVIDENCEHARBOR_API_TOKEN"], "${EVIDENCEHARBOR_API_TOKEN}")
            config = tomllib.loads((output / "codex-mcp.toml").read_text())
            self.assertEqual(config["mcp_servers"]["evidenceharbor"]["default_tools_approval_mode"], "prompt")
            with self.assertRaises(ValueError):
                prepare.prepare(output, sys.executable)

    def test_openclaw_manifest_and_schemas_match_core(self):
        from backend.mcp_server import mcp

        actual = asyncio.run(mcp.list_tools())
        schemas = json.loads((ROOT / "openclaw/tool-schemas.json").read_text())
        actual = sorted(
            [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in actual],
            key=lambda t: t["name"],
        )
        self.assertEqual(schemas, actual, "Run python -m integrations.export_tool_schemas")
        manifest = json.loads((ROOT / "openclaw/openclaw.plugin.json").read_text())
        names = [f"evidenceharbor_{tool['name']}" for tool in schemas]
        self.assertEqual(names, manifest["contracts"]["tools"])
        self.assertTrue(all(manifest["toolMetadata"][name]["optional"] for name in names))
        self.assertFalse(any("publish" in name or "schedule" in name for name in names))


if __name__ == "__main__":
    unittest.main()
