"""Save completed learning answers directly to Markdown using native Codex hooks."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys


class LearnError(Exception):
    pass


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


@contextmanager
def bindings(project):
    directory = Path(project).resolve() / "learn" / ".sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "active.json"
    with (directory / "active.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(records, dict) or any(not isinstance(record, dict) for record in records.values()):
                raise LearnError("Invalid active-note bindings.")
            before = dump(records)
            yield records
            after = dump(records)
            if after != before:
                atomic(path, after)
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def source_stem(text):
    extension = r"\.(?:pdf|docx?|pptx?|md|txt)"
    match = re.search(
        rf'''@?(?:"([^"\r\n]+{extension})"|'([^'\r\n]+{extension})'|([^\s<>"']+{extension})\b)''',
        text, re.I,
    )
    if match:
        path = next(part for part in match.groups() if part is not None)
        return re.split(r"[/\\]", path)[-1].rsplit(".", 1)[0]


def topic_title(text):
    text = text.splitlines()[0]
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text)
    text = " ".join(text.split())[:100].rstrip(" .")
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", text, re.I):
        text = "Note " + text
    return text or "Learning"


def selected_note(project, note):
    selected = str(note).strip().strip("\"'")
    if selected.startswith("@"):
        selected = selected[1:].strip().strip("\"'")
    target = Path(selected)
    if not target.suffix:
        target = target.with_suffix(".md")
    root = Path(project).resolve() / "learn"
    if not target.is_absolute():
        target = (root if target.parent == Path(".") else Path(project)) / target
    target = target.resolve()
    if target.parent != root or target.suffix.lower() != ".md" or not target.is_file():
        raise LearnError("Select an existing Markdown note in this project's learn folder.")
    return target


def bind(records, project, session, note=None, topic=None):
    current = records.get(session, {})
    stem = source_stem(topic or "")
    if note:
        target = selected_note(project, note)
    elif current.get("note") and not stem:
        target = selected_note(project, current["note"])
    else:
        title = topic_title(stem or topic or Path(project).name)
        root = Path(project).resolve() / "learn"
        number = 1
        while True:
            target = root / (title + (f" ({number})" if number > 1 else "") + ".md")
            try:
                with target.open("x", encoding="utf-8") as stream:
                    stream.write("# " + title + "\n")
                break
            except FileExistsError:
                if stem:
                    target = selected_note(project, target)
                    break
                number += 1
    for other, record in records.items():
        if other != session and record.get("note") == target.name:
            record.update(enabled=False, pending=None)
    pending = current.get("pending") if current.get("note") == target.name else None
    records[session] = {"note": target.name, "enabled": True, "pending": pending, "pause": False}


def status(records, project, session):
    record = records.get(session, {})
    return {"enabled": bool(record.get("enabled")),
            "note": str(Path(project).resolve() / "learn" / record["note"]) if record.get("note") else None,
            "skip_next_question": bool(record.get("pause"))}


def block_end(text, offset):
    position, fence, math = 0, None, False
    for line in text.splitlines(keepends=True):
        plain = re.sub(r"^(?:[ \t]*>[ \t]?)+", "", line).strip()
        marker = re.match(r"^(`{3,}|~{3,})", plain)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None and plain == "$$":
            math = not math
        position += len(line)
        if position >= offset and not plain and fence is None and not math:
            return position
    return len(text)


def save_answer(project, record, message):
    pending = record.get("pending")
    if not record.get("enabled") or not pending or not isinstance(message, str) or not message.strip():
        return
    note = selected_note(project, record["note"])
    question = pending["question"]
    anchor = pending.get("anchor")
    if anchor:
        text = note.read_text(encoding="utf-8")
        if text.count(anchor) != 1:
            raise LearnError("The selected passage changed or is ambiguous; select it again.")
        start = text.index(anchor)
        line_start = text.rfind("\n", 0, start) + 1
        prefix = re.match(r"(?:[ \t]*>[ \t]?)*", text[line_start:]).group()
        quote = "> " * (prefix.count(">") + 1)
        entry = "**Follow-up**\n\n" + question + "\n\n" + message.strip()
        entry = "\n".join(quote + line if line else quote.rstrip() for line in entry.splitlines())
        end = block_end(text, start + len(anchor))
        before = text[:end]
        separator = "" if before.endswith("\n\n") else "\n\n"
        updated = before + separator + entry + "\n\n" + text[end:]
        atomic(note, updated)
    else:
        with note.open("a", encoding="utf-8") as stream:
            stream.write("\n\n## Question\n\n" + question + "\n\n" + message.strip() + "\n")
    record["pending"] = None


def place(records, project, session, anchor):
    record = records.get(session, {})
    if not record.get("enabled") or not record.get("pending"):
        raise LearnError("There is no current learning question to place.")
    text = selected_note(project, record["note"]).read_text(encoding="utf-8")
    if not anchor or text.count(anchor) != 1:
        raise LearnError("Select an exact passage that occurs once in the current note.")
    record["pending"]["anchor"] = anchor


def add_question(record, question):
    pending = record.get("pending")
    if pending:
        pending["question"] += "\n\n" + question
    else:
        record["pending"] = {"question": question}


def hook(payload):
    session, project = payload.get("session_id"), payload.get("cwd")
    if not session or not project:
        return {}
    event = payload.get("hook_event_name")
    command = None
    with bindings(project) as records:
        record = records.get(session)
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            instruction = re.match(r"^\s*(?:# AGENTS\.md instructions|<skill>|<environment_context>)", prompt)
            match = None if instruction else re.search(r"(?<!\S)(?:[/\$](?:study:)?(study|condense)|@study)(?=\s|$)(?:\s+(off|status|pause|continue|cleanup)\b)?(.*)", prompt, re.I | re.S)
            if match:
                kind, action, remainder = match.groups()
                action, remainder = (action or "enable").lower(), remainder.strip()
                command = "condense" if kind and kind.lower() == "condense" else action
                if command == "enable":
                    remainder = "\n".join(part for part in (prompt[:match.start()].strip(), remainder) if part)
            turn = payload.get("turn_id")
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            duplicate = record and turn and turn == record.get("turn") and prompt_hash == record.get("prompt_hash")
            if match and not duplicate:
                if command in ("enable", "continue"):
                    bind(records, project, session, note=remainder if command == "continue" else None,
                         topic=remainder or None)
                    record = records[session]
                if record:
                    if command == "enable" and remainder:
                        add_question(record, remainder)
                    else:
                        record["pending"] = None
                        record["skip_answer"] = True
                    if command == "off":
                        record["enabled"] = False
                    elif command == "pause":
                        record["pause"] = not bool(remainder)
            elif not match and record and record.get("enabled") and not instruction and not duplicate:
                if record.get("pause"):
                    record["pending"] = None
                    record["pause"] = False
                    record["skip_answer"] = True
                elif not record.get("skip_answer"):
                    add_question(record, prompt)
            if record and not instruction and not duplicate:
                record["turn"] = turn
                record["prompt_hash"] = prompt_hash
        elif event == "Stop" and record:
            save_answer(project, record, payload.get("last_assistant_message"))
            if isinstance(payload.get("last_assistant_message"), str) and payload["last_assistant_message"].strip():
                record.pop("skip_answer", None)
        elif event == "SessionEnd" and record:
            record["pending"] = None
            record.pop("skip_answer", None)
        state = status(records, project, session)
    if event not in ("UserPromptSubmit", "SessionStart"):
        return {}
    context = {"session": session, "project": str(Path(project).resolve()),
               "script": str(Path(__file__).resolve()), **state,
               "command_completed": command if command in ("enable", "continue", "off", "status", "pause") else None}
    guidance = "study binding: " + json.dumps(context, ensure_ascii=False)
    if state["enabled"]:
        guidance += "\nFollow the study skill. Answer normally; the Stop hook saves the final answer. Read the note for earlier context. Exclude clearly unrelated questions before answering."
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": guidance}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("hook", "enable", "continue", "disable", "status", "pause", "exclude", "place"))
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--session", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--note")
    parser.add_argument("--topic")
    parser.add_argument("--question", help="Current learning question or material to save with this turn's answer")
    parser.add_argument("--anchor")
    args = parser.parse_args(argv)
    try:
        if args.command == "hook":
            result = hook(json.load(sys.stdin))
        else:
            if not args.session:
                raise LearnError("No session ID. Enable and trust the Learn hooks first.")
            with bindings(args.project) as records:
                record = records.get(args.session)
                if args.command in ("enable", "continue"):
                    if args.command == "continue" and not args.note:
                        raise LearnError("Select the note to continue with --note.")
                    bind(records, args.project, args.session, args.note, args.topic)
                    if args.question and args.question.strip():
                        records[args.session]["pending"] = {"question": args.question}
                elif args.command == "disable" and record:
                    record.update(enabled=False, pending=None)
                elif args.command == "pause":
                    if not record or not record.get("enabled"):
                        raise LearnError("Learning notes are not enabled.")
                    record["pause"] = True
                elif args.command == "exclude" and record:
                    record["pending"] = None
                    record["skip_answer"] = True
                elif args.command == "place":
                    place(records, args.project, args.session, args.anchor)
                result = status(records, args.project, args.session)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LearnError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
