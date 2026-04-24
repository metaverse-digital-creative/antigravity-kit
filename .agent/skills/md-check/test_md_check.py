#!/usr/bin/env python3
"""
test_md_check.py — TDD for md_check.py (markdown intel doc verifier).

Where osint-verify is company-centric (extract company sections → score),
md-check is doc-centric: validate every internal link resolves to a real
file, every external link returns 200-399, every email has MX records,
every phone number matches E.164-ish India format (country-aware).

Covers `intel/markets/india/README.md` specifically: 530 lines of
referenced companies, dealers, exhibitions, URLs, and phone numbers.

Run:
  python3 -m unittest test_md_check -v
  MD_CHECK_E2E=1 python3 -m unittest test_md_check -v   # + real HTTP
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import md_check as mc  # noqa: E402


SAMPLE_MD = """# India Die & Mold Industry Analysis

> **Created:** 2026-04-20 · **Companion docs:** [competitive strategy](./pipeline/competitive-strategy.md) · [clients](./clients/)

---

## 1. Market Size

Total market: ₹23,800 Crore (~USD 2.8B)

| Field | Value |
|---|---|
| **Member search** | [tagmaindia.org/member_search](https://www.tagmaindia.org/member_search) |
| **Contact** | tagma.mumbai@tagmaindia.org · +91-96534-27396 / +91-97694-07809 |

## 2. Prabha Industries

- Email: bopaiah@prabhaindustries.com
- Phone: +91-80-2345-6789
- Website: [prabhaindustries.com](https://www.prabhaindustries.com)

## 3. Broken Link

- Dead internal: [gone](./does-not-exist.md)
- Dead external: [404](https://nonexistent-xyz-9999.example/path)
"""


# ══════════════════════════════════════════════════════════════════════
# Link extraction
# ══════════════════════════════════════════════════════════════════════

class TestExtractLinks(unittest.TestCase):
    def test_extracts_all_markdown_links(self):
        links = mc.extract_links(SAMPLE_MD)
        urls = {l["url"] for l in links}
        self.assertIn("./pipeline/competitive-strategy.md", urls)
        self.assertIn("./clients/", urls)
        self.assertIn("https://www.tagmaindia.org/member_search", urls)
        self.assertIn("https://www.prabhaindustries.com", urls)
        self.assertIn("https://nonexistent-xyz-9999.example/path", urls)

    def test_classifies_internal_vs_external(self):
        self.assertTrue(mc.is_internal("./pipeline/x.md"))
        self.assertTrue(mc.is_internal("../foo/bar.md"))
        self.assertTrue(mc.is_internal("./clients/"))
        self.assertFalse(mc.is_internal("https://example.com"))
        self.assertFalse(mc.is_internal("http://example.com"))
        self.assertFalse(mc.is_internal("mailto:a@b.c"))

    def test_link_has_anchor_text(self):
        links = mc.extract_links("See [the catalog](https://ex.com)")
        self.assertEqual(links[0]["text"], "the catalog")
        self.assertEqual(links[0]["url"], "https://ex.com")


# ══════════════════════════════════════════════════════════════════════
# Internal link resolution
# ══════════════════════════════════════════════════════════════════════

class TestInternalLinks(unittest.TestCase):
    def test_resolves_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "pipeline").mkdir()
            (base / "pipeline" / "strategy.md").write_text("x")
            r = mc.check_internal("./pipeline/strategy.md", base_dir=base)
        self.assertTrue(r["ok"])

    def test_flags_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = mc.check_internal("./nope.md", base_dir=Path(tmp))
        self.assertFalse(r["ok"])

    def test_resolves_directory_with_trailing_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "clients").mkdir()
            r = mc.check_internal("./clients/", base_dir=base)
        self.assertTrue(r["ok"])

    def test_resolves_parent_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "sub"
            base.mkdir()
            (Path(tmp) / "sibling.md").write_text("x")
            r = mc.check_internal("../sibling.md", base_dir=base)
        self.assertTrue(r["ok"])

    def test_skips_mailto(self):
        r = mc.check_internal("mailto:a@b.c", base_dir=Path("/tmp"))
        self.assertIsNone(r)

    def test_skips_anchor(self):
        r = mc.check_internal("#section-2", base_dir=Path("/tmp"))
        self.assertIsNone(r)


# ══════════════════════════════════════════════════════════════════════
# External HTTP check (mocked)
# ══════════════════════════════════════════════════════════════════════

class TestExternalLinks(unittest.TestCase):
    def test_http_200_ok(self):
        fake = mock.MagicMock()
        fake.status = 200
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=fake):
            r = mc.check_external("https://ex.com")
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], 200)

    def test_http_404_not_ok(self):
        fake = mock.MagicMock()
        fake.status = 404
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=fake):
            r = mc.check_external("https://ex.com/dead")
        self.assertFalse(r["ok"])

    def test_http_error(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("no route")):
            r = mc.check_external("https://dead.example")
        self.assertFalse(r["ok"])
        self.assertIn("error", r)


# ══════════════════════════════════════════════════════════════════════
# Emails + MX
# ══════════════════════════════════════════════════════════════════════

class TestEmails(unittest.TestCase):
    def test_extracts_emails(self):
        emails = mc.extract_emails(SAMPLE_MD)
        self.assertIn("tagma.mumbai@tagmaindia.org", emails)
        self.assertIn("bopaiah@prabhaindustries.com", emails)

    def test_dedupes(self):
        self.assertEqual(
            mc.extract_emails("a@x.com a@x.com"),
            ["a@x.com"],
        )

    def test_mx_check_ok(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="10 mx.example.com.\n", stderr="",
        )
        with mock.patch("subprocess.run", return_value=fake):
            r = mc.check_mx("example.com")
        self.assertTrue(r["ok"])

    def test_mx_check_missing(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=fake):
            r = mc.check_mx("bare.example")
        self.assertFalse(r["ok"])


# ══════════════════════════════════════════════════════════════════════
# Phones
# ══════════════════════════════════════════════════════════════════════

class TestPhones(unittest.TestCase):
    def test_extract_india_phones(self):
        phones = mc.extract_phones(SAMPLE_MD, country="IN")
        # +91-96534-27396 and +91-97694-07809 and +91-80-2345-6789
        self.assertGreaterEqual(len(phones), 3)
        self.assertTrue(any("96534" in p for p in phones))
        self.assertTrue(any("80-2345" in p or "8023456789" in p for p in phones))

    def test_validate_india_phone_format_mobile(self):
        self.assertTrue(mc.is_valid_phone("+91-96534-27396", country="IN"))
        self.assertTrue(mc.is_valid_phone("+919653427396", country="IN"))
        self.assertTrue(mc.is_valid_phone("9653427396", country="IN"))

    def test_validate_india_phone_format_landline(self):
        # Landlines: +91-XX-XXXX-XXXX or similar
        self.assertTrue(mc.is_valid_phone("+91-80-2345-6789", country="IN"))

    def test_rejects_too_short(self):
        self.assertFalse(mc.is_valid_phone("12345", country="IN"))

    def test_rejects_letters(self):
        self.assertFalse(mc.is_valid_phone("+91-call-us", country="IN"))


# ══════════════════════════════════════════════════════════════════════
# End-to-end verify_doc
# ══════════════════════════════════════════════════════════════════════

class TestVerifyDoc(unittest.TestCase):
    def test_returns_report_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "pipeline").mkdir()
            (base / "pipeline" / "competitive-strategy.md").write_text("x")
            (base / "clients").mkdir()
            doc = base / "README.md"
            doc.write_text(SAMPLE_MD, encoding="utf-8")
            with mock.patch.object(mc, "check_external",
                                   return_value={"ok": True, "status": 200}), \
                 mock.patch.object(mc, "check_mx",
                                   return_value={"ok": True, "records": ["mx"]}):
                rep = mc.verify_doc(doc)
        for k in ("file", "checked_at", "internal_links", "external_links",
                  "emails", "phones", "ok", "needs_update", "stats", "evidence_score"):
            self.assertIn(k, rep)

    def test_broken_internal_link_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            doc = base / "README.md"
            doc.write_text(SAMPLE_MD, encoding="utf-8")
            with mock.patch.object(mc, "check_external",
                                   return_value={"ok": True, "status": 200}), \
                 mock.patch.object(mc, "check_mx",
                                   return_value={"ok": True, "records": ["mx"]}):
                rep = mc.verify_doc(doc)
        broken = [x for x in rep["internal_links"] if not x["ok"]]
        self.assertGreaterEqual(len(broken), 1)
        self.assertTrue(rep["needs_update"])

    def test_broken_external_link_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "pipeline").mkdir()
            (base / "pipeline" / "competitive-strategy.md").write_text("x")
            (base / "clients").mkdir()
            doc = base / "README.md"
            doc.write_text(SAMPLE_MD, encoding="utf-8")

            def ext(url):
                return {"ok": "nonexistent" not in url,
                        "status": 200 if "nonexistent" not in url else None}

            with mock.patch.object(mc, "check_external", side_effect=ext), \
                 mock.patch.object(mc, "check_mx",
                                   return_value={"ok": True, "records": ["mx"]}):
                rep = mc.verify_doc(doc)
        broken_ext = [x for x in rep["external_links"] if not x["ok"]]
        self.assertGreaterEqual(len(broken_ext), 1)

    def test_evidence_score_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "pipeline").mkdir()
            (base / "pipeline" / "competitive-strategy.md").write_text("x")
            (base / "clients").mkdir()
            doc = base / "README.md"
            doc.write_text(SAMPLE_MD, encoding="utf-8")
            with mock.patch.object(mc, "check_external",
                                   return_value={"ok": True, "status": 200}), \
                 mock.patch.object(mc, "check_mx",
                                   return_value={"ok": True, "records": ["mx"]}):
                rep = mc.verify_doc(doc)
        self.assertGreaterEqual(rep["evidence_score"], 0)
        self.assertLessEqual(rep["evidence_score"], 5)


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

class TestCli(unittest.TestCase):
    def test_main_emits_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "pipeline").mkdir()
            (base / "pipeline" / "competitive-strategy.md").write_text("x")
            (base / "clients").mkdir()
            doc = base / "README.md"
            doc.write_text(SAMPLE_MD, encoding="utf-8")
            with mock.patch.object(mc, "check_external",
                                   return_value={"ok": True, "status": 200}), \
                 mock.patch.object(mc, "check_mx",
                                   return_value={"ok": True, "records": ["mx"]}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = mc.main(["--file", str(doc)])
            data = json.loads(buf.getvalue())
        # needs_update=True means exit 1 — acceptable
        self.assertIn(rc, (0, 1))
        self.assertIn("internal_links", data)
        self.assertIn("phones", data)

    def test_main_missing_file_exits_2(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mc.main(["--file", "/nonexistent/readme.md"])
        self.assertEqual(rc, 2)


@unittest.skipUnless(bool(os.environ.get("MD_CHECK_E2E")),
                     "set MD_CHECK_E2E=1 for real HTTP check")
class TestE2ERealReadme(unittest.TestCase):
    def test_real_india_readme(self):
        real = (Path(__file__).parent.parent.parent.parent.parent
                / "mechas-os" / "intel" / "markets" / "india" / "README.md")
        if not real.exists():
            self.skipTest(f"not at {real}")
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "md_check.py"),
             "--file", str(real)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        # Exit 0 (all clean) or 1 (some warnings) — 2 would mean file missing
        self.assertIn(r.returncode, (0, 1), r.stderr)
        data = json.loads(r.stdout)
        self.assertGreater(len(data["external_links"]), 0)
        self.assertGreater(len(data["phones"]), 0)


if __name__ == "__main__":
    unittest.main()
