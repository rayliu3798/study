import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("learn_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "tests/.tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="install with spaces ", dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "Other user"
        self.home.mkdir()
        self.installed = self.home / ".codex/cache/plugin"
        self.plugin_id = "codex-learn@personal"
        self.hooks = [{"eventName": event, "pluginId": self.plugin_id, "key": f"{self.plugin_id}:{event}",
                       "currentHash": f"sha256:{event}", "enabled": True, "trustStatus": "untrusted",
                       "isManaged": False, "handlerType": "command", "source": "plugin",
                       "sourcePath": str(self.installed / "hooks/hooks.json"), "timeoutSec": 3,
                       "matcher": None, "command": f'python3 "{self.installed}/scripts/learn.py" hook'}
                      for event in installer.EVENTS]
        self.writes = []
        self.list_calls = 0
        self.change_during_review = False
        self.features = {"hooks": True}
        owner = self

        class FakeServer:
            def __init__(self, executable, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def call(self, method, params):
                if method == "hooks/list":
                    owner.list_calls += 1
                    hooks = json.loads(json.dumps(owner.hooks))
                    if owner.change_during_review and owner.list_calls > 1:
                        hooks[0]["currentHash"] = "sha256:changed"
                    return {"data": [{"hooks": hooks, "errors": [], "cwd": str(owner.home)}]}
                if method == "config/read":
                    return {"config": {"features": dict(owner.features)}}
                if method == "config/batchWrite":
                    owner.writes.append(params)
                    for hook in owner.hooks:
                        hook.update(trustStatus="trusted", enabled=True)
                    owner.features["hooks"] = True
                    return {"status": "ok"}
                raise AssertionError(method)

        self.server_patch = patch.object(installer, "AppServer", FakeServer)

    def approve(self, answer):
        with self.server_patch:
            return installer.approve_hooks("codex", self.home, self.plugin_id, self.installed,
                                           lambda message: answer, lambda message: None)

    def test_copy_from_outside_home_preserves_notes_and_skips_caches(self):
        target = self.home / "codex-learn"
        target.mkdir()
        (target / "plugin.json").write_text('{"name":"codex-learn"}', encoding="utf-8")
        (target / "personal-notes.md").write_text("Keep my notes", encoding="utf-8")
        installer.copy_package(ROOT, target, self.home)
        installer.validate_package(target)
        self.assertEqual((target / "personal-notes.md").read_text(), "Keep my notes")
        self.assertFalse((target / "plugin.json").exists())
        self.assertEqual(len(list(target.glob("plugin.portable-*.json.bak"))), 1)
        self.assertFalse((target / "scripts/__pycache__").exists())
        mtime = (target / "scripts/learn.py").stat().st_mtime_ns
        installer.copy_package(ROOT, target, self.home)
        self.assertEqual((target / "scripts/learn.py").stat().st_mtime_ns, mtime)

    def test_existing_marketplace_keeps_metadata_and_source_location(self):
        catalog = self.home / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        original = {"name": "my-personal", "interface": {"displayName": "My plugins"}, "plugins": [
            {"name": "other", "source": {"source": "local", "path": "./other"}},
            {"name": "codex-learn", "source": {"source": "local", "path": "./existing plugin"},
             "description": "Keep this custom metadata"}]}
        catalog.write_text(json.dumps(original), encoding="utf-8")
        calls = []

        def fake_cli(executable, *args, **kwargs):
            calls.append(args)
            if args[:3] == ("plugin", "marketplace", "list"):
                return {"marketplaces": [{"name": "my-personal", "root": str(self.home)}]}
            if args[:2] == ("plugin", "add") and "--json" in args:
                return {"installedPath": str(self.installed)}
            return "help"

        with patch.object(installer, "cli", fake_cli):
            result = installer.install(self.home, executable="codex", review=False, output=lambda message: None)
        self.assertEqual(result["plugin"], "codex-learn@my-personal")
        self.assertEqual(Path(result["source"]), self.home / "existing plugin")
        self.assertEqual(json.loads(catalog.read_text()), original)
        self.assertFalse(any(args[:3] == ("plugin", "marketplace", "add") for args in calls))
        self.assertFalse(result["hooks_ready"])

    def test_compatibility_manifest_can_be_registered_idempotently(self):
        target = self.home / "plugin"
        installer.copy_package(ROOT, target, self.home)
        result = installer.register(self.home, target)
        catalog = Path(result["marketplace"])
        mtime = catalog.stat().st_mtime_ns
        installer.register(self.home, target)
        self.assertEqual(catalog.stat().st_mtime_ns, mtime)
        self.assertFalse((target / "plugin.json").exists())

    def test_source_escape_is_rejected(self):
        catalog = self.home / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"name": "personal", "plugins": [{"name": "codex-learn",
            "source": {"source": "local", "path": "./../outside"}}]}), encoding="utf-8")
        with self.assertRaisesRegex(installer.LearnError, "inside"):
            installer.destination(self.home)

    def test_cli_failure_does_not_write_installation_files(self):
        with patch.object(installer, "cli", side_effect=installer.LearnError("unsupported CLI")):
            with self.assertRaisesRegex(installer.LearnError, "unsupported"):
                installer.install(self.home, executable="codex")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_declining_review_never_records_trust(self):
        self.assertFalse(self.approve("no"))
        self.assertEqual(self.writes, [])

    def test_approval_uses_native_hashes_and_only_this_plugins_keys(self):
        self.features["hooks"] = False
        self.assertTrue(self.approve("yes"))
        edit = self.writes[0]["edits"][0]
        self.assertEqual(edit["keyPath"], "hooks.state")
        self.assertEqual(edit["mergeStrategy"], "upsert")
        self.assertEqual(set(edit["value"]), {hook["key"] for hook in self.hooks})
        for hook in self.hooks:
            self.assertEqual(edit["value"][hook["key"]]["trusted_hash"], hook["currentHash"])
        self.assertEqual(self.writes[0]["edits"][1]["keyPath"], "features.hooks")

    def test_changed_definition_cannot_reuse_approval(self):
        self.change_during_review = True
        with self.assertRaisesRegex(installer.LearnError, "changed during review"):
            self.approve("yes")
        self.assertEqual(self.writes, [])

    def test_trusted_hooks_need_no_prompt_or_write(self):
        for hook in self.hooks:
            hook["trustStatus"] = "trusted"
        with self.server_patch:
            ready = installer.approve_hooks("codex", self.home, self.plugin_id, self.installed,
                lambda message: self.fail("Already trusted hooks should not prompt"), lambda message: None)
        self.assertTrue(ready)
        self.assertEqual(self.writes, [])

    def test_incomplete_or_foreign_hooks_are_not_approved(self):
        self.hooks.pop()
        with self.assertRaisesRegex(installer.LearnError, "four"):
            self.approve("yes")
        self.assertEqual(self.writes, [])

    @unittest.skipUnless(os.name == "nt", "Windows installer entry point")
    def test_windows_powershell_entry_point_passes_arguments(self):
        target = self.home / "extracted plugin with spaces"
        installer.copy_package(ROOT, target, self.home)
        shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(target / "install.ps1"), "--help"], capture_output=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"--no-review", result.stdout)


if __name__ == "__main__":
    unittest.main()
