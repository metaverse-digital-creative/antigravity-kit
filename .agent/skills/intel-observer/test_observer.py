#!/usr/bin/env python3
"""
test_observer.py — TDD for observer.py (intel/ directory watcher).

The observer tracks file changes across intel/ so multiple AI agents can
coordinate — one agent writes a file, observer emits an event, the next
agent knows to re-run only what changed (not the whole pipeline).

Run:
  python3 -m unittest test_observer -v
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import observer  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────

def make_tree(root: Path, files: dict[str, str]) -> None:
    """Write {relpath: content} into root."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════

class TestScan(unittest.TestCase):
    def test_empty_dir_returns_empty_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = observer.scan(tmp, state_path=None)
        self.assertEqual(events, [])

    def test_first_scan_marks_all_files_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {
                "a.md": "# A",
                "sub/b.json": '{"x": 1}',
            })
            events = observer.scan(tmp, state_path=None)
        types = {e["type"] for e in events}
        self.assertEqual(types, {"added"})
        paths = {e["path"] for e in events}
        self.assertEqual(paths, {"a.md", "sub/b.json"})

    def test_second_scan_with_no_changes_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"a.md": "# A"})
            state_file = Path(tmp) / ".state.json"
            observer.scan(tmp, state_path=state_file)  # seed
            events = observer.scan(tmp, state_path=state_file)
        self.assertEqual(events, [])

    def test_added_file_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / ".state.json"
            make_tree(Path(tmp), {"a.md": "# A"})
            observer.scan(tmp, state_path=state_file)  # seed
            make_tree(Path(tmp), {"b.md": "# B"})
            events = observer.scan(tmp, state_path=state_file)
        added = [e for e in events if e["type"] == "added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["path"], "b.md")

    def test_modified_file_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / ".state.json"
            make_tree(Path(tmp), {"a.md": "first"})
            observer.scan(tmp, state_path=state_file)
            # mtime precision can be ≥1s on some FS; bump content to force sha change
            (Path(tmp) / "a.md").write_text("second", encoding="utf-8")
            events = observer.scan(tmp, state_path=state_file)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "modified")
        self.assertEqual(events[0]["path"], "a.md")
        self.assertNotEqual(events[0].get("sha256_prev"), events[0].get("sha256"))

    def test_deleted_file_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / ".state.json"
            make_tree(Path(tmp), {"a.md": "# A", "b.md": "# B"})
            observer.scan(tmp, state_path=state_file)
            (Path(tmp) / "a.md").unlink()
            events = observer.scan(tmp, state_path=state_file)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "deleted")
        self.assertEqual(events[0]["path"], "a.md")

    def test_state_file_itself_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / ".observer-state.json"
            make_tree(Path(tmp), {"a.md": "# A"})
            observer.scan(tmp, state_path=state_file)
            # scan again — state file should NOT show as "added"
            events = observer.scan(tmp, state_path=state_file)
        self.assertNotIn(".observer-state.json",
                         {e["path"] for e in events})


class TestIgnorePatterns(unittest.TestCase):
    def test_pycache_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {
                "a.py": "x = 1",
                "__pycache__/a.cpython-313.pyc": "bytecode",
                "sub/__pycache__/b.pyc": "bytecode",
            })
            events = observer.scan(tmp, state_path=None)
        paths = {e["path"] for e in events}
        self.assertEqual(paths, {"a.py"})

    def test_ds_store_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"a.md": "# A", ".DS_Store": "junk"})
            events = observer.scan(tmp, state_path=None)
        paths = {e["path"] for e in events}
        self.assertEqual(paths, {"a.md"})

    def test_hidden_observer_state_not_tracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / ".observer-state.json"
            state_file.write_text("{}", encoding="utf-8")
            make_tree(Path(tmp), {"a.md": "# A"})
            events = observer.scan(tmp, state_path=state_file)
        paths = {e["path"] for e in events}
        self.assertIn("a.md", paths)
        self.assertNotIn(".observer-state.json", paths)


class TestEventShape(unittest.TestCase):
    def test_event_has_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"a.md": "# A"})
            events = observer.scan(tmp, state_path=None)
        e = events[0]
        for k in ("type", "path", "size", "mtime", "sha256"):
            self.assertIn(k, e)

    def test_event_is_json_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"a.md": "# A"})
            events = observer.scan(tmp, state_path=None)
        json.dumps(events)  # must not raise


class TestDispatcher(unittest.TestCase):
    """Dispatcher suggests which tool to run next based on changed path."""

    def test_exhibitions_json_triggers_validator(self):
        suggestion = observer.dispatch_for("intel/exhibitions.json")
        self.assertIsNotNone(suggestion)
        self.assertIn("validate", suggestion)

    def test_supply_csv_triggers_enricher(self):
        suggestion = observer.dispatch_for("intel/supply/hebei-cluster/new-entrants.csv")
        self.assertIn("hebei-enrich", suggestion)

    def test_markdown_in_intel_triggers_indexer(self):
        suggestion = observer.dispatch_for("intel/markets/india/segments.md")
        self.assertIn("index", suggestion.lower())

    def test_unknown_path_returns_none(self):
        self.assertIsNone(observer.dispatch_for("somewhere/else/file.txt"))

    def test_dispatcher_attached_to_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"exhibitions.json": '{"exhibitions":[]}'})
            events = observer.scan(tmp, state_path=None, dispatch=True)
        e = events[0]
        self.assertIn("suggested_command", e)


class TestCli(unittest.TestCase):
    def test_main_scan_mode_emits_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"a.md": "# A"})
            state = Path(tmp) / ".state.json"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = observer.main(["scan", "--root", tmp, "--state", str(state)])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("events", data)
        self.assertEqual(len(data["events"]), 1)

    def test_main_no_changes_exits_zero_empty_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"a.md": "# A"})
            state = Path(tmp) / ".state.json"
            observer.main(["scan", "--root", tmp, "--state", str(state)])  # seed
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = observer.main(["scan", "--root", tmp, "--state", str(state)])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["events"], [])

    def test_main_pretty_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"a.md": "# A"})
            state = Path(tmp) / ".state.json"
            buf = io.StringIO()
            with redirect_stdout(buf):
                observer.main(["scan", "--root", tmp, "--state", str(state), "--pretty"])
        self.assertIn("\n  ", buf.getvalue())


class TestStatePersistence(unittest.TestCase):
    def test_state_saved_after_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".state.json"
            make_tree(Path(tmp), {"a.md": "# A"})
            observer.scan(tmp, state_path=state)
            # Assert INSIDE the context so the tmpdir still exists
            self.assertTrue(state.exists())
            data = json.loads(state.read_text())
            self.assertIn("files", data)
            self.assertIn("a.md", data["files"])

    def test_state_loads_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".state.json"
            make_tree(Path(tmp), {"a.md": "# A", "b.md": "# B"})
            e1 = observer.scan(tmp, state_path=state)
            e2 = observer.scan(tmp, state_path=state)
        self.assertEqual(len(e1), 2)
        self.assertEqual(e2, [])


class TestSubprocess(unittest.TestCase):
    SCRIPT = Path(__file__).parent / "observer.py"

    def test_script_runs_standalone(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_tree(Path(tmp), {"x.md": "hello"})
            state = Path(tmp) / ".state.json"
            r = subprocess.run(
                [sys.executable, str(self.SCRIPT), "scan",
                 "--root", tmp, "--state", str(state)],
                capture_output=True, text=True, timeout=10, check=False,
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(len(data["events"]), 1)


if __name__ == "__main__":
    unittest.main()
