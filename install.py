"""Install Codex Learn with the native CLI and optional, explicit hook approval."""
import argparse
from collections import deque
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / "scripts"))
from learn import LearnError
from register import register

EVENTS = {"sessionStart", "sessionEnd", "userPromptSubmit", "stop"}
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


class AppServer:
    """Use local native RPC only; no thread or model turn is created."""
    def __init__(self, executable, inspect_hooks=False):
        command = [executable]
        if inspect_hooks:
            command.extend(["-c", "features.hooks=true"])
        command.extend(["app-server", "--stdio"])
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", creationflags=FLAGS)
        self.messages = queue.Queue()
        self.errors = deque(maxlen=12)
        self.counter = 0
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._errors, daemon=True)]
        for reader in self.readers:
            reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "codex-learn-installer", "version": "1.0"},
                                    "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.messages.put(None)

    def _errors(self):
        for line in self.process.stderr:
            self.errors.append(line.rstrip())

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message is None:
                raise LearnError("Codex app-server exited during installation.")
            if message.get("id") == request_id:
                if "error" in message:
                    raise LearnError(f"Codex {method}: {message['error'].get('message', message['error'])}")
                return message["result"]
        raise LearnError(f"Codex {method} did not respond within 20 seconds.")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for reader in self.readers:
            reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def plugin_hooks(server, home, plugin_id, installed):
    result = server.call("hooks/list", {"cwds": [str(home)]})
    entries = result["data"]
    hooks = [hook for entry in entries for hook in entry["hooks"] if hook.get("pluginId") == plugin_id]
    if len(hooks) != 4 or {hook["eventName"] for hook in hooks} != EVENTS:
        details = [error["message"] for entry in entries for error in entry["errors"]]
        raise LearnError("Codex did not load all four Learn hooks. " + " ".join(details))
    for hook in hooks:
        if (hook.get("source") != "plugin" or hook.get("handlerType") != "command"
                or not Path(hook["sourcePath"]).resolve().is_relative_to(installed)):
            raise LearnError("Unexpected hook source; refusing to approve it.")
    return hooks


def approve_hooks(executable, home, plugin_id, installed, prompt, output):
    with ExitStack() as stack:
        server = stack.enter_context(AppServer(executable))
        config = server.call("config/read", {"cwd": str(home), "includeLayers": False})["config"]
        disabled_feature = (config.get("features") or {}).get("hooks") is False
        if disabled_feature:
            # List native definitions without changing persistent settings or starting a thread.
            server = stack.enter_context(AppServer(executable, inspect_hooks=True))
        hooks = plugin_hooks(server, home, plugin_id, installed)
        if all(hook["trustStatus"] in {"trusted", "managed"} and hook["enabled"] for hook in hooks) and not disabled_feature:
            return True
        output("\nReview Codex Learn hooks:")
        for hook in hooks:
            output(f"\n{hook['eventName']} | {hook['trustStatus']} | timeout {hook['timeoutSec']}s\n"
                   f"  Source: {hook['sourcePath']}\n  Command: {hook['command']}\n"
                   f"  Matcher: {hook.get('matcher') or '(all)'}\n  Hash: {hook['currentHash']}")
        if disabled_feature:
            output("Codex hooks are disabled. Approval will also enable the hooks feature.")
        if prompt("Trust and enable these four Codex Learn hooks? [y/N] ").strip().lower() not in {"y", "yes"}:
            return False
        current = plugin_hooks(server, home, plugin_id, installed)
        fingerprints = lambda values: {(hook["key"], hook["currentHash"]) for hook in values}
        if fingerprints(current) != fingerprints(hooks):
            raise LearnError("The hooks changed during review. Run the installer again.")
        edits = [{"keyPath": "hooks.state", "mergeStrategy": "upsert", "value": {
            hook["key"]: {"trusted_hash": hook["currentHash"], "enabled": True}
            for hook in current if not hook.get("isManaged")}}]
        if disabled_feature:
            edits.append({"keyPath": "features.hooks", "mergeStrategy": "replace", "value": True})
        server.call("config/batchWrite", {"edits": edits, "reloadUserConfig": True})
    # Verify using a fresh native loader, rather than assuming a successful write means readiness.
    with AppServer(executable) as server:
        verified = plugin_hooks(server, home, plugin_id, installed)
        config = server.call("config/read", {"cwd": str(home), "includeLayers": False})["config"]
        if (not all(hook["enabled"] and hook["trustStatus"] in {"trusted", "managed"} for hook in verified)
                or (config.get("features") or {}).get("hooks") is False):
            raise LearnError("Codex did not activate the approved hooks; check /hooks for enforced settings.")
    return True


def install(home, package=PACKAGE, executable=None, review=True, prompt=input, output=print):
    home, package = Path(home).resolve(), Path(package).resolve()
    validate_package(package)
    executable = codex_executable(executable)
    cli(executable, "plugin", "add", "--help")
    target = destination(home)
    copy_package(package, target, home)
    registration = register(home, target)
    name = registration["name"]
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
    ready = approve_hooks(executable, home, plugin_id, installed, prompt, output) if review else False
    return {"plugin": plugin_id, "source": str(target), "installed": str(installed), "hooks_ready": ready,
            "next": "Restart Codex, then invoke $codex-learn:learn." if ready
                    else "Installation complete; hook approval is pending. Run this installer again to review."}


def ask(message):
    print(message, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Codex CLI executable if it is not on PATH")
    parser.add_argument("--no-review", action="store_true", help="Install without recording hook approval")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON; review is shown on stderr")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 10):
            raise LearnError("Python 3.10 or newer is required.")
        result = install(Path.home(), executable=args.codex, review=not args.no_review,
                         prompt=ask, output=lambda value: print(value, file=sys.stderr))
        print(json.dumps(result, indent=2) if args.json else result["next"])
    except (LearnError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
