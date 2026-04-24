#!/usr/bin/env python3
"""
osint_verify.py — verify OSINT dossier accuracy via live signals.

Input:  an OSINT dossier (markdown) with company sections, links, emails
Output: JSON report scoring each company's evidence strength
        (per CROSS-REFERENCE-PROTOCOL weight policy)

For each company extracted from the dossier, we run:
  - DNS resolution (A record) for every domain referenced
  - HTTP HEAD/GET to every URL (status code)
  - MX record check (via `dig +short MX`) for every email domain

Evidence score (0-5) reflects how many live-signal edges verified.
Companies with low scores are flagged as `needs_update`.

Stdlib only. External calls: socket (DNS), urllib (HTTP), subprocess (dig).

Usage:
    python3 osint_verify.py --dossier intel/markets/india/electrical-osint-dossiers.md
    python3 osint_verify.py --dossier PATH --pretty
    python3 osint_verify.py --dossier PATH --min-score 4   # filter
    python3 osint_verify.py --dossier PATH --out report.json

Exit codes:
    0 = all companies passed their evidence threshold
    1 = some warnings (weak entries, broken links, missing MX)
    2 = dossier file missing / unreadable
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

USER_AGENT = "Mozilla/5.0 osint-verify/1.0"
HTTP_TIMEOUT = 10.0
DNS_TIMEOUT = 5.0
MX_TIMEOUT = 8


# ── Extraction ────────────────────────────────────────────────────────

URL_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
BARE_URL_RE = re.compile(r"(?<!\()(https?://[^\s)<>]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECTION_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
SECTION_H3_RE = re.compile(r"^###\s+(?:\d+\.\s+)?(.+?)\s*$", re.MULTILINE)


def extract_urls(text: str) -> list[str]:
    out: list[str] = []
    for _, u in URL_RE.findall(text):
        out.append(u)
    for u in BARE_URL_RE.findall(text):
        out.append(u)
    # dedupe preserving order
    seen = set()
    uniq = []
    for u in out:
        u = u.rstrip(").,;")
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def extract_emails(text: str) -> list[str]:
    seen = set()
    out: list[str] = []
    for m in EMAIL_RE.finditer(text):
        e = m.group(0).lower()
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def domain_from_url(url: str) -> str:
    p = urlparse(url)
    return p.netloc


def domain_from_email(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def extract_companies(markdown: str) -> list[dict]:
    """
    Split dossier by ## Category and ### Company headings.
    Each company section collects URLs + emails referenced in that block.
    """
    if not markdown.strip():
        return []

    lines = markdown.splitlines(keepends=False)
    companies: list[dict] = []
    current_category = ""
    current_company: Optional[dict] = None
    buffer: list[str] = []

    def _flush():
        if current_company is None:
            return
        block = "\n".join(buffer)
        current_company["urls"] = extract_urls(block)
        current_company["emails"] = extract_emails(block)
        companies.append(current_company)

    for line in lines:
        m2 = re.match(r"^##\s+(.+?)\s*$", line)
        m3 = re.match(r"^###\s+(?:\d+\.\s+)?(.+?)\s*$", line)
        if m2 and not line.startswith("###"):
            _flush()
            current_company = None
            buffer = []
            current_category = m2.group(1).strip()
            continue
        if m3:
            _flush()
            name = m3.group(1).strip()
            # Strip trailing emoji / markers: 🔴 TOP TARGET etc.
            name = re.sub(r"\s*[🔴🟡🟢📡⬜]\s*.*$", "", name).strip()
            current_company = {
                "name": name,
                "category": current_category,
                "urls": [],
                "emails": [],
            }
            buffer = []
            continue
        if current_company is not None:
            buffer.append(line)

    _flush()
    return companies


# ── Live checks ───────────────────────────────────────────────────────

def check_dns(domain: str) -> dict:
    """Resolve A record. Returns {ok, a, error?}."""
    socket.setdefaulttimeout(DNS_TIMEOUT)
    try:
        a = socket.gethostbyname(domain)
        return {"ok": True, "a": a}
    except (socket.gaierror, socket.timeout, OSError) as e:
        return {"ok": False, "a": None, "error": str(e)}


def check_http(url: str) -> dict:
    """GET with short body read. Returns {ok, status, error?}."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            status = getattr(r, "status", 200)
            return {"ok": 200 <= status < 400, "status": status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "status": None, "error": str(e)}


def check_mx(domain: str) -> dict:
    """Use `dig +short MX` to check mail exchangers. Returns {ok, records, error?}."""
    try:
        r = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=MX_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return {"ok": False, "records": [], "error": str(e)}
    records = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return {"ok": bool(records), "records": records}


# ── Evidence scoring ──────────────────────────────────────────────────

def score_evidence(
    dns_ok: bool,
    http_ok: bool,
    mx_ok: bool,
    has_email: bool,
    has_url: bool,
) -> int:
    """0-5 score based on how many live-signal edges verified."""
    score = 0
    if has_url and dns_ok:
        score += 2
    if has_url and http_ok:
        score += 1
    if has_email and mx_ok:
        score += 2
    # Partial credit for DNS without HTTP (site returning 503 but domain real)
    if dns_ok and not http_ok and has_url:
        score += 0  # no boost
    return min(5, score)


# ── Orchestration ─────────────────────────────────────────────────────

def _verify_one(company: dict) -> dict:
    urls = company["urls"]
    emails = company["emails"]
    url_reports: list[dict] = []
    email_reports: list[dict] = []

    # Verify each URL
    for url in urls:
        d = domain_from_url(url)
        dns = check_dns(d) if d else {"ok": False, "a": None}
        http = check_http(url) if dns["ok"] else {"ok": False, "status": None, "error": "dns_failed"}
        url_reports.append({"url": url, "domain": d, "dns": dns, "http": http})

    # Verify each email domain (dedupe)
    email_domains: dict[str, dict] = {}
    for em in emails:
        d = domain_from_email(em)
        if d in email_domains:
            continue
        dns = check_dns(d)
        mx = check_mx(d) if dns["ok"] else {"ok": False, "records": [], "error": "dns_failed"}
        email_domains[d] = {"domain": d, "dns": dns, "mx": mx}
    for em in emails:
        d = domain_from_email(em)
        info = email_domains.get(d, {})
        email_reports.append({
            "email": em,
            "domain": d,
            "dns": info.get("dns", {"ok": False}),
            "mx": info.get("mx", {"ok": False}),
        })

    dns_any = any(u["dns"]["ok"] for u in url_reports) or \
              any(e["dns"]["ok"] for e in email_reports)
    http_any = any(u["http"]["ok"] for u in url_reports)
    mx_any = any(e["mx"]["ok"] for e in email_reports)

    evidence = score_evidence(
        dns_ok=dns_any, http_ok=http_any, mx_ok=mx_any,
        has_email=bool(emails), has_url=bool(urls),
    )

    return {
        **company,
        "url_reports": url_reports,
        "email_reports": email_reports,
        "evidence_score": evidence,
        "source_tier": 5,  # provided dossier = tier 5 per CROSS-REFERENCE-PROTOCOL
        "needs_update": evidence < 3,
    }


def verify_dossier_text(text: str, max_workers: int = 6) -> dict:
    companies = extract_companies(text)
    if not companies:
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "companies": [],
            "ok": True,
            "needs_update": False,
            "stats": {"total": 0, "passing": 0, "weak": 0},
        }

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        verified = list(ex.map(_verify_one, companies))

    weak = [c for c in verified if c["needs_update"]]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "companies": verified,
        "ok": len(weak) == 0,
        "needs_update": len(weak) > 0,
        "stats": {
            "total": len(verified),
            "passing": len(verified) - len(weak),
            "weak": len(weak),
            "weak_slugs": [c["name"] for c in weak],
        },
    }


def verify_dossier_file(path: Union[str, Path]) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"dossier not found: {p}")
    text = p.read_text(encoding="utf-8")
    report = verify_dossier_text(text)
    report["source"] = str(p)
    return report


# ── CLI ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osint_verify",
        description="Verify OSINT dossier accuracy via live signals",
    )
    p.add_argument("--dossier", required=True,
                   help="path to dossier markdown")
    p.add_argument("--out", default=None,
                   help="write report JSON here (default: stdout)")
    p.add_argument("--pretty", action="store_true", help="indent JSON")
    p.add_argument("--min-score", type=int, default=None,
                   help="filter to companies with evidence_score >= N")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        rep = verify_dossier_file(args.dossier)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e), "ok": False}), file=sys.stdout)
        return 2
    except OSError as e:
        print(json.dumps({"error": str(e), "ok": False}), file=sys.stdout)
        return 2

    if args.min_score is not None:
        rep["companies"] = [
            c for c in rep["companies"]
            if c["evidence_score"] >= args.min_score
        ]

    payload = json.dumps(rep, ensure_ascii=False,
                         indent=2 if args.pretty else None)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(payload)

    return 1 if rep.get("needs_update") else 0


if __name__ == "__main__":
    sys.exit(main())
