"""Install Codex Learn with the native CLI; review hooks in Codex."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / "scripts"))
from learn import LearnError
from register import register

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def codex_executable(explicit=None):
    candidate = explicit or shutil.which("codex.exe") or shutil.which("codex")
    if candidate:
        return str(candidate)
    if os.name == "nt":
        home = Path.home()
        for path in (home / "AppData/Local/Programs/OpenAI/Codex/bin/codex.exe",
                     home / ".codex/packages/standalone/current/bin/codex.exe"):
            if path.is_file():
                return str(path)
    raise LearnError("Codex CLI was not found. Install it or pass --codex <executable>.")


def cli(executable, *args, as_json=False):
    result = subprocess.run([executable, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=45, creationflags=FLAGS)
    if result.returncode:
        raise LearnError(f"codex {' '.join(args)} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout) if as_json else result.stdout


def validate_package(package):
    manifest = package / ".codex-plugin/plugin.json"
    if not manifest.is_file():
        raise LearnError("The package needs .codex-plugin/plugin.json.")
    metadata = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if metadata.get("name") != "codex-learn" or metadata.get("hooks") != "./hooks/hooks.json":
        raise LearnError("This is not the supported Codex Learn package.")
    for relative in ("scripts/learn.py", "scripts/run.ps1", "hooks/hooks.json",
                     "skills/learn/SKILL.md", "skills/condense/SKILL.md", "install.py", "install.ps1", "README.md"):
        if not (package / relative).is_file():
            raise LearnError(f"Incomplete plugin package: missing {relative}.")


def destination(home):
    catalog = home / ".agents/plugins/marketplace.json"
    if catalog.exists():
        data = json.loads(catalog.read_text(encoding="utf-8-sig"))
        if (not isinstance(data, dict) or not isinstance(data.get("name"), str)
                or not isinstance(data.get("plugins"), list)
                or not all(isinstance(entry, dict) for entry in data["plugins"])):
            raise LearnError("The existing personal marketplace is invalid; leaving it unchanged.")
        matches = [entry for entry in data.get("plugins", []) if entry.get("name") == "codex-learn"]
        if len(matches) > 1:
            raise LearnError("The personal marketplace has duplicate codex-learn entries.")
        if matches:
            source = matches[0].get("source", {})
            if source.get("source") != "local" or not source.get("path", "").startswith("./"):
                raise LearnError("An existing codex-learn entry uses another source; leaving it unchanged.")
            target = (home / source["path"]).resolve()
            if not target.is_relative_to(home) or target == home:
                raise LearnError("The existing plugin source must stay inside your home directory.")
            return target
    return (home / ".codex/plugins/sources/codex-learn").resolve()


def copy_package(package, target, home):
    if target == home or not target.is_relative_to(home):
        raise LearnError("The installation target must stay inside your home directory.")
    if target.exists() and any(target.iterdir()):
        manifests = (target / ".codex-plugin/plugin.json", target / "plugin.json")
        existing = next((path for path in manifests if path.is_file()), None)
        if not existing or json.loads(existing.read_text(encoding="utf-8-sig")).get("name") != "codex-learn":
            raise LearnError("The installation target contains another project; leaving it unchanged.")
    files = [package / name for name in ("install.py", "install.ps1", "README.md")]
    for name in (".codex-plugin", "hooks", "scripts", "skills"):
        files.extend(path for path in (package / name).rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".bak"})
    for source in files:
        if not source.resolve().is_relative_to(package):
            raise LearnError("A package file points outside the plugin directory.")
        output = target / source.relative_to(package)
        if not output.resolve().is_relative_to(target):
            raise LearnError("An installation file points outside the plugin directory.")
        content = source.read_bytes()
        if output.is_file() and output.read_bytes() == content:
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".install-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, output)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    # CLI 0.160.0 ignores plugin hooks when the portable root manifest is present.
    portable = target / "plugin.json"
    if portable.exists():
        portable.replace(target / f"plugin.portable-{time.time_ns()}.json.bak")

def install(home, package=PACKAGE, executable=None, output=print):
    home, package = Path(home).resolve(), Path(package).resolve()
    validate_package(package)
    executable = codex_executable(executable)
    target = destination(home)
    copy_package(package, target, home)
    name = register(home, target)["name"]
    marketplaces = cli(executable, "plugin", "marketplace", "list", "--json", as_json=True)["marketplaces"]
    matching = [market for market in marketplaces if market["name"] == name]
    if matching and not any(Path(market["root"]).resolve() == home for market in matching):
        raise LearnError(f"A different marketplace already uses the name {name}; it was not replaced.")
    if not matching:
        cli(executable, "plugin", "marketplace", "add", str(home), "--json", as_json=True)
    plugin_id = f"codex-learn@{name}"
    result = cli(executable, "plugin", "add", plugin_id, "--json", as_json=True)
    installed = Path(result["installedPath"]).resolve()
    output(f"Installed {plugin_id}\nSource: {target}\nInstalled copy: {installed}")
    return {"plugin": plugin_id, "source": str(target), "installed": str(installed),
            "next": "Review Learn hooks in Codex's native hooks UI, then restart or resume Codex."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Codex CLI executable if it is not on PATH")
    parser.add_argument("--json", action="store_true", help="Print the installation result as JSON")
    args = parser.parse_args()
    try:
        result = install(Path.home(), executable=args.codex,
                         output=lambda value: print(value, file=sys.stderr))
        print(json.dumps(result, indent=2) if args.json else result["next"])
    except (LearnError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
