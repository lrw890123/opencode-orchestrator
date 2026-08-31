import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tests.support.mcp_client import MCPSubprocessClient
from tests.test_mcp_protocol import INITIALIZE
from opencode_orchestrator.tools import TOOL_DEFINITIONS


ROOT = Path(__file__).parents[1]
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
PLUGIN_VALIDATOR = (
    CODEX_HOME / "skills/.system/plugin-creator/scripts/validate_plugin.py"
)
SKILL_VALIDATOR = (
    CODEX_HOME / "skills/.system/skill-creator/scripts/quick_validate.py"
)


class PluginPackageTest(unittest.TestCase):
    def test_manifest_and_mcp_configuration_match_the_plugin_contract(self):
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        mcp = json.loads((ROOT / ".mcp.json").read_text())

        self.assertEqual(manifest["name"], "opencode-orchestrator")
        self.assertEqual(manifest["version"], "2.1.5")
        self.assertEqual(
            manifest["description"],
            "Delegate approved coding tasks to OpenCode and resume Codex for review.",
        )
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertEqual(manifest["interface"]["displayName"], "OpenCode Orchestrator")
        self.assertEqual(manifest["interface"]["category"], "Developer Tools")

        server = mcp["mcpServers"]["opencode-orchestrator"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["./mcp/server.py"])
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(server["tool_timeout_sec"], 90000)
        self.assertEqual(server["default_tools_approval_mode"], "auto")
        self.assertEqual(server["tools"]["abort_task"]["approval_mode"], "prompt")
        self.assertFalse(any(str(ROOT) in json.dumps(item) for item in (manifest, mcp)))

    def test_marketplace_points_at_this_plugin_root(self):
        marketplace = json.loads(
            (ROOT / ".agents/plugins/marketplace.json").read_text()
        )

        self.assertEqual(marketplace["name"], "opencode-orchestrator-local")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "opencode-orchestrator")
        self.assertEqual(entry["source"], {"source": "local", "path": "./"})
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(entry["category"], "Developer Tools")

    @unittest.skipUnless(SKILL_VALIDATOR.is_file(), "Codex skill validator unavailable")
    def test_package_contains_one_valid_discoverable_skill(self):
        skills = list((ROOT / "skills").glob("*/SKILL.md"))

        self.assertEqual(skills, [ROOT / "skills/opencode-orchestrator/SKILL.md"])
        self.assertFalse((ROOT / "skill/opencode-orchestrator").exists())
        completed = subprocess.run(
            [sys.executable, str(SKILL_VALIDATOR), str(skills[0].parent)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_release_docs_describe_v3_policy_and_recovery_contract(self):
        documents = {
            ROOT / "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
            ROOT / "skills/opencode-orchestrator/SKILL.md": (
                ROOT / "skills/opencode-orchestrator/SKILL.md"
            ).read_text(encoding="utf-8"),
        }
        required_phrases = (
            "permission_policy",
            "progress_policy",
            "STALLED",
            "external_directory",
            "不消耗 Codex token",
            "production_supported=false",
            "task_status",
            "same task",
            "waiting_permission",
            "kind=continue",
            "remember_for_task",
            "SUPERSEDED",
        )
        for path, document in documents.items():
            for phrase in required_phrases:
                self.assertIn(phrase, document, f"{phrase!r} missing from {path}")
        self.assertIn("schema_version: 3", documents[ROOT / "README.md"])

    @unittest.skipUnless(PLUGIN_VALIDATOR.is_file(), "Codex plugin validator unavailable")
    def test_official_plugin_validator_accepts_the_package(self):
        with TemporaryDirectory() as tmp:
            plugin_root = Path(tmp) / "opencode-orchestrator"
            self.copy_runtime_package(plugin_root)

            completed = subprocess.run(
                [sys.executable, str(PLUGIN_VALIDATOR), str(plugin_root)],
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_configured_mcp_starts_from_a_copied_plugin_cache(self):
        with TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            plugin_root = temporary / "opencode-orchestrator"
            self.copy_runtime_package(plugin_root)
            configured = json.loads((plugin_root / ".mcp.json").read_text())[
                "mcpServers"
            ]["opencode-orchestrator"]
            script = (plugin_root / configured["cwd"] / configured["args"][0]).resolve()
            self.assertTrue(script.is_relative_to(plugin_root.resolve()))
            client = MCPSubprocessClient(script, state_root=temporary / "state")
            try:
                initialized = client.request(INITIALIZE)
                self.assertEqual(initialized["result"]["serverInfo"]["version"], "2.1.5")
                client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                listed = client.request(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                )
                cached_definitions = listed["result"]["tools"]
                self.assertEqual(len(cached_definitions), 8)
                self.assertEqual(cached_definitions, list(TOOL_DEFINITIONS))
                self.assertTrue(
                    all(
                        tool["outputSchema"]["properties"]["schema_version"]["const"] == 3
                        for tool in cached_definitions
                    )
                )
            finally:
                _, stderr = client.close()
            self.assertEqual(client.process.returncode, 0, stderr)

    @staticmethod
    def copy_runtime_package(destination: Path) -> None:
        destination.mkdir(parents=True)
        for relative in (
            ".codex-plugin",
            ".mcp.json",
            "bin",
            "mcp",
            "skills",
            "src",
        ):
            source = ROOT / relative
            target = destination / relative
            if source.is_dir():
                shutil.copytree(
                    source,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                shutil.copy2(source, target)


if __name__ == "__main__":
    unittest.main()
