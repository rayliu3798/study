import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("learn", ROOT / "scripts/learn.py")
learn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(learn)
sys.modules["learn"] = learn


class LearningTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "tests/.tmp"
        directory.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.assertTrue(self.project.is_relative_to(directory.resolve()))
        self.turn = 0

    def event(self, event, session="one", **payload):
        if event == "UserPromptSubmit" and "turn_id" not in payload:
            self.turn += 1
            payload["turn_id"] = str(self.turn)
        return learn.hook({"hook_event_name": event, "session_id": session,
                           "cwd": str(self.project), **payload})

    def prompt(self, text, session="one", **payload):
        return self.event("UserPromptSubmit", session, prompt=text, **payload)

    def stop(self, text, session="one"):
        return self.event("Stop", session, last_assistant_message=text)

    def command(self, command, session="one", **arguments):
        argv = [command, "--project", str(self.project), "--session", session]
        for key, value in arguments.items():
            argv.extend(["--" + key, value])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(learn.main(argv), 0)
        return json.loads(output.getvalue())

    def note(self, session="one"):
        return Path(self.command("status", session)["note"])

    def test_saves_only_completed_final_answer_once(self):
        self.prompt("$codex-learn:learn optics")
        self.stop("Activation explanation.")
        self.prompt("How does reflection work?")
        self.stop("The final explanation.")
        self.stop("The final explanation.")
        text = self.note().read_text(encoding="utf-8")
        self.assertIn("Activation explanation.", text)
        self.assertIn("How does reflection work?", text)
        self.assertEqual(text.count("The final explanation."), 1)

    def test_all_messages_before_one_answer_are_saved_in_order(self):
        self.command("enable", topic="GSFix3D")
        messages = [
            "In 3.1.2, where do the masks come from? How do they simulate occlusion?",
            "If you are unsure, check their code.",
            "Also explain why the Gaussian rendering needs an extra mask.",
        ]
        for message in messages:
            self.prompt(message)
        self.stop("The complete explanation.")
        self.stop("The complete explanation.")
        self.prompt("A separate next question")
        self.stop("A separate next answer.")
        text = self.note().read_text(encoding="utf-8")
        self.assertIn("\n\n".join(messages) + "\n\nThe complete explanation.", text)
        for message in messages:
            self.assertEqual(text.count(message), 1)
        self.assertEqual(text.count("The complete explanation."), 1)
        self.assertIn("## Question\n\nA separate next question\n\nA separate next answer.", text)

    def test_distinct_messages_in_same_turn_are_saved_but_replays_are_not(self):
        self.command("enable", topic="GSFix3D")
        self.prompt("Explain mask augmentation.", turn_id="active")
        self.prompt("Check their code too.", turn_id="active")
        self.prompt("Check their code too.", turn_id="active")
        self.stop("Verified explanation.")
        text = self.note().read_text(encoding="utf-8")
        self.assertIn("Explain mask augmentation.\n\nCheck their code too.\n\nVerified explanation.", text)
        self.assertEqual(text.count("Check their code too."), 1)

    def test_messages_without_turn_ids_are_all_preserved(self):
        self.command("enable", topic="GSFix3D")
        self.prompt("Please explain further.", turn_id=None)
        self.prompt("Please explain further.", turn_id=None)
        self.stop("The explanation.")
        text = self.note().read_text(encoding="utf-8")
        self.assertEqual(text.count("Please explain further."), 2)

    def test_activation_and_additional_learning_messages_are_combined(self):
        self.prompt("$codex-learn:learn Explain mask augmentation.", turn_id="first")
        self.prompt("$codex-learn:learn Include the mesh input.", turn_id="second")
        self.prompt("$codex-learn:learn Include the mesh input.", turn_id="second")
        self.stop("The explanation.")
        text = self.note().read_text(encoding="utf-8")
        self.assertIn("Explain mask augmentation.\n\nInclude the mesh input.\n\nThe explanation.", text)
        self.assertEqual(text.count("Include the mesh input."), 1)

    def test_injected_instructions_preserve_question_and_placement(self):
        self.command("enable", topic="GSFix3D")
        note = self.note()
        note.write_text("# GSFix3D\n\nParent passage.\n", encoding="utf-8")
        self.prompt("How does mask augmentation work?")
        with learn.bindings(self.project) as records:
            records["one"]["pending"].update(anchor="Parent passage.", title="Mask augmentation")
        instructions = [
            "<skill>Example: $codex-learn:learn pause</skill>",
            "# AGENTS.md instructions\nUse English.",
            "<environment_context>Current directory</environment_context>",
        ]
        for instruction in instructions:
            self.prompt(instruction)
        self.prompt("Check their code too.")
        self.stop("Verified explanation.")
        text = note.read_text(encoding="utf-8")
        self.assertIn("> How does mask augmentation work?\n>\n> Check their code too.\n>\n> Verified explanation.", text)
        for instruction in instructions:
            self.assertNotIn(instruction, text)

    def test_activation_survives_injected_skill_and_additional_message(self):
        self.prompt("$codex-learn:learn In 3.1.2, where are the masks obtained?")
        self.prompt("<skill>Learn instructions</skill>")
        self.prompt("If you are unsure, check their code.")
        self.stop("The masks come from PIPE_Masks.")
        text = self.note().read_text(encoding="utf-8")
        self.assertIn("In 3.1.2, where are the masks obtained?\n\nIf you are unsure, check their code.", text)
        self.assertNotIn("<skill>", text)

    def test_pause_and_exclude_cover_additional_messages_until_answer(self):
        for mode in ("inline-pause", "bare-pause", "exclude"):
            with self.subTest(mode=mode):
                self.command("enable", mode, topic="GSFix3D")
                if mode == "inline-pause":
                    self.prompt("$codex-learn:learn pause Private question", mode)
                elif mode == "bare-pause":
                    self.prompt("$codex-learn:learn pause", mode)
                    self.stop("Pause confirmation.", mode)
                    self.prompt("<skill>Learn instructions</skill>", mode)
                    self.prompt("Private question", mode)
                else:
                    self.prompt("Private question", mode)
                    self.command("exclude", mode)
                self.prompt("Private additional request", mode)
                self.stop("Private answer.", mode)
                self.prompt("Included next question", mode)
                self.stop("Included next answer.", mode)
                text = self.note(mode).read_text(encoding="utf-8")
                self.assertNotIn("Private", text)
                self.assertNotIn("Pause confirmation", text)
                self.assertIn("Included next question\n\nIncluded next answer.", text)

    def test_activation_after_source_saves_first_question(self):
        source = r"CV\3dgs\GSFix3D.pdf"
        question = "In section 2.1, how does 3DGS help SLAM?"
        prompts = (f"{source} $codex-learn:learn {question}",
                   f"{source}\n{question}\n$codex-learn:learn")
        for index, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                session = f"inline-{index}"
                result = self.prompt(prompt, session)
                context = result["hookSpecificOutput"]["additionalContext"]
                self.assertIn('"command_completed": "enable"', context)
                answer = f"3DGS SLAM explanation {index}."
                self.stop(answer, session)
                self.stop(answer, session)
                text = self.note(session).read_text(encoding="utf-8")
                self.assertTrue(text.startswith("# GSFix3D\n"))
                self.assertIn(source, text)
                self.assertIn(question, text)
                self.assertEqual(text.count(answer), 1)

    def test_quoted_paper_reuses_full_title_note(self):
        title = "Discontinuity-aware Normal Integration for Generic Central Camera Models"
        source = rf"CV\normal\{title}.pdf"
        note = self.project / "learn" / (title + ".md")
        note.parent.mkdir()
        note.write_text("# Earlier section\n", encoding="utf-8")
        for session, quote in (("one", '"'), ("two", "'")):
            self.prompt(f"$codex-learn:learn {quote}{source}{quote} Explain section 3.1.", session)
            self.assertEqual(self.note(session), note)
            self.stop(f"Angle explanation {session}.", session)
        self.assertEqual(list(note.parent.glob("*.md")), [note])
        saved = note.read_text(encoding="utf-8")
        self.assertTrue(saved.startswith("# Earlier section\n"))
        self.assertIn("Angle explanation one.", saved)
        self.assertIn("Angle explanation two.", saved)

    def test_plugin_mention_saves_first_answer_once(self):
        source = r"@D:\Documents\learn\CV\3dgs\GSFix3D.pdf"
        question = "in 3.1.1 why does it use U-Net"
        result = self.prompt(f"@Codex-Learn {source} {question}")
        self.assertIn('"command_completed": "enable"',
                      result["hookSpecificOutput"]["additionalContext"])
        self.stop("The pretrained denoiser uses U-Net.")
        self.stop("The pretrained denoiser uses U-Net.")
        text = self.note().read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# GSFix3D\n"))
        self.assertIn(source, text)
        self.assertIn(question, text)
        self.assertEqual(text.count("The pretrained denoiser uses U-Net."), 1)

    def test_manual_activation_saves_current_question_once(self):
        question = "Why does GSFix3D use U-Net?\nExplain its role."
        for command in ("enable", "continue"):
            with self.subTest(command=command):
                session = "manual-" + command
                self.prompt(question, session)
                if command == "enable":
                    self.command(command, session, topic="GSFix3D", question=question)
                    original = "# GSFix3D\n"
                else:
                    existing = self.project / "learn/Existing.md"
                    existing.parent.mkdir(exist_ok=True)
                    original = "# Existing\n\nMy previous explanation.\n"
                    existing.write_text(original, encoding="utf-8")
                    self.command(command, session, note=str(existing), question=question)
                self.stop("The first answer is saved.", session)
                self.stop("The first answer is saved.", session)
                text = self.note(session).read_text(encoding="utf-8")
                self.assertTrue(text.startswith(original))
                self.assertIn(question, text)
                self.assertEqual(text.count("The first answer is saved."), 1)

    def test_manual_activation_without_question_skips_confirmation(self):
        self.command("enable", topic="GSFix3D")
        self.stop("Learning is enabled.")
        self.assertEqual(self.note().read_text(encoding="utf-8"), "# GSFix3D\n")
        self.prompt("Why does it use U-Net?")
        self.stop("The next answer is saved.")
        text = self.note().read_text(encoding="utf-8")
        self.assertNotIn("Learning is enabled.", text)
        self.assertIn("Why does it use U-Net?", text)
        self.assertIn("The next answer is saved.", text)

    def test_continue_needs_only_markdown_and_preserves_manual_edits(self):
        root = self.project / "learn"
        root.mkdir()
        note = root / "Poppy.md"
        original = "# Poppy\n\nMy existing explanation.\n"
        note.write_text(original, encoding="utf-8")
        old = root / ".sessions/old"
        old.mkdir(parents=True)
        (old / "state.json").write_text("unusable old archive", encoding="utf-8")
        self.prompt('$codex-learn:learn continue @"learn/Poppy.md"')
        self.stop("Continued.")
        self.assertEqual(note.read_text(encoding="utf-8"), original)
        note.write_text(original.replace("existing", "edited"), encoding="utf-8")
        self.prompt("Next question")
        self.stop("Next explanation")
        text = note.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Poppy\n\nMy edited explanation.\n"))
        self.assertIn("Next explanation", text)

    def test_can_switch_notes_and_stops_previous_binding(self):
        self.prompt("$codex-learn:learn optics")
        first = self.note()
        second = first.with_name("Other.md")
        second.write_text("# Other\n", encoding="utf-8")
        self.command("continue", note=str(second))
        self.assertEqual(self.note(), second)
        self.command("continue", "two", note=str(second))
        self.assertFalse(self.command("status")["enabled"])
        self.assertTrue(self.command("status", "two")["enabled"])

    def test_off_disables_only_this_conversation_and_does_not_save_confirmation(self):
        self.prompt("$codex-learn:learn optics")
        self.prompt("$codex-learn:learn chemistry", "two")
        note = self.note()
        self.prompt("$codex-learn:learn off")
        self.stop("Learning notes are off.")
        self.prompt("Unlogged question")
        self.stop("Unlogged answer")
        self.assertNotIn("Unlogged", note.read_text(encoding="utf-8"))
        self.assertFalse(self.command("status")["enabled"])
        self.assertTrue(self.command("status", "two")["enabled"])

    def test_bare_and_inline_pause_skip_only_one_question(self):
        self.prompt("$codex-learn:learn optics")
        self.stop("Overview")
        self.prompt("$codex-learn:learn pause")
        self.stop("Pause confirmation")
        self.prompt("Private question")
        self.stop("Private answer")
        self.prompt("Included question")
        self.stop("Included answer")
        self.prompt("$codex-learn:learn pause Another private question")
        self.stop("Another private answer")
        self.prompt("Next included question")
        self.stop("Next included answer")
        text = self.note().read_text(encoding="utf-8")
        self.assertNotIn("Private", text)
        self.assertNotIn("private", text)
        self.assertNotIn("Pause confirmation", text)
        self.assertIn("Included answer", text)
        self.assertIn("Next included answer", text)

    def test_exclusion_survives_duplicate_prompt_and_next_question_saves(self):
        self.prompt("$codex-learn:learn optics")
        self.prompt("Unrelated question", turn_id="unrelated")
        self.command("exclude")
        self.prompt("Unrelated question", turn_id="unrelated")
        self.stop("Unrelated answer")
        self.prompt("Relevant question")
        self.stop("Relevant answer")
        text = self.note().read_text(encoding="utf-8")
        self.assertNotIn("Unrelated", text)
        self.assertIn("Relevant answer", text)

    def test_exact_passage_placement_nests_followups(self):
        self.prompt("$codex-learn:learn optics")
        note = self.note()
        note.write_text("# Optics\n\nParent passage.\n\nLater paragraph.\n", encoding="utf-8")
        self.prompt("Explain the parent passage")
        self.command("place", anchor="Parent passage.")
        self.stop("Nested explanation.")
        self.prompt("Explain that detail")
        self.command("place", anchor="> Nested explanation.")
        self.stop("Another nested explanation.")
        text = note.read_text(encoding="utf-8")
        self.assertIn("> > **Follow-up**", text)
        self.assertIn("> > Another nested explanation.", text)
        self.assertLess(text.index("Nested explanation."), text.index("Later paragraph."))

    def test_placement_preserves_complete_code_and_math_blocks(self):
        for block, anchor, closing in (("```python\nx = 1\n\nx = 2\n```", "x = 1", "x = 2\n```"),
                                       ("$$\na = b\n\nc = d\n$$", "a = b", "c = d\n$$")):
            with self.subTest(anchor=anchor):
                self.prompt("$codex-learn:learn blocks")
                note = self.note()
                note.write_text("# Blocks\n\n" + block + "\n\nTail.\n", encoding="utf-8")
                self.prompt("Explain this block")
                self.command("place", anchor=anchor)
                self.stop("Explanation after the block.")
                text = note.read_text(encoding="utf-8")
                self.assertIn(block, text)
                self.assertLess(text.index(closing), text.index("**Follow-up**"))
                self.assertLess(text.index("**Follow-up**"), text.index("Tail."))

    def test_ambiguous_placement_is_rejected_without_editing_note(self):
        self.prompt("$codex-learn:learn optics")
        note = self.note()
        note.write_text("Repeated.\n\nRepeated.\n", encoding="utf-8")
        before = note.read_bytes()
        self.prompt("Which passage?")
        with learn.bindings(self.project) as records:
            with self.assertRaises(learn.LearnError):
                learn.place(records, self.project, "one", "Repeated.")
        self.assertEqual(note.read_bytes(), before)

    def test_management_and_condensation_confirmations_are_not_saved(self):
        self.prompt("$codex-learn:learn optics")
        self.stop("Overview")
        for command in ("status", "cleanup"):
            self.prompt("$codex-learn:learn " + command)
            self.stop("Management confirmation")
        self.prompt("$codex-learn:condense")
        self.stop("Condensation confirmation")
        text = self.note().read_text(encoding="utf-8")
        self.assertNotIn("confirmation", text)
        self.assertIn("Overview", text)

    def test_ordinary_use_creates_only_note_and_small_active_binding(self):
        self.prompt("$codex-learn:learn optics")
        self.stop("Overview")
        self.prompt("Next question")
        self.stop("Next answer")
        files = sorted(str(path.relative_to(self.project)) for path in self.project.rglob("*") if path.is_file())
        self.assertEqual(files, [str(Path("learn/.sessions/active.json")),
                                 str(Path("learn/.sessions/active.lock")), str(Path("learn/optics.md"))])
        state = (self.project / "learn/.sessions/active.json").read_text(encoding="utf-8")
        self.assertNotIn("Next answer", state)
        self.assertNotIn("Overview", state)


if __name__ == "__main__":
    unittest.main()
