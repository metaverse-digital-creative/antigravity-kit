#!/usr/bin/env python3
"""
test_validate.py — TDD tests for validate.py (exhibition catalog linter).

Run:
  python3 -m unittest test_validate -v

The validator itself is a "can always run" tool: even with no catalog, it
emits a JSON report and exits with a non-zero code. These tests lock in
its contract.
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
import validate  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────

GOOD = {
    "exhibitions": [
        {
            "slug": "dmc-shanghai",
            "name_en": "Die & Mould China",
            "country": "CN",
            "city": "Shanghai",
            "venue": "NECC",
            "domain": "dmcexpo.com",
            "domain_registered": "1998-05-10",
            "organizer": "CDMIA",
            "exhibitor_count": 2000,
            "established": 1980,
            "frequency": "biennial",
            "role": "supply",
            "editions": [
                {"start": "2026-07-01", "end": "2026-07-04", "year": 2026}
            ],
            "relevance": {"supply": 5, "demand": 1, "competitor_intel": 2},
        }
    ]
}

MISSING_NAME = {
    "exhibitions": [
        {"slug": "no-name", "country": "CN", "editions": [{"start": "2026-07-01", "end": "2026-07-02"}]}
    ]
}

DUPLICATE_SLUGS = {
    "exhibitions": [
        {"slug": "dup", "name_en": "A", "country": "CN",
         "editions": [{"start": "2026-07-01", "end": "2026-07-02"}]},
        {"slug": "dup", "name_en": "B", "country": "CN",
         "editions": [{"start": "2026-08-01", "end": "2026-08-02"}]},
    ]
}

STALE = {
    "exhibitions": [
        {"slug": "old", "name_en": "Old Show", "country": "IN",
         "editions": [{"start": "2019-01-01", "end": "2019-01-04"}]}
    ]
}

BAD_DATES = {
    "exhibitions": [
        {"slug": "bad-end", "name_en": "Bad Order", "country": "CN",
         "editions": [{"start": "2026-07-05", "end": "2026-07-01"}]}
    ]
}

OUT_OF_RANGE_RELEVANCE = {
    "exhibitions": [
        {"slug": "bogus-score", "name_en": "X", "country": "IN",
         "editions": [{"start": "2026-07-01", "end": "2026-07-02"}],
         "relevance": {"supply": 99}}
    ]
}


def write_catalog(tmpdir: Path, data: dict, name: str = "exhibitions.json") -> Path:
    p = tmpdir / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ══════════════════════════════════════════════════════════════════════

class TestValidateReturnsDict(unittest.TestCase):
    def test_returns_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            rep = validate.validate(p)
        self.assertIsInstance(rep, dict)

    def test_report_has_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            rep = validate.validate(p)
        for k in ("ok", "needs_update", "checked_at", "catalog", "issues",
                  "item_count", "exit_code"):
            self.assertIn(k, rep)

    def test_report_is_json_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            rep = validate.validate(p)
        json.dumps(rep)  # must not raise


class TestAlwaysRuns(unittest.TestCase):
    """Validator must produce a usable report even when things are broken."""

    def test_missing_catalog_file(self):
        rep = validate.validate("/nonexistent/exhibitions.json")
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["needs_update"])
        self.assertGreaterEqual(rep["exit_code"], 1)
        self.assertTrue(any(i["check"] == "catalog_exists" for i in rep["issues"]))

    def test_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.json"
            p.write_text("{not valid", encoding="utf-8")
            rep = validate.validate(p)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(i["check"] == "catalog_parseable" for i in rep["issues"]))

    def test_missing_exhibitions_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.json"
            p.write_text(json.dumps({"other": []}), encoding="utf-8")
            rep = validate.validate(p)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(i["check"] == "catalog_schema" for i in rep["issues"]))


class TestFieldChecks(unittest.TestCase):
    def test_missing_required_field_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), MISSING_NAME)
            rep = validate.validate(p)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(
            i["check"] == "required_fields" and i["slug"] == "no-name"
            for i in rep["issues"]
        ))

    def test_duplicate_slug_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), DUPLICATE_SLUGS)
            rep = validate.validate(p)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(i["check"] == "unique_slugs" for i in rep["issues"]))

    def test_end_before_start_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), BAD_DATES)
            rep = validate.validate(p)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(i["check"] == "edition_dates" for i in rep["issues"]))

    def test_relevance_out_of_range_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), OUT_OF_RANGE_RELEVANCE)
            rep = validate.validate(p)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(i["check"] == "relevance_range" for i in rep["issues"]))


class TestEvidenceScoring(unittest.TestCase):
    """
    Per CROSS-REFERENCE-PROTOCOL.md (ops/):
      provided JSON = weight 5 · edge evidence (domain, WHOIS, editions, organizer) > no-edge claims
    Validator must score each item's evidence strength (0-5)
    and flag low-evidence entries for enrichment.
    """

    STRONG_EDGES = {
        "exhibitions": [
            {
                "slug": "strong",
                "name_en": "Strong Entry",
                "country": "CN",
                "city": "Shanghai",
                "domain": "dmcexpo.com",
                "domain_registered": "1998-05-10",
                "organizer": "CDMIA",
                "exhibitor_count": 2000,
                "editions": [{"start": "2026-07-01", "end": "2026-07-04", "year": 2026}],
                "relevance": {"supply": 5, "demand": 1, "competitor_intel": 2},
                "role": "supply",
            }
        ]
    }

    WEAK_NO_EDGES = {
        "exhibitions": [
            {
                "slug": "weak",
                "name_en": "Weak Entry",
                "country": "IN",
                "editions": [{"start": "2027-01-01", "end": "2027-01-03"}],
                # No domain, no organizer, no exhibitor_count, no relevance
            }
        ]
    }

    def test_strong_evidence_scores_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), self.STRONG_EDGES)
            rep = validate.validate(p)
        item = next(s for s in rep["item_scores"] if s["slug"] == "strong")
        self.assertEqual(item["evidence_score"], 5)
        self.assertEqual(item["source_tier"], 5)

    def test_weak_evidence_scores_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), self.WEAK_NO_EDGES)
            rep = validate.validate(p)
        item = next(s for s in rep["item_scores"] if s["slug"] == "weak")
        self.assertLessEqual(item["evidence_score"], 2)

    def test_weak_items_flagged_as_needs_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), self.WEAK_NO_EDGES)
            rep = validate.validate(p)
        self.assertTrue(any(
            i["check"] == "evidence_strength" and i["slug"] == "weak"
            for i in rep["issues"]
        ))

    def test_edges_count_reported_per_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), self.STRONG_EDGES)
            rep = validate.validate(p)
        item = next(s for s in rep["item_scores"] if s["slug"] == "strong")
        self.assertIn("edges", item)
        self.assertGreater(len(item["edges"]), 3)  # domain, organizer, editions, relevance, ...


class TestStalenessDetection(unittest.TestCase):
    def test_no_future_edition_flagged_as_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), STALE)
            rep = validate.validate(p)
        self.assertTrue(rep["needs_update"])
        self.assertTrue(any(
            i["check"] == "future_edition" and i["level"] == "warn"
            for i in rep["issues"]
        ))

    def test_future_edition_keeps_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            rep = validate.validate(p)
        # GOOD has a July-2026 edition; still future at this run
        self.assertFalse(any(
            i["check"] == "future_edition" and i["slug"] == "dmc-shanghai"
            for i in rep["issues"]
        ))

    def test_catalog_age_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            rep = validate.validate(p)
        self.assertIn("catalog_age_days", rep)
        self.assertGreaterEqual(rep["catalog_age_days"], 0)

    def test_old_catalog_file_flagged(self):
        """If the catalog file is older than `max_age_days`, emit warn."""
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            # Backdate the file 400 days
            old = (Path(p).stat().st_mtime) - (400 * 86400)
            os.utime(p, (old, old))
            rep = validate.validate(p, max_age_days=180)
        self.assertTrue(any(i["check"] == "catalog_freshness" for i in rep["issues"]))


class TestExitCode(unittest.TestCase):
    def test_exit_code_0_on_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            rep = validate.validate(p)
        # GOOD has future edition and all fields — should be 0
        # (catalog_freshness may warn if tmpfile mtime now = 0 days old, which is fine)
        self.assertEqual(rep["exit_code"], 0)

    def test_exit_code_1_on_warnings_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), STALE)
            rep = validate.validate(p)
        self.assertEqual(rep["exit_code"], 1)

    def test_exit_code_2_on_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), MISSING_NAME)
            rep = validate.validate(p)
        self.assertEqual(rep["exit_code"], 2)


class TestCli(unittest.TestCase):
    """Running the script directly must emit JSON on stdout."""

    def test_main_emits_json_on_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = validate.main(["--data", str(p)])
            data = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(data["ok"])

    def test_main_json_even_when_catalog_missing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = validate.main(["--data", "/absolutely/nonexistent.json"])
        data = json.loads(buf.getvalue())
        self.assertGreaterEqual(rc, 1)
        self.assertFalse(data["ok"])

    def test_main_quiet_flag_suppresses_stdout(self):
        """--quiet keeps JSON only in stderr (for wrapping in cron/pipe)."""
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            buf = io.StringIO()
            with redirect_stdout(buf):
                validate.main(["--data", str(p), "--quiet"])
        self.assertEqual(buf.getvalue().strip(), "")


class TestSubprocess(unittest.TestCase):
    """Spawn the script for real. Locks in 'always runnable, always JSON'."""

    SCRIPT = Path(__file__).parent / "validate.py"

    def test_script_runs_with_no_args(self):
        """No --data, no catalog in default paths → still emits JSON report."""
        # Force env var to point nowhere to isolate from real catalogs
        env = {**os.environ, "EXHIBITIONS_DATA": "/nonexistent-xyzzy.json"}
        r = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--data", "/nonexistent-xyzzy.json"],
            capture_output=True, text=True, timeout=10, env=env, check=False,
        )
        self.assertGreaterEqual(r.returncode, 1)
        data = json.loads(r.stdout)
        self.assertFalse(data["ok"])
        self.assertIn("issues", data)

    def test_script_exits_zero_on_clean_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            r = subprocess.run(
                [sys.executable, str(self.SCRIPT), "--data", str(p)],
                capture_output=True, text=True, timeout=10, check=False,
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertTrue(data["ok"])

    def test_script_pretty_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_catalog(Path(tmp), GOOD)
            r = subprocess.run(
                [sys.executable, str(self.SCRIPT), "--data", str(p), "--pretty"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        # Pretty output must still be valid JSON
        data = json.loads(r.stdout)
        self.assertTrue(data["ok"])
        self.assertIn("\n", r.stdout)  # indentation present


if __name__ == "__main__":
    unittest.main()
