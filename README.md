# Codex Learn

A local Codex plugin with two skills and native command hooks. Each conversation has its own Markdown file in its project's `learn/` folder.

## Install

Copy or extract this whole folder on the destination computer. The installer finds Codex, copies the runtime files to a persistent folder in your home, preserves your personal marketplace's other entries, and installs the plugin with Codex's native CLI. If Learn is already registered locally, it updates that source location rather than creating another entry.

Windows, from this folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

macOS/Linux:

```sh
python3 install.py
```

Python 3.10+ and a local Codex CLI with plugin and hooks support are required. On Windows the installer also recognizes Python bundled with Codex. Set `CODEX_LEARN_PYTHON` to an explicit Python executable if needed. Use `--codex <executable>` if Codex is not found automatically. Installation targets the current user's home.

The installer displays the four hook commands, sources, timeouts, and native definition hashes. Answer `y` to approve those exact hooks. It records that approval through Codex's configuration API and verifies readiness with a fresh native loader. If hooks are disabled, the review explains that approval will also enable the hooks feature. Other hooks' trust and settings are preserved. It never starts a conversation or enables learning notes during installation.

Declining approval installs the plugin and leaves hook review pending. Run the installer again to finish. Use `--no-review` to deliberately install without approving hooks, or `--json` for the final structured result. The approval API uses the local experimental Codex app-server protocol; unsupported clients produce an installation error instead of claiming the hooks work.

Restart or resume Codex after installation.

## Enable

Invoke `$codex-learn:learn` in the conversation you want to save. To stop syncing, invoke `$codex-learn:learn off`; to check it, invoke `$codex-learn:learn status`. The plugin contains skills; it does not register `/learn` or `/condense` CLI commands.

If you prefer Codex's own review UI, open `/hooks` to review SessionStart, SessionEnd, UserPromptSubmit, and Stop.

In the verified CLI 0.160.0 hook browser, choose an event with the arrow keys and press Enter. Check that Source is `Plugin - codex-learn@personal`; the command runs this plugin's `scripts/run.ps1 hook`. Press `t` to trust that hook and Esc to return. Repeat for all four events. The overview also has `t` to trust all hooks awaiting review; individual review avoids including other plugins.

The package uses `.codex-plugin/plugin.json`, which references `hooks/hooks.json`. Native loader checks with Codex CLI 0.160.0 found that a root portable `plugin.json` loaded the skills but omitted the hooks, even with a compatibility manifest present. The original portable manifest is retained as `plugin.portable.json.bak`; it is not an active manifest. SessionEnd uses the supported three-second timeout.

The initial activation includes previous questions and answers in this conversation. Subsequent activations resume without importing messages sent while logging was disabled.

- Quote a passage using Markdown `>` and ask about it. The answer appears below the matching passage's complete Markdown block.
- Quote that explanation to ask another question. It is nested beneath the explanation.
- Invoke `$codex-learn:condense` to integrate the latest explored topic into a coherent explanation. Include a quote or specify a branch to select another topic.

This plugin requires local command-hook execution; cloud orchestration cannot run its watcher.

## Files

`learn/session-<session-id>-<hash>.md` contains readable notes. Nested blockquotes hold follow-ups. `learn/.sessions/<session-id>-<hash>/conversation.md` keeps the original accepted user and assistant messages, including commentary. `state.json` retains the question tree and earlier condensed versions. Condensation also creates Markdown backups there.

The readable note shows completed final answers, or commentary while an answer is still in progress. It does not export internal reasoning, developer/system instructions, or tool payloads. Image-only content is not exported. Copy a selected passage as a Markdown blockquote for deterministic quotation recognition; undocumented client selection metadata is not assumed. Ambiguous quotations are supplied to Codex for source selection. No match is guessed. An unquoted follow-up can be attached by Codex using conversation context.

Handwritten text outside the `codex-learn:begin/end` markers is preserved. Editing the generated section pauses sync with an error instead of overwriting the edit. Keep handwritten additions outside that section. Archive/state files are generated files.

## Efficiency

The watcher polls file metadata every 500 ms and reads only new complete JSONL records. It never parses token streams or calls a model. It renders only when questions, answers, or their structure change, and does not rewrite unchanged notes, archives, or state. Searchable answer text is normalized once and cached for quotation matching; that cache is omitted from model snapshots. Its five-second heartbeat is a tiny separate file. Idle watchers stop after two hours; the next trusted prompt/session hook restarts them. SessionEnd and `/learn off` stop them sooner.

Only prompt, final-answer, and session lifecycle hooks run; there is no hook process for every tool call. Writers share a per-session file lock and use atomic replacement. JSON state and Markdown rendering still scale with total saved note size when content changes; this is intended for learning conversations rather than unlimited log retention. `$codex-learn:condense` runs in the current Codex turn, without a separate API client or background model calls.

## Validate

Run `python -m unittest discover -s tests -v`. Tests cover realistic Codex response-message records, quote nesting, fences/math, partial JSONL writes, duplicate prevention, ambiguous quotes, original retention, stale condensation, external edits, and a real watcher subprocess. They validate synchronization and the condensation transaction; the quality of model-written condensed prose still depends on Codex following the skill.

Run `python scripts/benchmark.py` to measure a 1,000-question synthetic transcript, one quoted update, and 20 idle syncs. It checks that only the appended bytes are consumed and idle syncs produce zero file writes. Timings vary by machine.

Codex's JSONL transcript format is not a stable public API. This parser supports the inspected `response_item/message` format and an older `event_msg/user_message` / `agent_message` form. It fails visibly on malformed records or changed session binding.

References: [Codex hooks](https://learn.chatgpt.com/docs/hooks), [plugin packaging](https://developers.openai.com/plugins/build/plugins), [skill invocation](https://learn.chatgpt.com/docs/reference/slash-commands).
