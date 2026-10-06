import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("learn", ROOT / "scripts" / "learn.py")
learn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(learn)
sys.modules.setdefault("learn", learn)


class LearningTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "tests" / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.project = Path(self.temp.name).resolve()
        self.assertTrue(self.project.is_relative_to(temp_root.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.store = learn.Store(self.project, "session-one")
        self.transcript = self.project / "transcript.jsonl"
        self.transcript.touch()
        self.append("session_meta", {"id": "session-one"})

    def append(self, kind, payload):
        with self.transcript.open("ab") as stream:
            stream.write((json.dumps({"type": kind, "payload": payload}, ensure_ascii=False) + "\n").encode("utf-8"))

    def turn(self, turn_id, question, answer=None):
        self.append("event_msg", {"type": "task_started", "turn_id": turn_id})
        self.message("user", question)
        if answer:
            self.message("assistant", answer)

    def message(self, role, text, phase="final_answer", item_id=None):
        self.append("response_item", {"type": "message", "role": role,
            "phase": phase, "id": item_id,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]})

    def enable(self):
        learn.enable(self.store, self.transcript)

    def sync(self):
        with self.store.locked() as state:
            learn.sync(state)
            self.store.commit(state)
        return state

    def state(self):
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def test_backfill_root_inline_and_nested(self):
        self.turn("t1", "Explain caching.", "A cache stores results.\n\nEviction controls its size.")
        self.turn("t2", "> A cache stores results.\n\nWhich results?", "Previously computed values.\n\nKeys identify each value.")
        self.turn("t3", "> Keys identify each value.\n\nWhat is a key?", "An identifier for a cached value.")
        self.enable()
        state = self.state()
        self.assertEqual(state["nodes"]["q0002"]["parent"], "q0001")
        self.assertEqual(state["nodes"]["q0003"]["parent"], "q0002")
        text = self.store.note.read_text(encoding="utf-8")
        self.assertLess(text.index("Follow-up q0002"), text.index("Eviction controls"))
        self.assertIn("> > **Follow-up q0003**", text)

    def test_fence_and_math_anchor_after_whole_block(self):
        answer = "```python\nx = 1\n\nx += 2\n```\n\n$$\na = b + c\n\nd = e\n$$\n\nLater paragraph."
        self.turn("t1", "Explain this.", answer)
        self.turn("t2", "> x = 1\n\nExplain that line.", "It initializes x.")
        self.turn("t3", "> a = b + c\n\nExplain the equation.", "It defines a as the sum of b and c.")
        self.enable()
        text = self.store.note.read_text(encoding="utf-8")
        self.assertLess(text.index("x += 2\n```"), text.index("Follow-up q0002"))
        self.assertLess(text.index("d = e\n$$"), text.index("Follow-up q0003"))
        self.assertLess(text.index("Follow-up q0003"), text.index("Later paragraph."))

    def test_ambiguous_quote_not_arbitrarily_attached(self):
        self.turn("t1", "First topic", "Shared passage here.")
        self.turn("t2", "Second topic", "Shared passage here.")
        self.turn("t3", "> Shared passage here.\n\nExplain.", "More detail.")
        self.enable()
        state = self.state()
        self.assertIsNone(state["nodes"]["q0003"]["parent"])
        self.assertEqual(len(state["nodes"]["q0003"]["candidates"]), 2)
        with self.store.locked() as state:
            learn.attach(state, {"node": "q0003", "parent": "q0001", "anchor": "Shared passage here."})
            self.store.commit(state)
        self.assertEqual(self.state()["nodes"]["q0003"]["parent"], "q0001")

    def test_unicode_markdown_matching(self):
        self.turn("t1", "解释", "The **cache** stores ‘结果’ for later use.")
        self.turn("t2", "> The cache stores '结果' for later use.\n\n为什么？", "为了复用。")
        self.enable()
        self.assertEqual(self.state()["nodes"]["q0002"]["parent"], "q0001")
        self.assertIn("为了复用", self.store.note.read_text(encoding="utf-8"))

    def test_partial_line_and_restart_are_idempotent(self):
        self.turn("t1", "Question", "Answer")
        self.enable()
        record = json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Second question"}]}}).encode()
        with self.transcript.open("ab") as stream:
            stream.write(record[:35])
        before = self.state()["cursor"]
        self.sync()
        self.assertEqual(before, self.state()["cursor"])
        with self.transcript.open("ab") as stream:
            stream.write(record[35:] + b"\n")
        self.sync()
        self.store = learn.Store(self.project, "session-one")
        self.sync()
        self.assertEqual(len(self.state()["nodes"]), 2)

    def test_idle_sync_does_not_rewrite(self):
        self.turn("t1", "Question", "Answer")
        self.enable()
        paths = [self.store.note, self.store.path, self.store.directory / "conversation.md"]
        mtimes = [p.stat().st_mtime_ns for p in paths]
        for _ in range(10):
            self.sync()
        self.assertEqual(mtimes, [p.stat().st_mtime_ns for p in paths])

    def test_control_turns_and_private_items_omitted(self):
        self.turn("t1", "Question", "Answer")
        self.message("developer", "PRIVATE DEVELOPER")
        self.append("response_item", {"type": "reasoning", "text": "PRIVATE REASONING"})
        self.append("response_item", {"type": "function_call_output", "output": "PRIVATE TOOL"})
        self.message("user", "<environment_context>INJECTED</environment_context>")
        self.turn("t2", "/learn", "Learning enabled.")
        self.turn("t3", "/condense", "Condensed.")
        self.enable()
        self.assertEqual(len(self.state()["nodes"]), 1)
        text = self.store.note.read_text(encoding="utf-8")
        for excluded in ("PRIVATE", "INJECTED", "Learning enabled.", "Condensed."):
            self.assertNotIn(excluded, text)

    def test_hook_before_and_after_transcript_prompt(self):
        self.enable()
        payload = {"session_id": "session-one", "cwd": str(self.project), "transcript_path": str(self.transcript),
                   "hook_event_name": "UserPromptSubmit", "turn_id": "t1", "prompt": "Question"}
        with patch.dict(os.environ, {"CODEX_LEARN_NO_WATCH": "1"}):
            result = learn.hook(payload)
            self.turn("t1", "Question", "Answer")
            self.sync()
            learn.hook(payload)
        self.assertEqual(len(self.state()["nodes"]), 1)
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_stop_fallback_then_flushed_final_is_not_duplicated(self):
        self.turn("t1", "Question")
        self.enable()
        with patch.dict(os.environ, {"CODEX_LEARN_NO_WATCH": "1"}):
            result = learn.hook({"session_id": "session-one", "cwd": str(self.project), "transcript_path": str(self.transcript),
                "hook_event_name": "Stop", "turn_id": "t1", "last_assistant_message": "Final answer"})
        self.assertEqual(result, {})
        self.message("assistant", "Final answer", item_id="m1")
        state = self.sync()
        self.assertEqual(len(state["nodes"]["q0001"]["messages"]), 1)
        self.assertEqual(state["nodes"]["q0001"]["messages"][0]["id"], "m1")

    def test_condense_preserves_history_unanswered_and_future_quotes(self):
        self.turn("t1", "Topic", "Original passage about caching.")
        self.turn("t2", "> Original passage about caching.\n\nWhy?", "An archived detail about reuse.")
        self.turn("t3", "> An archived detail about reuse.\n\nStill unresolved?")
        self.enable()
        with self.store.locked() as state:
            learn.condense(state, "q0001", state["revision"], "## Caching\n\nA cache reuses computed values.")
            self.store.commit(state, backup=True)
        state = self.state()
        self.assertEqual(state["nodes"]["q0002"]["folded_into"], "q0001")
        self.assertIsNone(state["nodes"]["q0003"]["folded_into"])
        self.assertEqual(state["nodes"]["q0003"]["parent"], "q0001")
        text = self.store.note.read_text(encoding="utf-8")
        self.assertNotIn("Follow-up q0002", text)
        self.assertIn("Still unresolved?", text)
        archive = (self.store.directory / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("An archived detail about reuse.", archive)
        self.assertEqual(len(list((self.store.directory / "backups").glob("*.md"))), 1)
        self.turn("t4", "> An archived detail about reuse.\n\nOne more question.", "A further answer.")
        state = self.sync()
        self.assertEqual(state["nodes"]["q0004"]["parent"], "q0001")

    def test_stale_condense_and_attachment_cycle_rejected(self):
        self.turn("t1", "Topic", "A passage.")
        self.turn("t2", "> A passage.\n\nWhy?", "Details.")
        self.enable()
        with self.store.locked() as state:
            with self.assertRaisesRegex(learn.LearnError, "Stale"):
                learn.condense(state, "q0001", -1, "Condensed")
            with self.assertRaisesRegex(learn.LearnError, "cycle"):
                learn.attach(state, {"node": "q0001", "parent": "q0002", "anchor": "Details."})

    def test_manual_notes_preserved_and_managed_edits_refused(self):
        self.turn("t1", "Topic", "Answer.")
        self.enable()
        with self.store.note.open("a", encoding="utf-8") as stream:
            stream.write("\nHandwritten note.\n")
        self.turn("t2", "Another question", "Another answer")
        self.sync()
        self.assertIn("Handwritten note.", self.store.note.read_text(encoding="utf-8"))
        text = self.store.note.read_text(encoding="utf-8").replace("Answer.", "Manual replacement.")
        self.store.note.write_text(text, encoding="utf-8")
        before = self.state()["cursor"]
        self.turn("t3", "Third question", "Third answer")
        with self.assertRaisesRegex(learn.LearnError, "externally"):
            self.sync()
        self.assertEqual(self.state()["cursor"], before)
        self.assertIn("Manual replacement.", self.store.note.read_text(encoding="utf-8"))

    def test_disabled_gap_not_backfilled_on_resume(self):
        self.turn("t1", "Included", "Included answer")
        self.enable()
        with self.store.locked() as state:
            state.update(enabled=False, watcher=None, current=None)
            self.store.commit(state)
        self.turn("t2", "Excluded", "Excluded answer")
        self.enable()
        self.turn("t3", "Included again", "Another included answer")
        self.sync()
        self.assertEqual(len(self.state()["nodes"]), 2)
        self.assertNotIn("Excluded", self.store.note.read_text(encoding="utf-8"))

    def test_wrong_session_and_truncation_refused(self):
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "other-session"}}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(learn.LearnError, "different session"):
            self.enable()
        self.transcript.write_text("", encoding="utf-8")
        self.turn("t1", "Topic", "Answer")
        self.enable()
        self.transcript.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(learn.LearnError, "truncated"):
            self.sync()

    def test_dual_event_and_response_format_not_duplicated(self):
        self.append("event_msg", {"type": "task_started", "turn_id": "t1"})
        self.append("event_msg", {"type": "user_message", "message": "Question"})
        self.message("user", "Question")
        self.append("event_msg", {"type": "agent_message", "message": "Answer"})
        self.message("assistant", "Answer")
        self.enable()
        self.assertEqual(len(self.state()["nodes"]), 1)
        self.assertEqual(len(self.state()["nodes"]["q0001"]["messages"]), 1)

    def test_actual_background_watcher_syncs_and_stops(self):
        self.enable()
        token = "test-token"
        with self.store.locked() as state:
            state["watcher"] = {"token": token, "heartbeat": time.time(), "pid": None}
            self.store.commit(state)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "learn.py"), "watch",
            "--project", str(self.project), "--session", "session-one", "--token", token], creationflags=flags)
        try:
            self.turn("t1", "Live question", "Live answer")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with self.store.locked() as state:
                    flushed = state["cursor"] == self.transcript.stat().st_size
                if flushed and "Live answer" in self.store.note.read_text(encoding="utf-8"):
                    break
                time.sleep(0.1)
            self.assertIn("Live answer", self.store.note.read_text(encoding="utf-8"))
            with self.store.locked():
                mtimes = [self.store.note.stat().st_mtime_ns, self.store.path.stat().st_mtime_ns]
            time.sleep(0.7)
            self.assertEqual(mtimes, [self.store.note.stat().st_mtime_ns, self.store.path.stat().st_mtime_ns])
            with self.store.locked() as state:
                state.update(enabled=False, watcher=None)
                self.store.commit(state)
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_two_cli_writers_serialize_without_duplicate_messages(self):
        self.turn("t1", "Topic", "Answer")
        self.enable()
        self.turn("t2", "Next topic", "Next answer")
        command = [sys.executable, str(ROOT / "scripts" / "learn.py"), "sync", "--project", str(self.project), "--session", "session-one"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        writers = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags) for _ in range(2)]
        for writer in writers:
            _, errors = writer.communicate(timeout=10)
            self.assertEqual(writer.returncode, 0, errors.decode())
        state = self.state()
        self.assertEqual(len(state["nodes"]), 2)
        self.assertEqual(len(state["nodes"]["q0002"]["messages"]), 1)

    @unittest.skipUnless(os.name == "nt", "Windows launcher")
    def test_windows_hook_launcher_accepts_stdin_and_returns_json(self):
        environment = dict(os.environ, CODEX_LEARN_PYTHON=sys.executable, CODEX_LEARN_NO_WATCH="1")
        payload = {"session_id": "session-one", "cwd": str(self.project), "transcript_path": str(self.transcript),
                   "hook_event_name": "UserPromptSubmit", "turn_id": "t1", "prompt": "/learn"}
        command = [str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                   "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "run.ps1"), "hook"]
        result = subprocess.run(command, input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertTrue(self.state()["enabled"])

    def test_quote_index_reused_and_omitted_from_model_snapshot(self):
        self.turn("t1", "Topic", "A distinctive passage.")
        self.enable()
        with self.store.locked() as state:
            with patch.object(learn, "normalized", wraps=learn.normalized) as normalize:
                match, _ = learn.find_parent(state, ["A distinctive passage."])
                self.assertEqual(match["parent"], "q0001")
                self.assertEqual(normalize.call_count, 1)
            self.assertNotIn("search_sources", learn.snapshot(state)["nodes"]["q0001"])

    def test_lookup_returns_complete_nested_context_without_other_topics(self):
        long_answer = "Detail without truncation. " * 120
        self.turn("t1", "Explain caching.", "A cache stores results.\n\nEviction limits its size.")
        self.turn("t2", "> A cache stores results.\n\nWhich results?", long_answer)
        self.turn("t3", "> Detail without truncation.\n\nAny qualification?", "Only results whose inputs still match.")
        self.turn("t4", "> Eviction limits its size.\n\nCorrect that assumption.", "Some caches have no configured size limit.")
        self.turn("t5", "Unrelated topic", "UNRELATED ANSWER MUST NOT BE SENT")
        self.enable()
        result = learn.lookup(self.state(), "q0002")
        self.assertEqual(set(result["nodes"]), {"q0001", "q0002", "q0003"})
        self.assertEqual(result["nodes"]["q0002"]["answer"], long_answer)
        self.assertEqual(result["nodes"]["q0003"]["answer"], "Only results whose inputs still match.")
        self.assertEqual(result["related"][0]["node"], "q0004")
        self.assertNotIn("UNRELATED ANSWER", json.dumps(result))
        self.assertNotIn("original_answer", result["nodes"]["q0002"])

    def test_lookup_ambiguous_quotes_paginate_without_selecting_a_parent(self):
        for number in range(6):
            self.turn("t" + str(number), "Topic " + str(number), "Shared passage here.\n\nContext " + str(number))
        self.turn("follow", "> Shared passage here.\n\nExplain.")
        self.enable()
        state = self.state()
        first = learn.lookup(state, limit=2)
        second = learn.lookup(state, offset=first["next_offset"], limit=2)
        third = learn.lookup(state, offset=second["next_offset"], limit=2)
        self.assertEqual(first["total_sources"], 6)
        self.assertEqual(first["sources"] + second["sources"] + third["sources"], list(state["nodes"])[:-1])
        self.assertIsNone(third["next_offset"])
        self.assertIsNone(state["nodes"]["q0007"]["parent"])
        self.assertEqual(set(first["nodes"]), {"q0001", "q0002"})

    def test_lookup_archived_quote_includes_visible_explanation_and_new_correction(self):
        self.turn("t1", "Caching", "Original statement about storage.")
        self.turn("t2", "> Original statement about storage.\n\nWhy?", "A distinctive archived explanation.")
        self.turn("t3", "> Original statement about storage.\n\nOther detail?", "UNNEEDED FOLDED SIBLING")
        self.enable()
        with self.store.locked() as state:
            learn.condense(state, "q0001", state["revision"], "A cache reuses computed values.")
            self.store.commit(state)
        self.turn("t4", "> A cache reuses computed values.\n\nQualification?", "Inputs must remain unchanged.")
        self.turn("t5", "> A distinctive archived explanation.\n\nExplain again.")
        state = self.sync()
        result = learn.lookup(state)
        self.assertEqual(result["sources"], ["q0002"])
        self.assertEqual(result["nodes"]["q0002"]["visible"], "q0001")
        self.assertEqual(result["nodes"]["q0002"]["answer"], "A distinctive archived explanation.")
        self.assertEqual(result["nodes"]["q0001"]["answer"], "A cache reuses computed values.")
        self.assertEqual(result["nodes"]["q0004"]["answer"], "Inputs must remain unchanged.")
        self.assertNotIn("UNNEEDED FOLDED SIBLING", json.dumps(result))

    def test_lookup_previous_condensed_version_and_original_are_preserved(self):
        self.turn("t1", "Caching", "Original statement about storage.")
        self.enable()
        with self.store.locked() as state:
            learn.condense(state, "q0001", state["revision"], "An intermediate explanation of reuse.")
            learn.condense(state, "q0001", state["revision"], "The current corrected explanation.")
            self.store.commit(state)
        result = learn.lookup(self.state(), query="An intermediate explanation of reuse.")
        node = result["nodes"]["q0001"]
        self.assertEqual(node["answer"], "The current corrected explanation.")
        self.assertEqual(node["original_answer"], "Original statement about storage.")
        self.assertEqual(node["matched_versions"], ["An intermediate explanation of reuse."])

    def test_lookup_deep_archived_answer_keeps_original_ancestor_context(self):
        self.turn("t1", "Root", "Original root definition.")
        self.turn("t2", "> Original root definition.\n\nExplain detail.", "Original nested definition.")
        self.turn("t3", "> Original nested definition.\n\nExplain further.", "Distinctive deep explanation.")
        self.enable()
        state = self.state()
        learn.condense(state, "q0002", state["revision"], "A condensed nested explanation.")
        learn.condense(state, "q0001", state["revision"], "The current integrated explanation.")
        result = learn.lookup(state, query="Distinctive deep explanation.")
        self.assertEqual(result["sources"], ["q0003"])
        self.assertEqual(result["nodes"]["q0002"]["original_answer"], "Original nested definition.")
        self.assertEqual(result["nodes"]["q0001"]["original_answer"], "Original root definition.")
        self.assertEqual(result["nodes"]["q0001"]["answer"], "The current integrated explanation.")

    def test_lookup_unquoted_followup_inspects_previous_answer_but_unmatched_quote_does_not(self):
        self.turn("t1", "Earlier topic", "An earlier answer.")
        self.turn("t2", "Latest topic", "The latest answer.")
        self.turn("t3", "Why?")
        self.enable()
        state = self.state()
        result = learn.lookup(state)
        self.assertEqual(result["sources"], ["q0002"])
        self.assertIsNone(state["nodes"]["q0003"]["parent"])
        self.turn("t4", "> A passage never said before.\n\nWhy?")
        result = learn.lookup(self.sync())
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["nodes"], {})

    def test_lookup_query_does_not_offer_the_in_progress_answer_as_its_own_source(self):
        self.turn("t1", "Root", "A distinctive previous explanation.")
        self.turn("t2", "> A distinctive previous explanation.\n\nWhy?")
        self.message("assistant", "Investigating: A distinctive previous explanation.", phase="commentary")
        self.enable()
        result = learn.lookup(self.state(), query="A distinctive previous explanation.")
        self.assertEqual(result["sources"], ["q0001"])

    def test_outline_searches_saved_answers_but_only_returns_paginated_labels(self):
        self.turn("t1", "A long question " * 40, "秘密的历史答案 about caching.")
        self.turn("t2", "Another question", "秘密的历史答案 about eviction.")
        self.turn("t3", "Unrelated", "A separate answer.")
        self.enable()
        state = self.state()
        first = learn.outline(state, query="秘密的历史答案", limit=1)
        second = learn.outline(state, query="秘密的历史答案", offset=first["next_offset"], limit=1)
        self.assertEqual(first["total"], 2)
        self.assertEqual({first["questions"][0]["node"], second["questions"][0]["node"]}, {"q0001", "q0002"})
        self.assertIsNone(second["next_offset"])
        self.assertNotIn("秘密的历史答案", json.dumps(first, ensure_ascii=False))
        long_question = second["questions"][0]
        self.assertEqual(len(long_question["question"]), 240)
        self.assertGreater(long_question["question_chars"], 240)
        self.assertEqual(learn.outline(state, query="nonexistent")["total"], 0)

    def test_snapshot_branch_has_ancestor_context_and_originals_after_repeated_condensation(self):
        self.turn("t1", "Root", "The root qualification.")
        self.turn("t2", "> The root qualification.\n\nExplain detail.", "The original detailed answer.")
        self.turn("t3", "> The original detailed answer.\n\nCorrect it.", "The original correction.")
        self.turn("t4", "Separate topic", "UNRELATED CONTENT")
        self.enable()
        state = self.state()
        learn.condense(state, "q0002", state["revision"], "First integrated explanation.")
        learn.condense(state, "q0002", state["revision"], "Revised integrated explanation.")
        result = learn.snapshot(state, "q0002")
        self.assertEqual(set(result["nodes"]), {"q0002", "q0003"})
        self.assertEqual(result["context"]["q0001"]["answer"], "The root qualification.")
        self.assertEqual(result["nodes"]["q0002"]["messages"][0]["text"], "The original detailed answer.")
        self.assertEqual(result["nodes"]["q0003"]["messages"][0]["text"], "The original correction.")
        self.assertEqual(result["revision"], state["revision"])
        self.assertNotIn("UNRELATED CONTENT", json.dumps(result))

    def test_namespaced_skill_control_turns_are_not_saved_as_questions(self):
        self.turn("t1", "Question", "Answer")
        self.turn("t2", "$codex-learn:learn", "Learning enabled.")
        self.turn("t3", "$codex-learn:condense", "Condensed.")
        self.enable()
        self.assertEqual(len(self.state()["nodes"]), 1)
        self.assertEqual(learn.control("$codex-learn:learn off"), ("learn", "off"))

    def test_lookup_and_outline_commands_return_retrieval_results_and_reject_invalid_pages(self):
        self.turn("t1", "Caching", "The complete answer.")
        self.enable()
        for command in ("lookup", "outline"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = learn.main([command, "--project", str(self.project), "--session", "session-one", "--node", "q0001"])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertNotIn("enabled", result)
            if command == "lookup":
                self.assertEqual(result["nodes"]["q0001"]["answer"], "The complete answer.")
            else:
                self.assertEqual(result["questions"][0]["node"], "q0001")
        for arguments in (("--offset", "-1"), ("--limit", "0"), ("--all",)):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = learn.main(["lookup", "--project", str(self.project), "--session", "session-one", *arguments])
            self.assertEqual(code, 1)
            self.assertIn("error", json.loads(errors.getvalue()))

    def test_hook_reports_candidate_ids_without_repeating_long_quotes(self):
        passage = "A long passage with an important qualification. " * 80
        self.turn("t1", "First topic", passage)
        self.turn("t2", "Second topic", passage)
        self.enable()
        payload = {"session_id": "session-one", "cwd": str(self.project), "transcript_path": str(self.transcript),
                   "hook_event_name": "UserPromptSubmit", "turn_id": "t3", "prompt": "> " + passage + "\n\nExplain."}
        with patch.dict(os.environ, {"CODEX_LEARN_NO_WATCH": "1"}):
            result = learn.hook(payload)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertLess(len(context), len(passage))
        self.assertNotIn(passage, context)
        self.assertIn('"candidate_count": 2', context)
        self.assertIsNone(self.state()["nodes"]["q0003"]["parent"])

    def test_marketplace_registration_preserves_entries_and_is_idempotent(self):
        spec = importlib.util.spec_from_file_location("register", ROOT / "scripts" / "register.py")
        registration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(registration)
        source = self.project / "plugin"
        source.mkdir()
        (source / "plugin.json").write_text("{}", encoding="utf-8")
        catalog = self.project / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        original = {"name": "existing", "plugins": [{"name": "other", "source": {"source": "local", "path": "./other"}}]}
        catalog.write_text(json.dumps(original), encoding="utf-8")
        registration.register(self.project, source)
        mtime = catalog.stat().st_mtime_ns
        registration.register(self.project, source)
        result = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertEqual(result["plugins"][0], original["plugins"][0])
        self.assertEqual(result["name"], "existing")
        self.assertEqual(len(result["plugins"]), 2)
        self.assertEqual(catalog.stat().st_mtime_ns, mtime)


if __name__ == "__main__":
    unittest.main()
