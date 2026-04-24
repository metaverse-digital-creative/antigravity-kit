#!/usr/bin/env python3
"""
validate.py — catalog validator / staleness monitor for exhibitions.json.

Design goals:
  - Always runnable (cron-safe) — even when catalog is missing, emit JSON
  - JSON-only output to stdout (pipeable; wrap for alerting)
  - Exit codes: 0 clean / 1 warnings only / 2 errors
  - Enforce the CROSS-REFERENCE-PROTOCOL weight policy:
      * Items in the provided JSON are tier-5 (highest trust)
      * Items with proven edges (verified domain, WHOIS date, editions,
        organizer, exhibitor count) score HIGHER than name-only entries
      * Weak items are flagged as `needs_enrichment`
  - Use stdlib only

Usage:
  python3 validate.py                              # auto-locate catalog
  python3 validate.py --data path/to/file.json     # explicit
  python3 validate.py --pretty                     # indent JSON
  python3 validate.py --quiet                      # no stdout (cron silent)
  python3 validate.py --max-age-days 180           # catalog file freshness

Exit codes:
  0 = all checks pass
  1 = warnings (stale entries, aging file, low evidence) — needs_update=True
  2 = errors (missing fields, duplicate slugs, malformed dates)

Reference: ops/CROSS-REFERENCE-PROTOCOL.md — the five-step + weighted-confidence rule.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Union

REQUIRED_FIELDS = ("slug", "name_en", "country", "editions")
VALID_ROLES = ("supply", "demand", "both")

# Edges (per CROSS-REFERENCE-PROTOCOL "proven evidence" list)
EDGE_FIELDS = (
    "domain",            # verifiable online
    "domain_registered", # WHOIS date, weight 5
    "organizer",         # named authority
    "exhibitor_count",   # measurable scale
    "venue",             # physical address
    "established",       # institutional age
    "relevance",         # explicit scoring was done
)

DEFAULT_MAX_AGE_DAYS = 180


# ── Issue helpers ────────────────────────────────────────────────────

def _issue(level: str, check: str, message: str, slug: Optional[str] = None) -> dict:
    d = {"level": level, "check": check, "message": message}
    if slug is not None:
        d["slug"] = slug
    return d


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Catalog loading ──────────────────────────────────────────────────

def _load_raw(path: Union[str, Path]) -> tuple[Optional[list], list[dict]]:
    """Return (items, [loading_issues])."""
    p = Path(path)
    issues: list[dict] = []
    if not p.exists():
        issues.append(_issue("error", "catalog_exists",
                             f"catalog not found: {p}"))
        return None, issues
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        issues.append(_issue("error", "catalog_readable",
                             f"cannot read catalog: {e}"))
        return None, issues
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        issues.append(_issue("error", "catalog_parseable",
                             f"malformed JSON: {e}"))
        return None, issues
    if not isinstance(data, dict) or "exhibitions" not in data:
        issues.append(_issue("error", "catalog_schema",
                             "missing top-level 'exhibitions' key"))
        return None, issues
    items = data["exhibitions"]
    if not isinstance(items, list):
        issues.append(_issue("error", "catalog_schema",
                             "'exhibitions' must be a list"))
        return None, issues
    return items, issues


# ── Individual checks ────────────────────────────────────────────────

def _check_required_fields(items: list[dict]) -> list[dict]:
    issues = []
    for it in items:
        for field in REQUIRED_FIELDS:
            v = it.get(field)
            if v in (None, "", [], {}):
                issues.append(_issue(
                    "error", "required_fields",
                    f"missing required field: {field}",
                    slug=it.get("slug", "?"),
                ))
    return issues


def _check_unique_slugs(items: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    for it in items:
        slug = it.get("slug")
        if not slug:
            continue
        seen[slug] = seen.get(slug, 0) + 1
    dupes = [s for s, n in seen.items() if n > 1]
    return [
        _issue("error", "unique_slugs", f"duplicate slug: {s!r}", slug=s)
        for s in dupes
    ]


def _check_edition_dates(items: list[dict]) -> list[dict]:
    issues: list[dict] = []
    for it in items:
        slug = it.get("slug", "?")
        for i, e in enumerate(it.get("editions") or []):
            try:
                s = datetime.strptime(e["start"], "%Y-%m-%d").date()
                en = datetime.strptime(e["end"], "%Y-%m-%d").date()
            except (KeyError, ValueError, TypeError):
                issues.append(_issue(
                    "error", "edition_dates",
                    f"edition[{i}] has malformed start/end",
                    slug=slug,
                ))
                continue
            if en < s:
                issues.append(_issue(
                    "error", "edition_dates",
                    f"edition[{i}] end ({en}) before start ({s})",
                    slug=slug,
                ))
    return issues


def _check_relevance_range(items: list[dict]) -> list[dict]:
    issues: list[dict] = []
    for it in items:
        rel = it.get("relevance") or {}
        for k, v in rel.items():
            if not isinstance(v, (int, float)) or v < 0 or v > 5:
                issues.append(_issue(
                    "error", "relevance_range",
                    f"relevance.{k}={v!r} not in 0-5",
                    slug=it.get("slug", "?"),
                ))
    return issues


def _check_future_edition(items: list[dict], today: Optional[date] = None) -> list[dict]:
    """Warn when an item has no future edition (stale)."""
    today = today or date.today()
    issues: list[dict] = []
    for it in items:
        slug = it.get("slug", "?")
        has_future = False
        for e in it.get("editions") or []:
            try:
                s = datetime.strptime(e["start"], "%Y-%m-%d").date()
            except (KeyError, ValueError, TypeError):
                continue
            if s >= today:
                has_future = True
                break
        if not has_future:
            issues.append(_issue(
                "warn", "future_edition",
                "no future edition; item may be stale",
                slug=slug,
            ))
    return issues


def _check_catalog_freshness(
    path: Union[str, Path],
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> tuple[float, list[dict]]:
    p = Path(path)
    if not p.exists():
        return (0.0, [])
    age_seconds = datetime.now().timestamp() - p.stat().st_mtime
    age_days = age_seconds / 86400
    issues = []
    if age_days > max_age_days:
        issues.append(_issue(
            "warn", "catalog_freshness",
            f"catalog file age {age_days:.1f}d > max {max_age_days}d",
        ))
    return (age_days, issues)


# ── Evidence scoring (per CROSS-REFERENCE-PROTOCOL) ──────────────────

def score_item_evidence(item: dict) -> dict:
    """
    Score one item 0-5 on evidence strength (proven edges).

    source_tier logic:
      5 — item is in the provided JSON (ALL items passed here start at 5)
      lower tiers would apply if we were fusing with external web/AI sources

    evidence_score:
      counts how many edge fields are populated with non-trivial values.
      domain +1, domain_registered +1, organizer +1, exhibitor_count +1,
      venue +0.5, established +0.5, relevance +1 (capped at 5)
    """
    edges_found: list[str] = []
    score = 0.0

    if item.get("domain"):
        edges_found.append("domain")
        score += 1
    if item.get("domain_registered"):
        edges_found.append("domain_registered")
        score += 1
    if item.get("organizer"):
        edges_found.append("organizer")
        score += 1
    if item.get("exhibitor_count"):
        edges_found.append("exhibitor_count")
        score += 1
    if item.get("venue"):
        edges_found.append("venue")
        score += 0.5
    if item.get("established"):
        edges_found.append("established")
        score += 0.5
    rel = item.get("relevance") or {}
    if rel:
        edges_found.append("relevance")
        score += 1

    score = min(5.0, score)
    return {
        "slug": item.get("slug", "?"),
        "source_tier": 5,                # provided JSON = tier 5 baseline
        "evidence_score": int(round(score)),
        "edges": edges_found,
    }


def _check_evidence_strength(items: list[dict], min_edges: int = 3) -> tuple[list[dict], list[dict]]:
    """
    Returns (per_item_scores, issues).
    Any item below `min_edges` is flagged as needs_enrichment (warn).
    """
    scores: list[dict] = []
    issues: list[dict] = []
    for it in items:
        s = score_item_evidence(it)
        scores.append(s)
        if len(s["edges"]) < min_edges:
            issues.append(_issue(
                "warn", "evidence_strength",
                (f"only {len(s['edges'])} edges present "
                 f"({','.join(s['edges']) or 'none'}); "
                 "needs enrichment (add domain/organizer/relevance)"),
                slug=s["slug"],
            ))
    return scores, issues


# ── Top-level validate() ─────────────────────────────────────────────

def validate(
    path: Optional[Union[str, Path]] = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    min_edges: int = 3,
) -> dict:
    """Run every check. Return a report dict (JSON-serializable)."""
    if path is None:
        path = os.environ.get("EXHIBITIONS_DATA", "./exhibitions.json")
    path_str = str(path)

    issues: list[dict] = []
    item_scores: list[dict] = []

    age_days, fresh_issues = _check_catalog_freshness(path, max_age_days)
    issues.extend(fresh_issues)

    items, load_issues = _load_raw(path)
    issues.extend(load_issues)

    if items is None:
        # Can't do anything more; return early with what we have
        has_errors = any(i["level"] == "error" for i in issues)
        has_warns = any(i["level"] == "warn" for i in issues)
        exit_code = 2 if has_errors else (1 if has_warns else 0)
        return {
            "ok": exit_code == 0,
            "needs_update": exit_code != 0,
            "checked_at": _now_utc_iso(),
            "catalog": path_str,
            "catalog_age_days": round(age_days, 2),
            "item_count": 0,
            "issues": issues,
            "item_scores": [],
            "exit_code": exit_code,
        }

    # Schema / data checks
    issues.extend(_check_required_fields(items))
    issues.extend(_check_unique_slugs(items))
    issues.extend(_check_edition_dates(items))
    issues.extend(_check_relevance_range(items))

    # Staleness + evidence
    issues.extend(_check_future_edition(items))
    ev_scores, ev_issues = _check_evidence_strength(items, min_edges=min_edges)
    item_scores = ev_scores
    issues.extend(ev_issues)

    has_errors = any(i["level"] == "error" for i in issues)
    has_warns = any(i["level"] == "warn" for i in issues)
    exit_code = 2 if has_errors else (1 if has_warns else 0)

    return {
        "ok": exit_code == 0,
        "needs_update": exit_code != 0,
        "checked_at": _now_utc_iso(),
        "catalog": path_str,
        "catalog_age_days": round(age_days, 2),
        "item_count": len(items),
        "stats": {
            "items_with_future_edition": sum(
                1 for it in items
                if any(
                    (lambda s: s >= date.today())(
                        datetime.strptime(e["start"], "%Y-%m-%d").date()
                    )
                    for e in (it.get("editions") or [])
                    if "start" in e
                    and isinstance(e["start"], str)
                    and len(e["start"]) == 10
                )
            ),
            "items_with_full_edges": sum(
                1 for s in item_scores if len(s["edges"]) >= min_edges
            ),
        },
        "issues": issues,
        "item_scores": item_scores,
        "exit_code": exit_code,
    }


# ── CLI ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="validate",
        description="Always-on JSON validator for exhibitions.json catalog",
    )
    p.add_argument("--data", default=None,
                   help="path to exhibitions.json (or $EXHIBITIONS_DATA)")
    p.add_argument("--pretty", action="store_true",
                   help="indent JSON output")
    p.add_argument("--quiet", action="store_true",
                   help="suppress stdout — rely on exit code")
    p.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                   help="warn if catalog file older than N days")
    p.add_argument("--min-edges", type=int, default=3,
                   help="items with fewer proven edges are flagged")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report = validate(
        path=args.data,
        max_age_days=args.max_age_days,
        min_edges=args.min_edges,
    )
    if not args.quiet:
        if args.pretty:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(report, ensure_ascii=False))
    return int(report.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
