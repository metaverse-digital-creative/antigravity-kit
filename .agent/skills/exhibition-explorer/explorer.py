#!/usr/bin/env python3
"""
explorer.py — Exhibition catalog explorer for mechas-os style recon.

Loads a JSON catalog of exhibitions and lets you list / filter / score / export.
Designed to be driven by the antigravity-kit /explore workflow, but usable
standalone as a Python module or CLI.

Data schema (exhibitions.json):
{
  "exhibitions": [
    {
      "slug":             "dmc-shanghai",           # required, stable id
      "name_en":          "Die & Mould China",      # required
      "name_cn":          "中国国际模具...",         # optional
      "country":          "CN",                     # ISO-3166-1 alpha-2
      "city":             "Shanghai",
      "venue":            "NECC",
      "domain":           "dmcexpo.com",
      "organizer":        "CDMIA",
      "frequency":        "biennial",               # annual / biennial / triennial
      "established":      1980,
      "exhibitor_count":  2000,
      "editions": [                                 # one entry per known edition
        {"start": "2026-07-01", "end": "2026-07-04", "year": 2026}
      ],
      "relevance": {                                # 0-5 each
        "supply": 5,
        "demand": 1,
        "competitor_intel": 2
      },
      "role":   "supply",                           # supply | demand | both
      "action": "attend",                           # attend | exhibit | scout | watch | recon
      "notes":  "free-form..."
    }
  ]
}

Catalog discovery order:
  1. explicit --data PATH
  2. EXHIBITIONS_DATA env var
  3. ../mechas-os/intel/exhibitions.json (relative to this script)
  4. ./intel/exhibitions.json (cwd)

Usage:
  explorer list                              # one line per exhibition
  explorer show dmc-shanghai                 # detail for one
  explorer filter --country IN --role demand --after 2026-12-31
  explorer score --goal supply               # ranked by relevance.supply
  explorer ics --out calendar.ics            # RFC 5545 iCalendar export
  explorer --json list                       # JSON on every command

Stdlib only.  No pip deps.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Union

LOG = logging.getLogger("explorer")

VALID_GOALS: tuple[str, ...] = ("supply", "demand", "competitor_intel")
VALID_ROLES: tuple[str, ...] = ("supply", "demand", "both")


# ── Catalog I/O ──────────────────────────────────────────────────────

def _default_catalog_paths() -> list[Path]:
    here = Path(__file__).resolve()
    return [
        # antigravity-kit is a sibling of mechas-os intel dir usually;
        # search both the parent-of-parent repo and the cwd.
        here.parent.parent.parent.parent.parent / "mechas-os" / "intel" / "exhibitions.json",
        here.parent.parent.parent.parent / "intel" / "exhibitions.json",
        Path.cwd() / "intel" / "exhibitions.json",
        Path.cwd() / "exhibitions.json",
    ]


def _resolve_catalog_path(explicit: Optional[Union[str, Path]] = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("EXHIBITIONS_DATA")
    if env:
        return Path(env)
    for p in _default_catalog_paths():
        if p.exists():
            return p
    # Return the first default; load_catalog will raise FileNotFoundError
    return _default_catalog_paths()[0]


def load_catalog(path: Optional[Union[str, Path]] = None) -> list[dict]:
    """Load exhibition catalog from JSON. Returns the 'exhibitions' list.

    Raises FileNotFoundError if file missing, JSONDecodeError if malformed,
    KeyError if 'exhibitions' key missing.
    """
    p = _resolve_catalog_path(path)
    if not p.exists():
        raise FileNotFoundError(f"catalog not found: {p}")
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)  # raises JSONDecodeError
    if "exhibitions" not in data:
        raise KeyError("'exhibitions' key missing from catalog")
    items = data["exhibitions"]
    if not isinstance(items, list):
        raise TypeError("'exhibitions' must be a list")
    return items


# ── Filter ────────────────────────────────────────────────────────────

def _next_edition(item: dict, today: Optional[date] = None) -> Optional[dict]:
    today = today or date.today()
    future = []
    for e in item.get("editions") or []:
        try:
            start = datetime.strptime(e["start"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if start >= today:
            future.append((start, e))
    if not future:
        # fallback to latest known
        past = []
        for e in item.get("editions") or []:
            try:
                start = datetime.strptime(e["start"], "%Y-%m-%d").date()
                past.append((start, e))
            except (KeyError, ValueError):
                continue
        if past:
            return max(past, key=lambda x: x[0])[1]
        return None
    return min(future, key=lambda x: x[0])[1]


def filter_items(
    items: Iterable[dict],
    country: Optional[str] = None,
    role: Optional[str] = None,
    sector: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    min_score: Optional[int] = None,
    goal: Optional[str] = None,
) -> list[dict]:
    """Apply filters. Returns new list."""
    out: list[dict] = []
    after_d = datetime.strptime(after, "%Y-%m-%d").date() if after else None
    before_d = datetime.strptime(before, "%Y-%m-%d").date() if before else None

    for it in items:
        if country and it.get("country") != country:
            continue
        if role and it.get("role") not in {role, "both"}:
            continue
        if sector and sector not in (it.get("sectors") or []):
            continue

        # Edition date constraints — must have at least one edition in range.
        if after_d or before_d:
            hit = False
            for e in it.get("editions") or []:
                try:
                    s = datetime.strptime(e["start"], "%Y-%m-%d").date()
                except (KeyError, ValueError):
                    continue
                if after_d and s < after_d:
                    continue
                if before_d and s > before_d:
                    continue
                hit = True
                break
            if not hit:
                continue

        if min_score is not None:
            g = goal or "demand"
            score = int(((it.get("relevance") or {}).get(g, 0)))
            if score < min_score:
                continue

        out.append(it)
    return out


# ── Scoring ───────────────────────────────────────────────────────────

def rank(items: Iterable[dict], goal: str) -> list[dict]:
    """Return items sorted descending by relevance[goal]. Raises on bad goal."""
    if goal not in VALID_GOALS:
        raise ValueError(f"goal must be one of {VALID_GOALS}, got {goal!r}")
    return sorted(
        list(items),
        key=lambda it: int(((it.get("relevance") or {}).get(goal, 0))),
        reverse=True,
    )


# ── Lookup ────────────────────────────────────────────────────────────

def show(items: Iterable[dict], slug: str) -> Optional[dict]:
    """Return the exhibition with matching slug, or None."""
    for it in items:
        if it.get("slug") == slug:
            return it
    return None


# ── ICS export (RFC 5545) ─────────────────────────────────────────────

def _ics_date(s: str) -> str:
    """YYYY-MM-DD → YYYYMMDD."""
    return s.replace("-", "")


def _ics_escape(s: str) -> str:
    """RFC 5545 text escape: backslash, comma, semicolon, newline."""
    return (
        s.replace("\\", "\\\\")
         .replace("\n", "\\n")
         .replace(",", "\\,")
         .replace(";", "\\;")
    )


def _ics_event(item: dict, edition: dict) -> list[str]:
    try:
        start = datetime.strptime(edition["start"], "%Y-%m-%d").date()
        end = datetime.strptime(edition["end"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return []
    # iCalendar DATE value: DTEND is exclusive → end + 1 day
    dtend_exclusive = end + timedelta(days=1)
    uid = f"{item['slug']}-{start.isoformat()}@antigravity-exhibitions"
    name = item.get("name_en") or item.get("slug", "")
    loc_parts = [item.get("venue"), item.get("city"), item.get("country")]
    location = ", ".join(p for p in loc_parts if p)
    action = item.get("action", "")
    notes = item.get("notes", "")
    description = f"Action: {action}. Organizer: {item.get('organizer', '?')}."
    if notes:
        description = f"{description} {notes}"
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;VALUE=DATE:{_ics_date(start.isoformat())}",
        f"DTEND;VALUE=DATE:{_ics_date(dtend_exclusive.isoformat())}",
        f"SUMMARY:{_ics_escape(name)}",
        f"LOCATION:{_ics_escape(location)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        "END:VEVENT",
    ]


def to_ics(items: Iterable[dict]) -> str:
    """Render items as an iCalendar document. Emits one VEVENT per edition."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//antigravity-kit//exhibition-explorer//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for it in items:
        for ed in it.get("editions") or []:
            lines.extend(_ics_event(it, ed))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ── Output helpers ────────────────────────────────────────────────────

def _emit(payload, output: str) -> None:
    if output == "json":
        print(json.dumps(payload, ensure_ascii=False))
        return
    if isinstance(payload, list):
        _render_table(payload)
    elif isinstance(payload, dict):
        _render_detail(payload)
    else:
        print(payload)


def _render_table(items: list[dict]) -> None:
    if not items:
        print("(no exhibitions match)")
        return
    header = f"  {'slug':<28} {'country':<4} {'city':<14} {'next':<12} {'role':<8} {'action':<8} name"
    print(header)
    print(f"  {'-'*28} {'-'*4} {'-'*14} {'-'*12} {'-'*8} {'-'*8} {'-'*30}")
    for it in items:
        nxt = _next_edition(it) or {}
        next_start = nxt.get("start", "—")
        name = it.get("name_en") or ""
        print(f"  {it.get('slug', '')[:28]:<28} "
              f"{(it.get('country') or '')[:4]:<4} "
              f"{(it.get('city') or '')[:14]:<14} "
              f"{str(next_start)[:12]:<12} "
              f"{(it.get('role') or '')[:8]:<8} "
              f"{(it.get('action') or '')[:8]:<8} "
              f"{name[:40]}")


def _render_detail(item: dict) -> None:
    if "error" in item:
        print(f"ERROR: {item['error']}")
        return
    print(f"\n═══ {item.get('name_en', '')} ({item.get('slug', '')})")
    for k in ("country", "city", "venue", "domain", "organizer",
              "frequency", "established", "exhibitor_count", "role", "action"):
        v = item.get(k)
        if v is not None:
            print(f"  {k:<16} {v}")
    rel = item.get("relevance") or {}
    if rel:
        print(f"  relevance      {', '.join(f'{k}={v}' for k, v in rel.items())}")
    editions = item.get("editions") or []
    if editions:
        print(f"  editions:")
        for e in editions:
            print(f"    {e.get('start', '?')} → {e.get('end', '?')}")
    if item.get("notes"):
        print(f"  notes: {item['notes']}")


# ── Commands ──────────────────────────────────────────────────────────

def cmd_list(data: Optional[str] = None, output: str = "text") -> int:
    items = load_catalog(data)
    _emit(items, output)
    return 0


def cmd_show(data: Optional[str], slug: str, output: str = "text") -> int:
    items = load_catalog(data)
    entry = show(items, slug)
    if entry is None:
        _emit({"error": f"no exhibition with slug={slug!r}"}, output)
        return 1
    _emit(entry, output)
    return 0


def cmd_filter(
    data: Optional[str],
    country: Optional[str] = None,
    role: Optional[str] = None,
    sector: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    min_score: Optional[int] = None,
    goal: Optional[str] = None,
    output: str = "text",
) -> int:
    items = load_catalog(data)
    result = filter_items(
        items,
        country=country, role=role, sector=sector,
        after=after, before=before,
        min_score=min_score, goal=goal,
    )
    _emit(result, output)
    return 0


def cmd_score(data: Optional[str], goal: str, output: str = "text") -> int:
    items = load_catalog(data)
    ranked = rank(items, goal)
    _emit(ranked, output)
    return 0


def cmd_ics(data: Optional[str], out_path: Optional[str] = None) -> int:
    items = load_catalog(data)
    ics = to_ics(items)
    if out_path:
        Path(out_path).write_text(ics, encoding="utf-8")
        print(f"wrote {len(items)} exhibitions → {out_path}")
    else:
        sys.stdout.write(ics)
    return 0


# ── CLI ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="explorer",
        description="Exhibition catalog explorer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON (every subcommand)")
    p.add_argument("--data", default=None,
                   help="path to exhibitions.json (or $EXHIBITIONS_DATA)")

    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list all exhibitions")

    show_p = sub.add_parser("show", help="detail for one exhibition")
    show_p.add_argument("slug")

    filt = sub.add_parser("filter", help="filter by country / role / date / score")
    filt.add_argument("--country")
    filt.add_argument("--role", choices=list(VALID_ROLES))
    filt.add_argument("--sector")
    filt.add_argument("--after", help="YYYY-MM-DD")
    filt.add_argument("--before", help="YYYY-MM-DD")
    filt.add_argument("--min-score", type=int, dest="min_score")
    filt.add_argument("--goal", choices=list(VALID_GOALS), default="demand")

    sc = sub.add_parser("score", help="rank by relevance[goal]")
    sc.add_argument("--goal", choices=list(VALID_GOALS), required=True)

    ics = sub.add_parser("ics", help="emit iCalendar (.ics) for every edition")
    ics.add_argument("--out", help="write to file; default stdout")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)-7s %(message)s")

    output = "json" if args.json else "text"

    try:
        if args.cmd == "list":
            return cmd_list(args.data, output=output)
        if args.cmd == "show":
            return cmd_show(args.data, args.slug, output=output)
        if args.cmd == "filter":
            return cmd_filter(
                args.data,
                country=args.country, role=args.role, sector=args.sector,
                after=args.after, before=args.before,
                min_score=args.min_score, goal=args.goal,
                output=output,
            )
        if args.cmd == "score":
            return cmd_score(args.data, args.goal, output=output)
        if args.cmd == "ics":
            return cmd_ics(args.data, args.out)
        # Default: list
        return cmd_list(args.data, output=output)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"error: malformed catalog: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
