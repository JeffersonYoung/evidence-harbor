"""Build local marketplace copies without modifying any agent's settings or credentials."""

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def prepare(destination, python):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Destination must not exist; choose a fresh output directory")
    interpreter = Path(python).absolute()  # Preserve virtualenv symlink; resolving it selects system Python.
    if not interpreter.is_file():
        raise ValueError("Python interpreter must exist")
    for client in ("codex", "claude"):
        root = destination / client
        plugin = root / "agent-plugin"
        shutil.copytree(HERE / "agent-plugin", plugin)
        server = {"command": str(interpreter), "args": ["-m", "backend.mcp_server"]}
        if client == "codex":
            server["env_vars"] = ["EVIDENCEHARBOR_API_URL", "EVIDENCEHARBOR_API_TOKEN", "EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS"]
            shutil.copytree(HERE / ".agents", root / ".agents")
            shutil.rmtree(plugin / ".claude-plugin")
        else:
            server["env"] = {
                key: "${" + key + "}" for key in ("EVIDENCEHARBOR_API_URL", "EVIDENCEHARBOR_API_TOKEN")
            }
            shutil.copytree(HERE / ".claude-plugin", root / ".claude-plugin")
            shutil.rmtree(plugin / ".codex-plugin")
        dump(plugin / ".mcp.json", {"mcpServers": {"evidenceharbor": server}})
    # Direct MCP fallback for clients without plugin support. No secrets are stored.
    command = json.dumps(str(interpreter))
    (destination / "codex-mcp.toml").write_text(
        "[mcp_servers.evidenceharbor]\n"
        + f"command = {command}\n"
        + 'args = ["-m", "backend.mcp_server"]\n'
        + 'env_vars = ["EVIDENCEHARBOR_API_URL", "EVIDENCEHARBOR_API_TOKEN", "EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS"]\n'
        + "startup_timeout_sec = 20\ntool_timeout_sec = 65\n"
        + 'default_tools_approval_mode = "prompt"\n'
    )
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--python", default=sys.executable, help="Interpreter with EvidenceHarbor installed")
    args = parser.parse_args()
    print(prepare(args.output, args.python))
