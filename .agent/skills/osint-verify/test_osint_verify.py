#!/usr/bin/env python3
"""
test_osint_verify.py — TDD for osint_verify.py

Takes a markdown OSINT dossier (companies + key people + links) and verifies
every URL / email / domain against live signals so the next run is more
accurate.

Run:
  python3 -m unittest test_osint_verify -v                  # unit only
  OSINT_E2E=1 python3 -m unittest test_osint_verify -v      # + real DNS/HTTP
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
import osint_verify as ov  # noqa: E402


SAMPLE_MD = """# India Electrical OSINT Dossiers

## Category A: TRANSFORMER BUYERS

### 1. CG Power and Industrial Solutions Ltd

| Field | Data |
|---|---|
| **Website** | [cgglobal.com](https://www.cgglobal.com) |
| **Email** | help@cgglobal.com / info@cgglobal.com |

**Key Decision Makers:**

| Name | Title | Contact |
|---|---|---|
| Shirish Shah | VP Sourcing | shirish.shah@cgglobal.com |

### 2. Voltamp Transformers Ltd

| Field | Data |
|---|---|
| **Website** | [voltamptransformers.com](https://www.voltamptransformers.com) |
| **Email** | voltamp@voltamptransformers.com |

## Category B: LAMINATION MFRS

### 3. KRYFS Power Components Ltd

| Field | Data |
|---|---|
| **Website** | [kryfs.com](https://www.kryfs.com) |
| **Email** | sales@kryfs.com |

### 4. Bad Entry (broken)

| Field | Data |
|---|---|
| **Website** | [nonexistent.example](https://nonexistent-xyzzy-12345.example) |
| **Email** | contact@nonexistent-xyzzy-12345.example |
"""


# ══════════════════════════════════════════════════════════════════════
# Extraction
# ══════════════════════════════════════════════════════════════════════

class TestExtractCompanies(unittest.TestCase):
    def test_finds_all_sections(self):
        companies = ov.extract_companies(SAMPLE_MD)
        names = {c["name"] for c in companies}
        self.assertEqual(names, {
            "CG Power and Industrial Solutions Ltd",
            "Voltamp Transformers Ltd",
            "KRYFS Power Components Ltd",
            "Bad Entry (broken)",
        })

    def test_each_company_has_urls_and_emails(self):
        companies = ov.extract_companies(SAMPLE_MD)
        cg = next(c for c in companies if c["name"].startswith("CG Power"))
        self.assertIn("https://www.cgglobal.com", cg["urls"])
        self.assertIn("help@cgglobal.com", cg["emails"])
        self.assertIn("shirish.shah@cgglobal.com", cg["emails"])

    def test_categories_attached(self):
        companies = ov.extract_companies(SAMPLE_MD)
        cg = next(c for c in companies if c["name"].startswith("CG Power"))
        self.assertIn("TRANSFORMER BUYERS", cg["category"])

    def test_empty_markdown_returns_empty(self):
        self.assertEqual(ov.extract_companies(""), [])


class TestExtractUrlsEmails(unittest.TestCase):
    def test_extracts_markdown_links(self):
        txt = "See [site](https://example.com/path) and [other](http://foo.bar)"
        urls = ov.extract_urls(txt)
        self.assertIn("https://example.com/path", urls)
        self.assertIn("http://foo.bar", urls)

    def test_extracts_bare_emails(self):
        txt = "Contact: foo@bar.com or baz@qux.co.in"
        emails = ov.extract_emails(txt)
        self.assertEqual(set(emails), {"foo@bar.com", "baz@qux.co.in"})

    def test_dedupes_emails(self):
        txt = "x@y.com x@y.com"
        self.assertEqual(ov.extract_emails(txt), ["x@y.com"])

    def test_domain_from_url(self):
        self.assertEqual(ov.domain_from_url("https://www.foo.bar/path?x=1"), "www.foo.bar")
        self.assertEqual(ov.domain_from_url("http://foo.bar"), "foo.bar")

    def test_domain_from_email(self):
        self.assertEqual(ov.domain_from_email("a@b.co.in"), "b.co.in")


# ══════════════════════════════════════════════════════════════════════
# Verification (mocked)
# ══════════════════════════════════════════════════════════════════════

class TestDnsCheck(unittest.TestCase):
    def test_resolvable_domain_ok(self):
        with mock.patch("socket.gethostbyname", return_value="93.184.216.34"):
            r = ov.check_dns("example.com")
        self.assertTrue(r["ok"])
        self.assertEqual(r["a"], "93.184.216.34")

    def test_unresolvable_domain(self):
        import socket
        with mock.patch("socket.gethostbyname", side_effect=socket.gaierror("not found")):
            r = ov.check_dns("nope.example")
        self.assertFalse(r["ok"])


class TestHttpCheck(unittest.TestCase):
    def test_http_200_ok(self):
        fake = mock.MagicMock()
        fake.status = 200
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=fake):
            r = ov.check_http("https://example.com")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], 200)

    def test_http_404_not_ok(self):
        fake = mock.MagicMock()
        fake.status = 404
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=fake):
            r = ov.check_http("https://example.com/dead")
        self.assertFalse(r["ok"])

    def test_http_network_error(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("no route")):
            r = ov.check_http("https://dead.example")
        self.assertFalse(r["ok"])
        self.assertIn("error", r)


class TestMxCheck(unittest.TestCase):
    def test_mx_present(self):
        # dig returns text; fake subprocess
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="10 mx.example.com.\n20 mx2.example.com.\n",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=fake):
            r = ov.check_mx("example.com")
        self.assertTrue(r["ok"])
        self.assertIn("mx.example.com", r["records"][0])

    def test_mx_absent(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=fake):
            r = ov.check_mx("bare.example")
        self.assertFalse(r["ok"])


# ══════════════════════════════════════════════════════════════════════
# Evidence scoring
# ══════════════════════════════════════════════════════════════════════

class TestEvidenceScore(unittest.TestCase):
    def test_max_score_on_full_live_signals(self):
        score = ov.score_evidence(
            dns_ok=True, http_ok=True, mx_ok=True,
            has_email=True, has_url=True,
        )
        self.assertEqual(score, 5)

    def test_low_score_on_nothing_verified(self):
        score = ov.score_evidence(
            dns_ok=False, http_ok=False, mx_ok=False,
            has_email=True, has_url=True,
        )
        self.assertLessEqual(score, 2)

    def test_domain_live_no_http_partial(self):
        score = ov.score_evidence(
            dns_ok=True, http_ok=False, mx_ok=True,
            has_email=True, has_url=True,
        )
        self.assertGreaterEqual(score, 3)
        self.assertLess(score, 5)


# ══════════════════════════════════════════════════════════════════════
# End-to-end orchestrator (mocked)
# ══════════════════════════════════════════════════════════════════════

class TestVerifyDossier(unittest.TestCase):
    def test_verify_returns_report(self):
        with mock.patch.object(ov, "check_dns", return_value={"ok": True, "a": "1.2.3.4"}), \
             mock.patch.object(ov, "check_http", return_value={"ok": True, "status": 200}), \
             mock.patch.object(ov, "check_mx", return_value={"ok": True, "records": ["mx"]}):
            rep = ov.verify_dossier_text(SAMPLE_MD)
        self.assertIn("companies", rep)
        self.assertIn("checked_at", rep)
        self.assertIn("ok", rep)
        self.assertEqual(len(rep["companies"]), 4)

    def test_report_flags_broken(self):
        def dns(d):
            return {"ok": "nonexistent" not in d, "a": "1.1.1.1" if "nonexistent" not in d else None}
        def http(u):
            return {"ok": "nonexistent" not in u, "status": 200 if "nonexistent" not in u else None}
        def mx(d):
            return {"ok": "nonexistent" not in d, "records": ["mx"] if "nonexistent" not in d else []}
        with mock.patch.object(ov, "check_dns", side_effect=dns), \
             mock.patch.object(ov, "check_http", side_effect=http), \
             mock.patch.object(ov, "check_mx", side_effect=mx):
            rep = ov.verify_dossier_text(SAMPLE_MD)
        bad = [c for c in rep["companies"] if c["name"].startswith("Bad Entry")]
        self.assertEqual(len(bad), 1)
        self.assertLessEqual(bad[0]["evidence_score"], 2)


class TestCli(unittest.TestCase):
    def test_main_emits_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "dossier.md"
            p.write_text(SAMPLE_MD, encoding="utf-8")
            with mock.patch.object(ov, "check_dns", return_value={"ok": True, "a": "x"}), \
                 mock.patch.object(ov, "check_http", return_value={"ok": True, "status": 200}), \
                 mock.patch.object(ov, "check_mx", return_value={"ok": True, "records": ["mx"]}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ov.main(["--dossier", str(p)])
            data = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertIn("companies", data)

    def test_main_missing_file_exits_nonzero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ov.main(["--dossier", "/nonexistent/file.md"])
        self.assertNotEqual(rc, 0)


class TestSubprocess(unittest.TestCase):
    SCRIPT = Path(__file__).parent / "osint_verify.py"

    @unittest.skipUnless(bool(os.environ.get("OSINT_E2E")),
                         "set OSINT_E2E=1 for real network")
    def test_real_dossier_smoke(self):
        """Run against the real dossier in mechas-os."""
        real = (Path(__file__).parent.parent.parent.parent.parent
                / "mechas-os" / "intel" / "markets" / "india"
                / "electrical-osint-dossiers.md")
        if not real.exists():
            self.skipTest(f"real dossier not at {real}")
        r = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--dossier", str(real)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertGreater(len(data["companies"]), 5)


if __name__ == "__main__":
    unittest.main()
