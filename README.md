# Codex Learn

Learning notes in ordinary Markdown. The note is the source of truth.

## Design

The native prompt hook remembers the current question. The native Stop hook writes its final answer to the selected note. One small `learn/.sessions/active.json` file holds active note bindings, pending questions, and pause flags. A file lock serializes hook writes.

There is no conversation archive, question database, answer-version history, background watcher, or automatic note regeneration. No model or embedding service runs in the plugin. Condensation and cleanup are direct Markdown edits.

## Use

- `$codex-learn:learn <topic or material>` enables notes for this conversation.
- `$codex-learn:learn continue @"learn/Poppy.md"` selects an existing note. The file alone is sufficient.
- `$codex-learn:learn off` stops saving.
- `$codex-learn:learn status` reports the active note.
- `$codex-learn:learn pause` excludes the next question and answer.
- `$codex-learn:learn pause <question>` excludes that question only.
- `$codex-learn:learn cleanup` removes clearly unrelated sections from the active note.
- `$codex-learn:condense` integrates the selected topic and its follow-ups without shortening the teaching.

The plugin mention `@Codex-Learn <topic or material>` also enables notes. For manual activation during a turn, pass the current question or material with `enable --question <question or material>` so the first answer is saved. Manual `continue` accepts the same argument when a question accompanies the selected note.

Saving starts at activation and happens when the answer finishes. Earlier messages are not imported. Only questions and final answers are saved; progress updates and tool messages are omitted. Ordinary file edits are allowed. Continuing a note disables other active bindings to that file and can switch the current conversation to a different note.

Follow-ups can be placed beneath an exact passage from the current note, including nested blockquotes. The assistant sets the passage with the script's `place --anchor <exact passage>` command. The hook saves the answer after the complete Markdown block. There are no question IDs or historical source searches.

Existing Markdown notes remain usable, including notes produced by earlier versions. Old session archives are not read or required.

## Install

Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1` on Windows, or `python3 install.py` on macOS/Linux. Python 3.10+ and a local Codex CLI with native plugin/hooks support are required. The installer copies and registers the plugin using the native CLI. Review and trust Learn's hooks through Codex's native hooks UI, then restart or resume Codex.

On Windows, the launcher can use Python bundled with Codex. `CODEX_LEARN_PYTHON` selects an explicit interpreter; `--codex <executable>` selects an explicit CLI during installation.

## Verification

Run `python -m unittest discover -s tests`. The tests cover saving, exclusions, direct note continuation, manual edits, nested placement, and installation. They do not launch a watcher or a real Codex conversation.
