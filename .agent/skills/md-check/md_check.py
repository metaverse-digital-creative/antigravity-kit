#!/usr/bin/env python3
"""
md_check.py — markdown doc verifier for intel/ READMEs and cross-referenced docs.

Validates every link, email, and phone number in a markdown file:
  - Internal links `[text](./rel/path.md)` → file exists?
  - External links `[text](https://...)` → HTTP 200-399?
  - Emails `a@b.c` → MX records exist?
  - Phone numbers (country-aware) → format valid?

Emits a JSON report with evidence_score (0-5). Weight policy from
ops/CROSS-REFERENCE-PROTOCOL.md: provided doc = tier 5; proven edges
(live links + MX-verified emails + valid phones) weighted > no-edge claims.

Usage:
  python3 md_check.py --file intel/markets/india/README.md
  python3 md_check.py --file intel/markets/india/README.md --pretty
  python3 md_check.py --file PATH --country IN      # default IN, also supports US/TW/CN/...
  python3 md_check.py --file PATH --out report.json

Exit codes:
  0 = all checks pass
  1 = some warnings (broken links / missing MX / bad phone format)
  2 = file not found

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

USER_AGENT = "Mozilla/5.0 md-check/1.0"
HTTP_TIMEOUT = 10.0
DNS_TIMEOUT = 5.0
MX_TIMEOUT = 8


# ── Regexes ──────────────────────────────────────────────────────────

LINK_RE = re.compile(r"\[([^\]]+?)\]\(([^)\s]+?)\)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Phones — collect anything with lots of digits + a plus or hyphens
# country-specific validation happens later
PHONE_RE = re.compile(r"(?:\+?\d[\s\-().]?){7,18}\d")


# ── Link extraction ──────────────────────────────────────────────────

def extract_links(text: str) -> list[dict]:
    out = []
    for m in LINK_RE.finditer(text):
        out.append({"text": m.group(1), "url": m.group(2)})
    return out


def is_internal(url: str) -> bool:
    if url.startswith("mailto:") or url.startswith("tel:"):
        return False
    if url.startswith("#"):
        return False
    p = urlparse(url)
    return not p.scheme or p.scheme not in ("http", "https")


# ── Internal link resolution ─────────────────────────────────────────

def check_internal(url: str, base_dir: Union[str, Path]) -> Optional[dict]:
    """Return {ok, path} — or None if url isn't an internal file link."""
    if url.startswith("mailto:") or url.startswith("tel:") or url.startswith("#"):
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return None
    # Strip fragment
    url_noanchor = url.split("#", 1)[0].split("?", 1)[0]
    if not url_noanchor:
        return None
    base = Path(base_dir)
    target = (base / url_noanchor).resolve()
    # Accept directories with trailing /
    if url.endswith("/"):
        return {"ok": target.is_dir(), "path": str(target), "url": url}
    return {"ok": target.exists(), "path": str(target), "url": url}


# ── External HTTP check ──────────────────────────────────────────────

def check_external(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            status = getattr(r, "status", 200)
            return {"ok": 200 <= status < 400, "status": status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"ok": False, "status": None, "error": str(e)}


# ── Emails ────────────────────────────────────────────────────────────

def extract_emails(text: str) -> list[str]:
    seen = set()
    out = []
    for m in EMAIL_RE.finditer(text):
        e = m.group(0).lower()
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


MX_RESOLVERS: tuple[str, ...] = ("", "@8.8.8.8", "@1.1.1.1")  # "" = system resolver


def check_mx(domain: str) -> dict:
    """Resolve MX records, falling back to public resolvers if the local
    resolver returns SERVFAIL (common on Tailscale / corporate networks)."""
    last_err = None
    for resolver in MX_RESOLVERS:
        cmd = ["dig", "+short", "+time=3", "+tries=1", "MX", domain]
        if resolver:
            cmd.insert(1, resolver)
        try:
            r = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=MX_TIMEOUT, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            last_err = str(e)
            continue
        records = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if records:
            return {"ok": True, "records": records, "resolver": resolver or "system"}
        last_err = f"no MX via {resolver or 'system'}"
    return {"ok": False, "records": [], "error": last_err or "no MX"}


# ── Phones ────────────────────────────────────────────────────────────

def extract_phones(text: str, country: str = "IN") -> list[str]:
    out = set()
    for m in PHONE_RE.finditer(text):
        raw = m.group(0).strip()
        # Trim trailing non-digit
        raw = re.sub(r"[^\d)]$", "", raw)
        if is_plausible_phone(raw, country=country):
            out.add(raw)
    return sorted(out)


def is_plausible_phone(raw: str, country: str = "IN") -> bool:
    """Cheap plausibility check — digits-only length falls in country's range."""
    digits = re.sub(r"\D", "", raw)
    if country == "IN":
        # India: 10-digit mobile/landline, optionally +91 prefix (2 more digits)
        return 10 <= len(digits) <= 12
    if country == "US":
        return 10 <= len(digits) <= 11
    if country == "TW":
        return 8 <= len(digits) <= 12
    if country == "CN":
        return 11 <= len(digits) <= 13
    # Default: anything 7-15 digits
    return 7 <= len(digits) <= 15


def is_valid_phone(raw: str, country: str = "IN") -> bool:
    """Stricter: plausible length + at least some numeric structure (no letters)."""
    if re.search(r"[A-Za-z]", raw):
        return False
    return is_plausible_phone(raw, country=country)


# ── Verify orchestration ─────────────────────────────────────────────

def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def verify_doc(
    path: Union[str, Path],
    country: str = "IN",
    max_workers: int = 8,
) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"markdown not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    base_dir = p.parent

    links = extract_links(text)
    emails = extract_emails(text)
    phones = extract_phones(text, country=country)

    # Internal links
    internal_reports = []
    external_urls = []
    for link in links:
        url = link["url"]
        if is_internal(url):
            r = check_internal(url, base_dir=base_dir)
            if r is None:
                continue  # mailto/anchor — skip
            r["text"] = link["text"]
            internal_reports.append(r)
        else:
            external_urls.append(url)

    # External (parallel)
    external_reports = []
    external_urls = list(dict.fromkeys(external_urls))  # dedupe preserving order
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for url, res in zip(external_urls, ex.map(check_external, external_urls)):
            external_reports.append({**res, "url": url})

    # Emails (MX per unique domain)
    domains = list(dict.fromkeys(_email_domain(e) for e in emails))
    domain_mx: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for d, res in zip(domains, ex.map(check_mx, domains)):
            domain_mx[d] = res
    email_reports = [
        {"email": e, "domain": _email_domain(e), **domain_mx.get(_email_domain(e), {"ok": False})}
        for e in emails
    ]

    # Phones
    phone_reports = [
        {"raw": p, "ok": is_valid_phone(p, country=country)}
        for p in phones
    ]

    # Stats + evidence score
    int_ok = sum(1 for r in internal_reports if r["ok"])
    ext_ok = sum(1 for r in external_reports if r["ok"])
    email_ok = sum(1 for r in email_reports if r["ok"])
    phone_ok = sum(1 for r in phone_reports if r["ok"])

    int_total = len(internal_reports)
    ext_total = len(external_reports)
    em_total = len(email_reports)
    ph_total = len(phone_reports)

    def _ratio(ok, total):
        return 1.0 if total == 0 else ok / total

    score_f = (
        _ratio(int_ok, int_total) * 1.5
        + _ratio(ext_ok, ext_total) * 1.5
        + _ratio(email_ok, em_total) * 1.0
        + _ratio(phone_ok, ph_total) * 1.0
    )
    evidence_score = int(round(score_f))
    evidence_score = max(0, min(5, evidence_score))

    needs_update = any([
        int_ok < int_total,
        ext_ok < ext_total,
        email_ok < em_total,
        phone_ok < ph_total,
    ])

    return {
        "file": str(p),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "country": country,
        "internal_links": internal_reports,
        "external_links": external_reports,
        "emails": email_reports,
        "phones": phone_reports,
        "stats": {
            "internal_total": int_total, "internal_ok": int_ok,
            "external_total": ext_total, "external_ok": ext_ok,
            "email_total": em_total, "email_ok": email_ok,
            "phone_total": ph_total, "phone_ok": phone_ok,
        },
        "evidence_score": evidence_score,
        "source_tier": 5,  # provided markdown = tier 5
        "ok": not needs_update,
        "needs_update": needs_update,
    }


# ── CLI ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="md_check",
        description="Markdown intel-doc verifier (links / emails / phones)",
    )
    p.add_argument("--file", required=True, help="path to markdown doc")
    p.add_argument("--country", default="IN",
                   help="country code for phone format (IN, US, TW, CN, ...)")
    p.add_argument("--pretty", action="store_true", help="indent JSON")
    p.add_argument("--out", default=None, help="write report to file instead of stdout")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = verify_doc(args.file, country=args.country)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e), "ok": False}))
        return 2

    payload = json.dumps(report, ensure_ascii=False,
                         indent=2 if args.pretty else None)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(payload)

    return 1 if report.get("needs_update") else 0


if __name__ == "__main__":
    sys.exit(main())
