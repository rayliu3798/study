import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("study_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "tests/.tmp"
        directory.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "Other user"
        self.home.mkdir()

    def test_registration_and_install_preserve_other_marketplace_entries(self):
        catalog = self.home / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        other = {"name": "other", "source": {"source": "local", "path": "./other"}}
        catalog.write_text(json.dumps({"name": "my-personal", "plugins": [other]}), encoding="utf-8")
        calls = []

        def cli(executable, *args, **kwargs):
            calls.append(args)
            if args[:3] == ("plugin", "marketplace", "list"):
                return {"marketplaces": [{"name": "my-personal", "root": str(self.home)}]}
            if args[:2] == ("plugin", "add"):
                return {"installedPath": str(self.home / "installed")}
            raise AssertionError(args)

        with patch.object(installer, "cli", cli):
            result = installer.install(self.home, executable="codex", output=lambda message: None)
        entries = json.loads(catalog.read_text(encoding="utf-8"))["plugins"]
        self.assertEqual(entries[0], other)
        self.assertEqual(entries[1]["name"], "study")
        self.assertEqual(result["plugin"], "study@my-personal")
        self.assertEqual(calls, [("plugin", "marketplace", "list", "--json"),
                                 ("plugin", "add", "study@my-personal", "--json")])

    def test_installation_copy_preserves_notes_and_omits_python_caches(self):
        target = self.home / "learn-plugin"
        target.mkdir()
        (target / ".codex-plugin").mkdir()
        (target / ".codex-plugin/plugin.json").write_text('{"name":"codex-learn"}', encoding="utf-8")
        note = target / "personal.md"
        note.write_text("Handwritten notes.", encoding="utf-8")
        installer.copy_package(ROOT, target, self.home)
        self.assertEqual(note.read_text(encoding="utf-8"), "Handwritten notes.")
        self.assertFalse((target / "scripts/__pycache__").exists())
        installer.validate_package(target)

    def test_installation_refuses_to_overwrite_an_unrelated_project(self):
        target = self.home / "other-project"
        target.mkdir()
        note = target / "personal.md"
        note.write_text("Keep this.", encoding="utf-8")
        with self.assertRaises(installer.StudyError):
            installer.copy_package(ROOT, target, self.home)
        self.assertEqual(note.read_text(encoding="utf-8"), "Keep this.")


if __name__ == "__main__":
    unittest.main()
