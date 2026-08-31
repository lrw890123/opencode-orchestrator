import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PLUGIN_NAME = "opencode-orchestrator"
MARKETPLACE_NAME = "opencode-orchestrator-local"
SELECTOR = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"


class Result:
    def __init__(self, returncode=0, payload=None, stderr=""):
        self.returncode = returncode
        self.stdout = json.dumps(payload if payload is not None else {})
        self.stderr = stderr


class FakeCodexRunner:
    def __init__(
        self,
        plugin_root: Path,
        *,
        marketplace_present=False,
        plugin_installed=False,
        plugin_version=None,
    ):
        self.plugin_root = plugin_root.resolve()
        self.marketplace_present = marketplace_present
        self.marketplace_root = self.plugin_root if marketplace_present else None
        self.plugin_installed = plugin_installed
        self.plugin_version = plugin_version
        self.commands = []
        self.plugin_list_calls = 0
        self.fail_final_plugin_list = False
        self.old_skill: Path | None = None

    def __call__(self, command):
        command = [str(item) for item in command]
        self.commands.append(command)
        tail = command[1:]
        if tail == ["plugin", "marketplace", "list", "--json"]:
            marketplaces = (
                [{"name": MARKETPLACE_NAME, "path": str(self.marketplace_root)}]
                if self.marketplace_present
                else []
            )
            return Result(payload=marketplaces)
        if len(tail) == 4 and tail[:3] == ["plugin", "marketplace", "add"]:
            self.marketplace_present = True
            self.marketplace_root = Path(tail[3]).resolve()
            return Result(payload={"name": MARKETPLACE_NAME})
        if tail == ["plugin", "add", SELECTOR, "--json"]:
            if self.old_skill is not None:
                assert self.old_skill.is_dir(), "plugin was added after moving the old Skill"
            self.plugin_installed = True
            manifest = json.loads(
                (self.marketplace_root / ".codex-plugin/plugin.json").read_text()
            )
            self.plugin_version = manifest["version"]
            return Result(payload={"name": PLUGIN_NAME, "installed": True})
        if tail == ["plugin", "list", "--json"]:
            self.plugin_list_calls += 1
            if self.fail_final_plugin_list and self.plugin_list_calls == 3:
                return Result(returncode=1, stderr="injected final verification failure")
            return Result(
                payload=(
                    [
                        {
                            "name": PLUGIN_NAME,
                            "marketplace": MARKETPLACE_NAME,
                            "version": self.plugin_version or "2.1.4",
                            "installed": True,
                        }
                    ]
                    if self.plugin_installed
                    else []
                )
            )
        if tail == ["plugin", "remove", SELECTOR, "--json"]:
            self.plugin_installed = False
            self.plugin_version = None
            return Result(payload={"removed": True})
        if tail == ["plugin", "marketplace", "remove", MARKETPLACE_NAME]:
            self.marketplace_present = False
            self.marketplace_root = None
            return Result(payload={"removed": True})
        return Result(returncode=1, stderr=f"unexpected command: {command}")


class PluginInstallTest(unittest.TestCase):
    def make_old_skill(self, codex_home: Path) -> Path:
        old_skill = codex_home / "skills/opencode-orchestrator"
        old_skill.mkdir(parents=True)
        (old_skill / "SKILL.md").write_bytes(b"---\nname: opencode-orchestrator\n---\nold skill\n")
        (old_skill / "config.json").write_bytes(b'{"server":"old"}\n')
        return old_skill

    def make_prior_cache(self, codex_home: Path, version="2.0.1") -> Path:
        cache = (
            codex_home
            / "plugins/cache"
            / MARKETPLACE_NAME
            / PLUGIN_NAME
            / version
        )
        shutil.copytree(ROOT, cache)
        manifest_path = cache / ".codex-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = version
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (cache / "prior-version-marker.txt").write_text(
            "prior-cache-bytes", encoding="utf-8"
        )
        return cache

    def test_preinstall_validates_and_registers_without_moving_old_skill(self):
        from scripts.install_plugin import preinstall

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            old_skill = self.make_old_skill(codex_home)
            before = {path.name: path.read_bytes() for path in old_skill.iterdir()}
            record = root / "record/install.json"
            runner = FakeCodexRunner(ROOT)

            result = preinstall(ROOT, codex_home, "codex", runner, record)

            self.assertEqual(result["status"], "PREINSTALLED")
            self.assertEqual(
                {path.name: path.read_bytes() for path in old_skill.iterdir()},
                before,
            )
            self.assertIn(
                ["codex", "plugin", "marketplace", "add", str(ROOT.resolve())],
                runner.commands,
            )
            persisted = json.loads(record.read_text())
            self.assertEqual(persisted["status"], "PREINSTALLED")
            self.assertEqual(persisted["plugin"]["version"], "2.1.4")
            self.assertTrue(persisted["checks"]["mcp_handshake"])
            self.assertEqual(persisted["plugin"]["tool_timeout_sec"], 90000)

            with self.assertRaises(FileExistsError):
                preinstall(ROOT, codex_home, "codex", runner, record)

    def test_activation_moves_old_skill_only_after_plugin_install_and_rollback_restores_it(self):
        from scripts.install_plugin import activate, preinstall, rollback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            old_skill = self.make_old_skill(codex_home)
            old_bytes = {
                path.relative_to(old_skill): path.read_bytes()
                for path in old_skill.rglob("*")
                if path.is_file()
            }
            record = root / "record/install.json"
            runner = FakeCodexRunner(ROOT)
            runner.old_skill = old_skill
            preinstall(ROOT, codex_home, "codex", runner, record)
            activation_command_start = len(runner.commands)

            activated = activate(record, runner=runner)

            self.assertTrue(activated["restart_required"])
            self.assertFalse(old_skill.exists())
            backup = record.parent / "backup/skill"
            self.assertTrue(backup.is_dir())
            activation_commands = runner.commands[activation_command_start:]
            plugin_add_index = activation_commands.index(
                ["codex", "plugin", "add", SELECTOR, "--json"]
            )
            self.assertLess(
                plugin_add_index,
                activation_commands.index(["codex", "plugin", "list", "--json"]),
            )

            rolled_back = rollback(record, runner=runner)

            self.assertEqual(rolled_back["status"], "ROLLED_BACK")
            self.assertEqual(
                {
                    path.relative_to(old_skill): path.read_bytes()
                    for path in old_skill.rglob("*")
                    if path.is_file()
                },
                old_bytes,
            )
            self.assertIn(
                ["codex", "plugin", "remove", SELECTOR, "--json"],
                runner.commands,
            )
            self.assertIn(
                ["codex", "plugin", "marketplace", "remove", MARKETPLACE_NAME],
                runner.commands,
            )

    def test_rollback_preserves_a_marketplace_that_existed_before_preinstall(self):
        from scripts.install_plugin import activate, preinstall, rollback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            self.make_old_skill(codex_home)
            record = root / "record/install.json"
            runner = FakeCodexRunner(ROOT, marketplace_present=True)
            preinstall(ROOT, codex_home, "codex", runner, record)
            activate(record, runner=runner)

            rollback(record, runner=runner)

            self.assertNotIn(
                ["codex", "plugin", "marketplace", "remove", MARKETPLACE_NAME],
                runner.commands,
            )
            self.assertTrue(runner.marketplace_present)

    def test_interrupted_activation_record_can_be_rolled_back_after_skill_move(self):
        from scripts.install_plugin import activate, preinstall, rollback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            old_skill = self.make_old_skill(codex_home)
            expected_config = (old_skill / "config.json").read_bytes()
            record = root / "record/install.json"
            runner = FakeCodexRunner(ROOT)
            preinstall(ROOT, codex_home, "codex", runner, record)
            runner.fail_final_plugin_list = True

            with self.assertRaises(RuntimeError):
                activate(record, runner=runner)

            interrupted = json.loads(record.read_text())
            self.assertEqual(interrupted["status"], "ACTIVATION_FAILED")
            self.assertFalse(old_skill.exists())
            rollback(record, runner=runner)
            self.assertEqual((old_skill / "config.json").read_bytes(), expected_config)
            self.assertEqual(json.loads(record.read_text())["status"], "ROLLED_BACK")

    def test_rollback_reconciles_marketplace_add_left_at_intent_after_registration(self):
        from scripts.install_plugin import preinstall, rollback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            record = root / "record/install.json"
            runner = FakeCodexRunner(ROOT)
            preinstall(ROOT, codex_home, "codex", runner, record)
            interrupted = json.loads(record.read_text())
            marketplace_add = next(
                item for item in interrupted["mutations"] if item["kind"] == "marketplace_add"
            )
            marketplace_add["state"] = "INTENT"
            record.write_text(json.dumps(interrupted), encoding="utf-8")

            rolled_back = rollback(record, runner=runner)

            self.assertEqual(rolled_back["status"], "ROLLED_BACK")
            self.assertFalse(runner.marketplace_present)
            self.assertEqual(marketplace_add["kind"], "marketplace_add")

    def test_rollback_reconciles_plugin_add_left_at_intent_after_install(self):
        from scripts.install_plugin import preinstall, rollback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            record = root / "record/install.json"
            runner = FakeCodexRunner(ROOT)
            preinstall(ROOT, codex_home, "codex", runner, record)
            interrupted = json.loads(record.read_text())
            interrupted["mutations"].append(
                {
                    "kind": "plugin_add",
                    "state": "INTENT",
                    "command": ["codex", "plugin", "add", SELECTOR, "--json"],
                }
            )
            record.write_text(json.dumps(interrupted), encoding="utf-8")
            runner.plugin_installed = True
            runner.plugin_version = "2.1.4"

            rollback(record, runner=runner)

            self.assertFalse(runner.plugin_installed)
            self.assertIn(
                ["codex", "plugin", "remove", SELECTOR, "--json"],
                runner.commands,
            )

    def test_rollback_reconciles_skill_move_left_at_intent_after_move(self):
        from scripts.install_plugin import preinstall, rollback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            old_skill = self.make_old_skill(codex_home)
            expected = (old_skill / "config.json").read_bytes()
            record = root / "record/install.json"
            runner = FakeCodexRunner(ROOT)
            preinstall(ROOT, codex_home, "codex", runner, record)
            interrupted = json.loads(record.read_text())
            backup = record.parent / "backup/skill"
            backup.parent.mkdir(parents=True, exist_ok=True)
            interrupted["mutations"].append(
                {
                    "kind": "skill_move",
                    "state": "INTENT",
                    "source": str(old_skill),
                    "destination": str(backup),
                    "original_fingerprint": interrupted["old_skill"]["fingerprint"],
                }
            )
            record.write_text(json.dumps(interrupted), encoding="utf-8")
            old_skill.replace(backup)

            rollback(record, runner=runner)

            self.assertEqual((old_skill / "config.json").read_bytes(), expected)
            self.assertFalse(backup.exists())

    def test_upgrade_snapshot_restores_prior_version_source_and_cache_bytes(self):
        from scripts.install_plugin import activate, preinstall, rollback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            prior_cache = self.make_prior_cache(codex_home)
            record = root / "record/install.json"
            runner = FakeCodexRunner(
                ROOT,
                marketplace_present=True,
                plugin_installed=True,
                plugin_version="2.0.1",
            )

            preinstalled = preinstall(ROOT, codex_home, "codex", runner, record)
            snapshot = Path(preinstalled["previous"]["cache"]["snapshot_path"])
            self.assertTrue(snapshot.is_dir())
            self.assertEqual(
                (snapshot / "prior-version-marker.txt").read_text(),
                "prior-cache-bytes",
            )
            self.assertEqual(
                preinstalled["previous"]["cache"]["fingerprint"],
                preinstalled["previous"]["cache"]["snapshot_fingerprint"],
            )
            activate(record, runner=runner)
            self.assertEqual(runner.plugin_version, "2.1.4")

            rolled_back = rollback(record, runner=runner)

            self.assertEqual(rolled_back["status"], "ROLLED_BACK")
            self.assertTrue(runner.plugin_installed)
            self.assertEqual(runner.plugin_version, "2.0.1")
            self.assertTrue(runner.marketplace_present)
            self.assertEqual(runner.marketplace_root, ROOT.resolve())
            self.assertEqual(
                (prior_cache / "prior-version-marker.txt").read_text(),
                "prior-cache-bytes",
            )

    def test_upgrade_records_recovery_identity_before_snapshot_copy(self):
        from scripts.install_plugin import preinstall

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            self.make_prior_cache(codex_home)
            record = root / "record/install.json"
            runner = FakeCodexRunner(
                ROOT,
                marketplace_present=True,
                plugin_installed=True,
                plugin_version="2.0.1",
            )
            real_copytree = shutil.copytree

            def crash_after_snapshot(source, destination, *args, **kwargs):
                copied = real_copytree(source, destination, *args, **kwargs)
                if Path(destination).name == "previous-plugin-root":
                    raise RuntimeError("injected snapshot crash")
                return copied

            with patch(
                "scripts.install_plugin.shutil.copytree",
                side_effect=crash_after_snapshot,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected snapshot crash"):
                    preinstall(ROOT, codex_home, "codex", runner, record)

            persisted = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "PREINSTALLING")
            self.assertTrue(persisted["previous"]["plugin_installed"])
            self.assertEqual(persisted["previous"]["plugin"]["version"], "2.0.1")
            self.assertEqual(persisted["previous"]["cache"]["state"], "INTENT")
            self.assertIsNotNone(persisted["previous"]["cache"]["fingerprint"])
            self.assertTrue(
                Path(persisted["previous"]["cache"]["snapshot_path"]).is_dir()
            )

    def test_upgrade_rollback_reconciles_restore_command_crash_windows(self):
        from scripts.install_plugin import activate, preinstall, rollback

        scenarios = {
            "install-command-applied": (True, "install-previous-plugin"),
            "snapshot-removal-applied": (False, "remove-snapshot-marketplace"),
        }
        for label, (marketplace_present, intent_label) in scenarios.items():
            with self.subTest(label=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                codex_home = root / "codex-home"
                self.make_prior_cache(codex_home)
                record = root / "record/install.json"
                runner = FakeCodexRunner(
                    ROOT,
                    marketplace_present=True,
                    plugin_installed=True,
                    plugin_version="2.0.1",
                )
                preinstall(ROOT, codex_home, "codex", runner, record)
                activate(record, runner=runner)
                interrupted = json.loads(record.read_text(encoding="utf-8"))
                snapshot = Path(interrupted["previous"]["cache"]["snapshot_path"])
                runner.plugin_installed = True
                runner.plugin_version = "2.0.1"
                runner.marketplace_present = marketplace_present
                runner.marketplace_root = (
                    snapshot.resolve() if marketplace_present else None
                )
                intent_command = (
                    ["codex", "plugin", "add", SELECTOR, "--json"]
                    if intent_label == "install-previous-plugin"
                    else [
                        "codex",
                        "plugin",
                        "marketplace",
                        "remove",
                        MARKETPLACE_NAME,
                    ]
                )
                interrupted["mutations"].append(
                    {
                        "kind": "plugin_restore",
                        "state": "INTENT",
                        "previous_version": "2.0.1",
                        "previous_marketplace_root": str(ROOT.resolve()),
                        "snapshot_path": str(snapshot),
                        "snapshot_fingerprint": interrupted["previous"]["cache"][
                            "snapshot_fingerprint"
                        ],
                        "steps": [
                            {
                                "label": intent_label,
                                "state": "INTENT",
                                "command": intent_command,
                            }
                        ],
                    }
                )
                record.write_text(json.dumps(interrupted), encoding="utf-8")

                rollback(record, runner=runner)

                reconciled = json.loads(record.read_text(encoding="utf-8"))
                restore = next(
                    item
                    for item in reconciled["mutations"]
                    if item["kind"] == "plugin_restore"
                )
                self.assertEqual(restore["state"], "APPLIED")
                self.assertFalse(
                    any(step["state"] == "INTENT" for step in restore["steps"])
                )
                self.assertEqual(
                    sum(
                        step["label"] == intent_label
                        for step in restore["steps"]
                    ),
                    1,
                )
                self.assertTrue(runner.plugin_installed)
                self.assertEqual(runner.plugin_version, "2.0.1")
                self.assertTrue(runner.marketplace_present)
                self.assertEqual(runner.marketplace_root, ROOT.resolve())

    def test_rollback_reconciles_rollback_intents_already_applied_to_actual_state(self):
        from scripts.install_plugin import preinstall, rollback

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            record = root / "record/install.json"
            runner = FakeCodexRunner(ROOT)
            preinstall(ROOT, codex_home, "codex", runner, record)
            interrupted = json.loads(record.read_text())
            interrupted["mutations"].extend(
                [
                    {
                        "kind": "plugin_add",
                        "state": "INTENT",
                        "command": ["codex", "plugin", "add", SELECTOR, "--json"],
                    },
                    {
                        "kind": "plugin_remove",
                        "state": "INTENT",
                        "command": ["codex", "plugin", "remove", SELECTOR, "--json"],
                    },
                    {
                        "kind": "marketplace_remove",
                        "state": "INTENT",
                        "command": [
                            "codex",
                            "plugin",
                            "marketplace",
                            "remove",
                            MARKETPLACE_NAME,
                        ],
                    },
                ]
            )
            record.write_text(json.dumps(interrupted), encoding="utf-8")
            runner.plugin_installed = False
            runner.plugin_version = None
            runner.marketplace_present = False
            runner.marketplace_root = None

            rollback(record, runner=runner)
            reconciled = json.loads(record.read_text())
            states = {
                item["kind"]: item["state"] for item in reconciled["mutations"]
            }

            self.assertEqual(states["plugin_remove"], "APPLIED")
            self.assertEqual(states["marketplace_remove"], "APPLIED")


if __name__ == "__main__":
    unittest.main()
