#!/usr/bin/env python3
"""
test_explorer.py — TDD tests for exhibition-explorer.

Run:
  python3 -m unittest test_explorer -v                       # unit only
  EXPLORER_E2E=1 python3 -m unittest test_explorer -v        # + real CLI spawn

All tests are stdlib. E2E tests are gated behind EXPLORER_E2E.
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
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import explorer  # noqa: E402


# ── Sample catalog used across tests ─────────────────────────────────

SAMPLE = {
    "exhibitions": [
        {
            "slug": "dmc-shanghai",
            "name_en": "Die & Mould China (DMC)",
            "country": "CN",
            "city": "Shanghai",
            "venue": "NECC",
            "domain": "dmcexpo.com",
            "organizer": "CDMIA",
            "frequency": "biennial",
            "editions": [{"start": "2026-07-01", "end": "2026-07-04", "year": 2026}],
            "established": 1980,
            "exhibitor_count": 2000,
            "relevance": {"supply": 5, "demand": 1, "competitor_intel": 2},
            "role": "supply",
            "action": "attend",
            "notes": "Largest Chinese mold component exhibition."
        },
        {
            "slug": "imtex-bangalore",
            "name_en": "IMTEX + Tooltech",
            "country": "IN",
            "city": "Bangalore",
            "venue": "BIEC",
            "domain": "imtex.in",
            "frequency": "biennial",
            "editions": [{"start": "2027-01-20", "end": "2027-01-25", "year": 2027}],
            "established": 1969,
            "relevance": {"supply": 1, "demand": 5, "competitor_intel": 4},
            "role": "demand",
            "action": "attend"
        },
        {
            "slug": "diemex-pune",
            "name_en": "DIEMEX",
            "country": "IN",
            "city": "Pune",
            "domain": "diemex.in",
            "frequency": "biennial",
            "editions": [{"start": "2026-10-15", "end": "2026-10-17", "year": 2026}],
            "established": 2014,
            "relevance": {"supply": 1, "demand": 4, "competitor_intel": 5},
            "role": "demand",
            "action": "scout"
        },
        {
            "slug": "aero-india-bangalore",
            "name_en": "Aero India",
            "country": "IN",
            "city": "Bangalore",
            "domain": "aeroindia.gov.in",
            "frequency": "biennial",
            "editions": [{"start": "2027-02-10", "end": "2027-02-14", "year": 2027}],
            "established": 1996,
            "relevance": {"supply": 0, "demand": 3, "competitor_intel": 2},
            "role": "demand",
            "action": "recon"
        },
    ]
}


def make_catalog_file(tmpdir: Path) -> Path:
    p = tmpdir / "exhibitions.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    return p


# ══════════════════════════════════════════════════════════════════════
# Unit tests
# ══════════════════════════════════════════════════════════════════════

class TestLoadCatalog(unittest.TestCase):
    def test_load_valid_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            items = explorer.load_catalog(p)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["slug"], "dmc-shanghai")

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            explorer.load_catalog(Path("/nonexistent/exhibitions.json"))

    def test_load_malformed_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                explorer.load_catalog(p)

    def test_load_missing_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "no-key.json"
            p.write_text(json.dumps({"other": []}), encoding="utf-8")
            with self.assertRaises(KeyError):
                explorer.load_catalog(p)

    def test_env_var_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            with mock.patch.dict(os.environ, {"EXHIBITIONS_DATA": str(p)}, clear=False):
                items = explorer.load_catalog()
        self.assertEqual(len(items), 4)


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.items = SAMPLE["exhibitions"]

    def test_filter_by_country(self):
        result = explorer.filter_items(self.items, country="CN")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["slug"], "dmc-shanghai")

    def test_filter_by_role(self):
        result = explorer.filter_items(self.items, role="demand")
        self.assertEqual({r["slug"] for r in result},
                         {"imtex-bangalore", "diemex-pune", "aero-india-bangalore"})

    def test_filter_by_date_after(self):
        result = explorer.filter_items(self.items, after="2027-01-01")
        slugs = {r["slug"] for r in result}
        self.assertIn("imtex-bangalore", slugs)
        self.assertIn("aero-india-bangalore", slugs)
        self.assertNotIn("dmc-shanghai", slugs)

    def test_filter_by_date_before(self):
        result = explorer.filter_items(self.items, before="2026-12-31")
        slugs = {r["slug"] for r in result}
        self.assertIn("dmc-shanghai", slugs)
        self.assertIn("diemex-pune", slugs)
        self.assertNotIn("imtex-bangalore", slugs)

    def test_filter_combined(self):
        result = explorer.filter_items(self.items, country="IN", after="2026-12-31")
        slugs = {r["slug"] for r in result}
        self.assertEqual(slugs, {"imtex-bangalore", "aero-india-bangalore"})

    def test_filter_by_min_relevance(self):
        result = explorer.filter_items(self.items, min_score=4, goal="demand")
        slugs = {r["slug"] for r in result}
        self.assertIn("imtex-bangalore", slugs)  # demand=5
        self.assertIn("diemex-pune", slugs)      # demand=4
        self.assertNotIn("aero-india-bangalore", slugs)  # demand=3


class TestScoring(unittest.TestCase):
    def test_score_by_supply_goal(self):
        items = SAMPLE["exhibitions"]
        ranked = explorer.rank(items, goal="supply")
        self.assertEqual(ranked[0]["slug"], "dmc-shanghai")

    def test_score_by_demand_goal(self):
        items = SAMPLE["exhibitions"]
        ranked = explorer.rank(items, goal="demand")
        self.assertEqual(ranked[0]["slug"], "imtex-bangalore")

    def test_score_by_competitor_intel_goal(self):
        items = SAMPLE["exhibitions"]
        ranked = explorer.rank(items, goal="competitor_intel")
        self.assertEqual(ranked[0]["slug"], "diemex-pune")

    def test_unknown_goal_raises(self):
        with self.assertRaises(ValueError):
            explorer.rank(SAMPLE["exhibitions"], goal="bogus")


class TestICS(unittest.TestCase):
    def test_ics_output_has_vcalendar_wrapper(self):
        ics = explorer.to_ics(SAMPLE["exhibitions"])
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR"))
        self.assertTrue(ics.rstrip().endswith("END:VCALENDAR"))
        self.assertIn("VERSION:2.0", ics)

    def test_ics_contains_one_vevent_per_edition(self):
        ics = explorer.to_ics(SAMPLE["exhibitions"])
        self.assertEqual(ics.count("BEGIN:VEVENT"), 4)
        self.assertEqual(ics.count("END:VEVENT"), 4)

    def test_ics_event_has_summary_and_dates(self):
        ics = explorer.to_ics([SAMPLE["exhibitions"][0]])
        self.assertIn("SUMMARY:Die & Mould China (DMC)", ics)
        self.assertIn("DTSTART;VALUE=DATE:20260701", ics)
        self.assertIn("DTEND;VALUE=DATE:20260705", ics)  # exclusive end = end+1

    def test_ics_crlf_line_endings(self):
        # RFC 5545 requires CRLF; explorer should comply
        ics = explorer.to_ics(SAMPLE["exhibitions"])
        self.assertIn("\r\n", ics)


class TestShow(unittest.TestCase):
    def test_show_returns_entry(self):
        entry = explorer.show(SAMPLE["exhibitions"], "dmc-shanghai")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name_en"], "Die & Mould China (DMC)")

    def test_show_returns_none_if_missing(self):
        self.assertIsNone(explorer.show(SAMPLE["exhibitions"], "nope"))


class TestCli(unittest.TestCase):
    def test_parser_list(self):
        args = explorer.build_parser().parse_args(["list"])
        self.assertEqual(args.cmd, "list")

    def test_parser_filter_flags(self):
        args = explorer.build_parser().parse_args([
            "filter", "--country", "IN", "--role", "demand",
            "--after", "2026-12-31", "--min-score", "4", "--goal", "demand",
        ])
        self.assertEqual(args.cmd, "filter")
        self.assertEqual(args.country, "IN")
        self.assertEqual(args.role, "demand")
        self.assertEqual(args.after, "2026-12-31")
        self.assertEqual(args.min_score, 4)

    def test_parser_score_goal_required(self):
        args = explorer.build_parser().parse_args(["score", "--goal", "supply"])
        self.assertEqual(args.cmd, "score")
        self.assertEqual(args.goal, "supply")

    def test_parser_ics(self):
        args = explorer.build_parser().parse_args(["ics", "--out", "/tmp/cal.ics"])
        self.assertEqual(args.cmd, "ics")
        self.assertEqual(args.out, "/tmp/cal.ics")

    def test_parser_show(self):
        args = explorer.build_parser().parse_args(["show", "dmc-shanghai"])
        self.assertEqual(args.cmd, "show")
        self.assertEqual(args.slug, "dmc-shanghai")

    def test_parser_json_flag_top_level(self):
        args = explorer.build_parser().parse_args(["--json", "list"])
        self.assertTrue(args.json)

    def test_parser_data_path_top_level(self):
        args = explorer.build_parser().parse_args(["--data", "/tmp/x.json", "list"])
        self.assertEqual(args.data, "/tmp/x.json")


class TestJsonOutput(unittest.TestCase):
    def test_list_json_is_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            buf = io.StringIO()
            with redirect_stdout(buf):
                explorer.cmd_list(str(p), output="json")
            data = json.loads(buf.getvalue())
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 4)

    def test_show_json_is_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            buf = io.StringIO()
            with redirect_stdout(buf):
                explorer.cmd_show(str(p), "dmc-shanghai", output="json")
            data = json.loads(buf.getvalue())
        self.assertEqual(data["slug"], "dmc-shanghai")

    def test_show_missing_is_error_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            buf = io.StringIO()
            with redirect_stdout(buf):
                explorer.cmd_show(str(p), "ghost", output="json")
            data = json.loads(buf.getvalue())
        self.assertIn("error", data)


class TestMain(unittest.TestCase):
    def test_main_list_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            rc = explorer.main(["--data", str(p), "list"])
        self.assertEqual(rc, 0)

    def test_main_score_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            rc = explorer.main(["--data", str(p), "score", "--goal", "supply"])
        self.assertEqual(rc, 0)

    def test_main_missing_data_is_error(self):
        rc = explorer.main(["--data", "/nonexistent.json", "list"])
        self.assertNotEqual(rc, 0)


# ══════════════════════════════════════════════════════════════════════
# E2E — spawn the script, check outputs
# ══════════════════════════════════════════════════════════════════════

E2E_ENABLED = bool(os.environ.get("EXPLORER_E2E"))


@unittest.skipUnless(E2E_ENABLED, "set EXPLORER_E2E=1 to enable CLI spawn tests")
class TestE2ECli(unittest.TestCase):
    SCRIPT = Path(__file__).parent / "explorer.py"

    def _run(self, *args, data_path, timeout=10):
        env = {**os.environ, "EXHIBITIONS_DATA": str(data_path)}
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args],
            capture_output=True, text=True, timeout=timeout, check=False, env=env,
        )

    def test_list_text_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            r = self._run("list", data_path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dmc-shanghai", r.stdout)
        self.assertIn("imtex-bangalore", r.stdout)

    def test_list_json_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            r = self._run("--json", "list", data_path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(len(data), 4)

    def test_ics_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            out = Path(tmp) / "out.ics"
            r = self._run("ics", "--out", str(out), data_path=p)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(out.exists())
            self.assertTrue(out.read_text().startswith("BEGIN:VCALENDAR"))

    def test_help_works(self):
        r = self._run("--help", data_path="/dev/null")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage", r.stdout.lower())

    def test_show_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_catalog_file(Path(tmp))
            r = self._run("show", "dmc-shanghai", data_path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Die & Mould China", r.stdout)


if __name__ == "__main__":
    unittest.main()
