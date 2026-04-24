#!/usr/bin/env python3
"""
test_md_graph.py — TDD for md_graph.py

md-graph verifies the LINK GRAPH across a directory of markdown docs:

  - every entity (H1 of a file) referenced by another doc should be
    referenced via an actual [text](path.md) link, not just plaintext
  - links should be bi-directional when both parties exist
    (A links to B → B should link back to A)
  - triangles: when three entities all inter-reference, completeness
    is reported (2/3 pairs = incomplete, 3/3 = complete)
  - orphans: no inbound link from any other file

Complements md-check (which validates LINKS in one file). md-graph
validates the GRAPH across many files.

Run:
  python3 -m unittest test_md_graph -v
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import md_graph as mg  # noqa: E402


# ── Fixture builder ───────────────────────────────────────────────────

def make_corpus(root: Path, docs: dict[str, str]) -> None:
    for rel, content in docs.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
# Heading / entity extraction
# ══════════════════════════════════════════════════════════════════════

class TestExtractH1(unittest.TestCase):
    def test_first_h1_is_entity_name(self):
        text = "# Prabha Industries\n\nSome body"
        self.assertEqual(mg.extract_h1(text), "Prabha Industries")

    def test_trims_trailing_emoji_markers(self):
        text = "# KRYFS Power Components Ltd 🔴 HIGHEST PRIORITY"
        self.assertEqual(mg.extract_h1(text), "KRYFS Power Components Ltd")

    def test_returns_none_if_no_h1(self):
        self.assertIsNone(mg.extract_h1("no heading here"))

    def test_ignores_h2_and_below(self):
        text = "## not-it\n\n# actually-it\n\n## also-not"
        self.assertEqual(mg.extract_h1(text), "actually-it")


# ══════════════════════════════════════════════════════════════════════
# Build entity index across a directory
# ══════════════════════════════════════════════════════════════════════

class TestBuildEntityIndex(unittest.TestCase):
    def test_indexes_every_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "a/prabha.md": "# Prabha Industries\nbody",
                "b/xlar.md":  "# XLAR Enterprises\nbody",
                "c/sub/jj.md": "# JJ Engitech\nbody",
            })
            idx = mg.build_entity_index(root)
        self.assertIn("Prabha Industries", idx)
        self.assertIn("XLAR Enterprises", idx)
        self.assertIn("JJ Engitech", idx)
        # paths relative to root
        self.assertTrue(idx["Prabha Industries"].endswith("a/prabha.md"))

    def test_skips_files_without_h1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "has.md": "# Has H1",
                "nope.md": "just body text",
            })
            idx = mg.build_entity_index(root)
        self.assertIn("Has H1", idx)
        self.assertEqual(len(idx), 1)


# ══════════════════════════════════════════════════════════════════════
# Bare-mention detection (name as plaintext without a link)
# ══════════════════════════════════════════════════════════════════════

class TestBareMentions(unittest.TestCase):
    def test_plaintext_entity_name_flagged(self):
        # "Prabha Industries" is a known entity; appears as plain text → should be a link
        text = "We sold 87 units to Prabha Industries last year."
        bare = mg.find_bare_mentions(text, known_entities=["Prabha Industries"])
        self.assertEqual(bare, ["Prabha Industries"])

    def test_linked_mention_not_flagged(self):
        text = "We sold to [Prabha Industries](./prabha.md) last year."
        bare = mg.find_bare_mentions(text, known_entities=["Prabha Industries"])
        self.assertEqual(bare, [])

    def test_substring_false_positive_prevented(self):
        text = "Prabha Industries Ltd is different from the main one."
        # known entity is "Prabha Industries" but here it's followed by "Ltd"
        # strict word-boundary match — should still flag as bare (partial appearance)
        bare = mg.find_bare_mentions(text, known_entities=["Prabha Industries"])
        self.assertIn("Prabha Industries", bare)

    def test_case_sensitive(self):
        text = "prabha industries (lowercase) vs Prabha Industries"
        bare = mg.find_bare_mentions(text, known_entities=["Prabha Industries"])
        # Only the proper-case plain mention should be flagged
        self.assertEqual(len(bare), 1)

    def test_inside_link_text_not_flagged(self):
        """[Prabha Industries](./prabha.md) is fine — inside link text, already linked."""
        text = "[Prabha Industries](./prabha.md)"
        bare = mg.find_bare_mentions(text, known_entities=["Prabha Industries"])
        self.assertEqual(bare, [])


# ══════════════════════════════════════════════════════════════════════
# Build the full graph
# ══════════════════════════════════════════════════════════════════════

class TestBuildGraph(unittest.TestCase):
    def test_collects_outgoing_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "prabha.md": "# Prabha Industries\nDealer: [XLAR](./xlar.md)",
                "xlar.md": "# XLAR Enterprises\nBuyer: [Prabha Industries](./prabha.md)",
            })
            g = mg.build_graph(root)
        self.assertIn("prabha.md", g)
        self.assertIn("xlar.md", g["prabha.md"]["links_out"])

    def test_graph_has_title_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {"a.md": "# Entity A\ntext"})
            g = mg.build_graph(root)
        self.assertEqual(g["a.md"]["title"], "Entity A")


# ══════════════════════════════════════════════════════════════════════
# Bi-directional / one-way detection
# ══════════════════════════════════════════════════════════════════════

class TestBidirectional(unittest.TestCase):
    def test_mutual_links_are_bidirectional(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "a.md": "# A\nSee [B](./b.md)",
                "b.md": "# B\nSee [A](./a.md)",
            })
            g = mg.build_graph(root)
            pairs = mg.find_one_way_pairs(g)
        self.assertEqual(pairs, [])

    def test_one_way_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "a.md": "# A\nSee [B](./b.md)",   # A → B
                "b.md": "# B\nno back-link",        # B → (nothing)
            })
            g = mg.build_graph(root)
            pairs = mg.find_one_way_pairs(g)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["from"], "a.md")
        self.assertEqual(pairs[0]["to"], "b.md")


# ══════════════════════════════════════════════════════════════════════
# Tri-directional (triangles) — A ↔ B ↔ C ↔ A
# ══════════════════════════════════════════════════════════════════════

class TestTriangles(unittest.TestCase):
    def test_complete_triangle_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "a.md": "# A\n[B](./b.md) [C](./c.md)",
                "b.md": "# B\n[A](./a.md) [C](./c.md)",
                "c.md": "# C\n[A](./a.md) [B](./b.md)",
            })
            g = mg.build_graph(root)
            tris = mg.find_triangles(g)
        complete = [t for t in tris if t["complete"]]
        self.assertEqual(len(complete), 1)
        self.assertEqual(set(complete[0]["nodes"]), {"a.md", "b.md", "c.md"})

    def test_incomplete_triangle_flagged(self):
        """A↔B, A↔C, but B and C don't link to each other (2/3 pairs)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "a.md": "# A\n[B](./b.md) [C](./c.md)",
                "b.md": "# B\n[A](./a.md)",
                "c.md": "# C\n[A](./a.md)",
            })
            g = mg.build_graph(root)
            tris = mg.find_triangles(g)
        incomplete = [t for t in tris if not t["complete"]]
        self.assertGreaterEqual(len(incomplete), 1)


# ══════════════════════════════════════════════════════════════════════
# Orphan detection (no inbound links)
# ══════════════════════════════════════════════════════════════════════

class TestOrphans(unittest.TestCase):
    def test_unreferenced_file_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "hub.md":    "# Hub\nsee [a](./a.md)",
                "a.md":      "# A\nbody",
                "orphan.md": "# Orphan\nno one links me",
            })
            g = mg.build_graph(root)
            orphans = mg.find_orphans(g)
        self.assertIn("orphan.md", orphans)
        self.assertNotIn("a.md", orphans)


# ══════════════════════════════════════════════════════════════════════
# End-to-end verify_graph — single entry point, JSON report
# ══════════════════════════════════════════════════════════════════════

class TestVerifyGraph(unittest.TestCase):
    def test_report_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "prabha.md": "# Prabha Industries\n[XLAR Enterprises](./xlar.md)",
                "xlar.md":   "# XLAR Enterprises\n[Prabha Industries](./prabha.md)",
                "orphan.md": "# Orphan Company",
                "bare.md":   "# Bare Mentioner\nTalks about Prabha Industries without linking",
            })
            rep = mg.verify_graph(root)
        for k in ("root", "checked_at", "files", "bare_mentions",
                  "one_way_pairs", "orphans", "triangles",
                  "evidence_score", "ok", "needs_update"):
            self.assertIn(k, rep)
        self.assertGreaterEqual(len(rep["bare_mentions"]), 1)
        self.assertIn("orphan.md", rep["orphans"])
        self.assertIn("bare.md", rep["orphans"])

    def test_evidence_score_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "a.md": "# A\n[B](./b.md)",
                "b.md": "# B\n[A](./a.md)",
            })
            rep = mg.verify_graph(root)
        self.assertGreaterEqual(rep["evidence_score"], 0)
        self.assertLessEqual(rep["evidence_score"], 5)


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

class TestCli(unittest.TestCase):
    def test_main_emits_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "a.md": "# A\n[B](./b.md)",
                "b.md": "# B\n[A](./a.md)",
            })
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mg.main(["--root", str(root)])
            data = json.loads(buf.getvalue())
        self.assertIn(rc, (0, 1))
        self.assertIn("bare_mentions", data)

    def test_main_missing_root_exits_2(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mg.main(["--root", "/nonexistent/absolutely"])
        self.assertEqual(rc, 2)

    def test_main_pretty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {"a.md": "# A"})
            buf = io.StringIO()
            with redirect_stdout(buf):
                mg.main(["--root", str(root), "--pretty"])
        self.assertIn("\n  ", buf.getvalue())


class TestSubprocess(unittest.TestCase):
    SCRIPT = Path(__file__).parent / "md_graph.py"

    def test_script_runs_standalone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, {
                "a.md": "# A\n[B](./b.md)",
                "b.md": "# B\n[A](./a.md)",
            })
            r = subprocess.run(
                [sys.executable, str(self.SCRIPT), "--root", str(root)],
                capture_output=True, text=True, timeout=15, check=False,
            )
        self.assertIn(r.returncode, (0, 1))
        data = json.loads(r.stdout)
        self.assertEqual(len(data["files"]), 2)


if __name__ == "__main__":
    unittest.main()
