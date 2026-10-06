---
name: learn
description: Enable or manage live Markdown learning notes for this conversation. Use when the user invokes /learn or $learn, asks to start learning notes, stop learning notes, or check their learning-note status.
---

# Learn

Enable this conversation's learning log. Save notes in `learn/` under the project directory. Do not enable other conversations.

Use the session ID, project path, and transcript path supplied by the Codex Learn hook. If unavailable, use `CODEX_THREAD_ID` for the session ID and the current project directory. Never choose the newest transcript or a different chat. If no session ID is available, report that the plugin hooks need to be enabled and trusted before activation.

The executable is `../../scripts/learn.py` relative to this skill. On Windows use `../../scripts/run.ps1` through PowerShell; on other platforms use Python 3. Resolve an absolute path from this skill's actual location. Pass paths as separate, correctly quoted arguments.

1. For `/learn` or `$learn`, run `enable --project <project> --session <session> --transcript <transcript>`. The first activation includes earlier questions and answers so previous answers can be quoted. It starts a hidden watcher that saves completed transcript messages as they arrive. If the hook already activated learning, running enable is idempotent.
2. For `/learn off`, run `disable --project <project> --session <session>`.
3. For `/learn status`, run `status --project <project> --session <session>`.
4. Read the JSON result. Confirm the actual note path and enabled/disabled status briefly. Do not claim hooks or slash-menu discovery were verified just because a script succeeded.

While enabled, hooks and the watcher do the saving; do not manually append answers to Markdown. Quoted follow-ups are attached beneath the matching answer block. Descendants remain nested, and source messages are retained in `learn/.sessions/<session>/conversation.md` and `state.json`.

For every later question, use the hook's node and candidate information. If the user's quotation is ambiguous, identify its source using conversation context, run `snapshot`, and then `attach --plan-file <JSON file>`. A plan contains `node`, `parent`, and `anchor` (a passage from that parent's current answer). If an unquoted question clearly follows up on an existing answer, attach it the same way. Never invent a source match. If multiple sources remain indistinguishable, ask one concise clarification.

After condensation, an old quotation may refer to an archived answer. Attach it to the visible condensed ancestor using an equivalent passage from the current note as the anchor. Read the current snapshot before doing this. User-visible answers remain normal chat answers.
