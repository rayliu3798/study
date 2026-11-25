"""Register this plugin in a personal marketplace, preserving existing entries."""
import argparse
import json
from pathlib import Path
import time
from study import atomic, dump, StudyError


def register(home, plugin):
    home, plugin = Path(home).resolve(), Path(plugin).resolve()
    manifests = (plugin / ".codex-plugin" / "plugin.json", plugin / "plugin.json")
    if not plugin.is_relative_to(home) or not any(path.is_file() for path in manifests):
        raise StudyError("Plugin must exist inside the specified personal marketplace root.")
    path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {
        "name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    entry = {"name": "study", "source": {"source": "local", "path": "./" + plugin.relative_to(home).as_posix()},
             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}
    if not isinstance(marketplace.get("name"), str) or not isinstance(marketplace.get("plugins"), list):
        raise StudyError("Invalid personal marketplace: name and plugins are required.")
    matches = [e for e in marketplace["plugins"] if e.get("name") in ("study", "codex-learn")]
    if len(matches) > 1 or (matches and matches[0].get("source") != entry["source"]):
        raise StudyError("A different study entry already exists; refusing to replace it.")
    if not matches or matches[0]["name"] != "study":
        if path.exists():
            atomic(path.with_name("marketplace.backup-" + str(time.time_ns()) + ".json"), path.read_text(encoding="utf-8-sig"))
        if matches:
            matches[0]["name"] = "study"
        else:
            marketplace["plugins"].append(entry)
        atomic(path, dump(marketplace))
    return {"marketplace": str(path), "name": marketplace["name"], "plugin": str(plugin),
            "next": f"Run codex plugin add study@{marketplace['name']}, then review its hooks."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, help="Actual user's home, not a sandbox account")
    args = parser.parse_args()
    print(dump(register(args.home, Path(__file__).resolve().parents[1])))
