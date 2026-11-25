"""Reproducible local throughput check; no actual conversations are read."""
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
import study


def main():
    root = Path(__file__).resolve().parents[1] / "tests" / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as folder:
        project = Path(folder).resolve()
        assert project.is_relative_to(root.resolve())
        transcript = project / "rollout.jsonl"
        session = "benchmark"
        def record(role, text):
            return json.dumps({"type": "response_item", "payload": {"type": "message", "role": role,
                "phase": "final_answer", "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}) + "\n"
        with transcript.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
            for index in range(1000):
                stream.write(record("user", f"Explain topic {index}."))
                stream.write(record("assistant", f"Unique conclusion for topic {index}.\n\n" + "An explanation with definitions, examples, and qualifications. " * 20))
        store = study.Store(project, session)
        started = time.perf_counter()
        study.enable(store, transcript)
        backfill_seconds = time.perf_counter() - started
        with store.locked() as state:
            cursor = state["cursor"]
        update = record("user", "> Unique conclusion for topic 999.\n\nExplain this further.") + record("assistant", "A more detailed explanation.")
        with transcript.open("ab") as stream:
            stream.write(update.encode("utf-8"))
        started = time.perf_counter()
        with store.locked() as state:
            study.sync(state)
            store.commit(state)
        update_seconds = time.perf_counter() - started
        appended_bytes = state["cursor"] - cursor
        with patch.object(study, "atomic", wraps=study.atomic) as writes:
            started = time.perf_counter()
            for _ in range(20):
                with store.locked() as state:
                    study.sync(state)
                    store.commit(state)
            idle_seconds = time.perf_counter() - started
            idle_writes = writes.call_count
        print(study.dump({"questions": 1000, "transcript_bytes": transcript.stat().st_size,
            "backfill_seconds": round(backfill_seconds, 3), "quoted_update_seconds": round(update_seconds, 3),
            "new_bytes_read": appended_bytes, "expected_new_bytes": len(update.encode("utf-8")),
            "idle_syncs": 20, "idle_seconds": round(idle_seconds, 3), "idle_writes": idle_writes}).strip())
        assert appended_bytes == len(update.encode("utf-8")) and idle_writes == 0


if __name__ == "__main__":
    main()
