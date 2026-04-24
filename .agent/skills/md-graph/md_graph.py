#!/usr/bin/env python3
"""
md_graph.py — cross-file link-graph verifier for intel/ directories.

Where md-check validates links WITHIN one doc, md-graph validates the
GRAPH ACROSS many docs:

  - Entity-aware: each .md file's H1 is the "entity name"
  - Bare mentions: plaintext "Prabha Industries" in doc B when Prabha
    has its own file should be linked → flag as missing reference
  - Bi-directional: if A has [B](b.md), B should have [A](a.md)
  - Tri-directional (triangles): sets of 3 mutually-linked entities;
    incomplete (2/3 pairs) flagged
  - Orphans: files with zero inbound links from anywhere in the corpus

Output: JSON with evidence_score (0-5). Weight policy per
ops/CROSS-REFERENCE-PROTOCOL.md: proven edges (bi-dir + complete triangle
+ no orphans) > no-edge claims.

Usage:
    python3 md_graph.py --root intel/ --pretty
    python3 md_graph.py --root intel/markets/india/
    python3 md_graph.py --root intel/ --out graph.json

Exit codes:
    0 = clean
    1 = warnings (one-way links, bare mentions, orphans, incomplete triangles)
    2 = --root not found

Stdlib only.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

# ── Regexes ──────────────────────────────────────────────────────────

H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
LINK_RE = re.compile(r"\[([^\]]+?)\]\(([^)\s]+?)\)")
# Trailing emoji / markers to strip from H1 ("KRYFS 🔴 HIGHEST PRIORITY")
EMOJI_TAIL_RE = re.compile(
    r"\s*[🔴🟡🟢📡⬜🎯⚠️✅🇮🇳🇨🇳🇹🇼🇺🇸🇩🇪🇯🇵🇰🇷🇹🇷🇻🇳🇮🇩🇲🇽]\s*.*$"
)

# Generic / ambiguous H1 titles that should NOT be treated as unique entities.
# (An entity name must be specific enough to be meaningfully linkable.)
GENERIC_H1 = {
    "README", "readme", "Readme",
    "Index", "INDEX", "index",
    "Notes", "notes",
    "TODO", "Todo",
    "Changelog", "CHANGELOG",
}


# ── Heading extraction ───────────────────────────────────────────────

def extract_h1(text: str) -> Optional[str]:
    m = H1_RE.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    # Strip trailing emoji + marker phrases
    name = EMOJI_TAIL_RE.sub("", name).strip()
    return name or None


def is_linkable_entity(name: str) -> bool:
    """
    An H1 is a 'linkable entity' only if specific enough to uniquely
    identify something. Generic titles like 'README' are too ambiguous
    to enforce bi-directional links on.
    """
    if not name or name in GENERIC_H1:
        return False
    # Require at least 3 characters OR a space/capital letter
    return len(name) >= 4 or " " in name


# ── Corpus scanning ──────────────────────────────────────────────────

def _rel(p: Path, root: Path) -> str:
    return str(p.relative_to(root))


def build_entity_index(root: Union[str, Path]) -> dict[str, str]:
    """Map entity name (from H1) → relative file path."""
    root = Path(root)
    idx: dict[str, str] = {}
    for p in root.rglob("*.md"):
        # Skip node_modules, .git, etc.
        if any(part in {".git", "node_modules", "__pycache__"} for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h1 = extract_h1(text)
        if h1 and is_linkable_entity(h1):
            idx[h1] = _rel(p, root)
    return idx


def extract_links(text: str) -> list[tuple[str, str]]:
    """Return [(link_text, url), ...]."""
    return [(m.group(1), m.group(2)) for m in LINK_RE.finditer(text)]


# ── Bare-mention detection ───────────────────────────────────────────

def find_bare_mentions(
    text: str,
    known_entities: list[str],
) -> list[str]:
    """Return entity names that appear as plaintext but have no link in this text."""
    # Remove link bodies entirely before scanning — we don't want to flag
    # a name that is ALREADY inside a [link text](url).
    stripped = LINK_RE.sub(" ", text)
    out = []
    for name in known_entities:
        # Word-boundary-aware search for the exact name
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])")
        if pattern.search(stripped):
            out.append(name)
    return out


# ── Graph build ───────────────────────────────────────────────────────

def build_graph(root: Union[str, Path]) -> dict[str, dict]:
    """
    Build {rel_path: {title, links_out}} across every .md file under root.
    `links_out` contains RELATIVE PATHS (normalized), not arbitrary URLs.
    External https:// links are dropped (not part of the internal graph).
    """
    root = Path(root).resolve()
    graph: dict[str, dict] = {}

    md_files = []
    for p in root.rglob("*.md"):
        if any(part in {".git", "node_modules", "__pycache__"} for part in p.parts):
            continue
        md_files.append(p)

    for p in md_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = extract_h1(text) or p.stem
        rel_path = _rel(p, root)

        links_out: list[str] = []
        for _, url in extract_links(text):
            if url.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                continue
            # Resolve relative to this file's parent
            target = (p.parent / url.split("#", 1)[0]).resolve()
            # Only count if target is still within the corpus
            try:
                target_rel = target.relative_to(root)
            except ValueError:
                continue
            links_out.append(str(target_rel))

        graph[rel_path] = {
            "title": title,
            "links_out": links_out,
        }

    return graph


def _inbound_map(graph: dict[str, dict]) -> dict[str, set[str]]:
    """For each file, the set of files pointing AT it."""
    inbound: dict[str, set[str]] = {f: set() for f in graph}
    for src, meta in graph.items():
        for tgt in meta["links_out"]:
            if tgt in graph:
                inbound.setdefault(tgt, set()).add(src)
    return inbound


# ── Relationship detection ───────────────────────────────────────────

def find_one_way_pairs(graph: dict[str, dict]) -> list[dict]:
    """Pairs where A links to B but B does not link to A."""
    pairs = []
    seen = set()
    for src, meta in graph.items():
        for tgt in meta["links_out"]:
            if tgt not in graph:
                continue
            if src in graph[tgt]["links_out"]:
                continue  # bi-directional
            key = (src, tgt)
            if key in seen:
                continue
            seen.add(key)
            pairs.append({
                "from": src,
                "to": tgt,
                "from_title": meta["title"],
                "to_title": graph[tgt]["title"],
            })
    return pairs


def find_triangles(graph: dict[str, dict]) -> list[dict]:
    """
    For every triple (A, B, C) where at least 2 of 3 possible pairs have
    SOME link (in either direction), report triangle completeness.
    complete=True iff all 3 pairs are bi-directional.
    """
    files = list(graph.keys())
    nodes_with_edges = set()
    for src, meta in graph.items():
        for tgt in meta["links_out"]:
            if tgt in graph:
                nodes_with_edges.add(src)
                nodes_with_edges.add(tgt)
    candidates = sorted(nodes_with_edges)

    results = []
    for a, b, c in itertools.combinations(candidates, 3):
        def bidir(x, y):
            return y in graph[x]["links_out"] and x in graph[y]["links_out"]
        def connected(x, y):
            return y in graph[x]["links_out"] or x in graph[y]["links_out"]
        pair_count = sum(1 for x, y in ((a, b), (b, c), (a, c)) if connected(x, y))
        if pair_count < 2:
            continue
        bidir_count = sum(1 for x, y in ((a, b), (b, c), (a, c)) if bidir(x, y))
        results.append({
            "nodes": [a, b, c],
            "titles": [graph[a]["title"], graph[b]["title"], graph[c]["title"]],
            "pair_connections": pair_count,
            "bidir_pairs": bidir_count,
            "complete": bidir_count == 3,
        })
    return results


def find_orphans(graph: dict[str, dict]) -> list[str]:
    inbound = _inbound_map(graph)
    return sorted(f for f, refs in inbound.items() if not refs)


def find_bare_mentions_across(
    root: Union[str, Path],
    graph: dict[str, dict],
) -> list[dict]:
    """Find entities mentioned by plaintext in OTHER files (not linked)."""
    root = Path(root).resolve()
    # Reverse: title → file (only linkable entities)
    entities = {
        meta["title"]: f for f, meta in graph.items()
        if is_linkable_entity(meta["title"])
    }
    names = list(entities.keys())

    out = []
    for rel_path, meta in graph.items():
        abs_p = root / rel_path
        try:
            text = abs_p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Remove this file's own H1 line so we don't flag self-reference
        text_wo_own = re.sub(
            rf"^#\s+{re.escape(meta['title'])}.*$",
            "", text, count=1, flags=re.MULTILINE,
        )
        for name in names:
            if entities[name] == rel_path:
                continue  # self
            bare_hits = find_bare_mentions(text_wo_own, [name])
            if bare_hits:
                out.append({
                    "file": rel_path,
                    "mentions_entity": name,
                    "entity_at": entities[name],
                })
    return out


# ── Top-level verify ─────────────────────────────────────────────────

def verify_graph(root: Union[str, Path]) -> dict:
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"root not found: {root}")

    graph = build_graph(root)
    one_way = find_one_way_pairs(graph)
    triangles = find_triangles(graph)
    orphans = find_orphans(graph)
    bare = find_bare_mentions_across(root, graph)

    total = max(1, len(graph))
    orphan_ratio = len(orphans) / total
    one_way_pairs = len(one_way)

    # evidence_score: bi-dir coverage + low orphan rate + no bare mentions
    score = 5
    if orphan_ratio > 0.5:
        score -= 2
    elif orphan_ratio > 0.2:
        score -= 1
    if one_way_pairs > 10:
        score -= 2
    elif one_way_pairs > 0:
        score -= 1
    if len(bare) > 20:
        score -= 2
    elif len(bare) > 0:
        score -= 1
    score = max(0, min(5, score))

    needs_update = bool(one_way or orphans or bare or
                        any(not t["complete"] for t in triangles))

    return {
        "root": str(root.resolve()),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": graph,
        "bare_mentions": bare,
        "one_way_pairs": one_way,
        "triangles": triangles,
        "orphans": orphans,
        "stats": {
            "file_count": len(graph),
            "orphan_count": len(orphans),
            "one_way_count": len(one_way),
            "bare_mention_count": len(bare),
            "complete_triangle_count": sum(1 for t in triangles if t["complete"]),
            "incomplete_triangle_count": sum(1 for t in triangles if not t["complete"]),
        },
        "evidence_score": score,
        "source_tier": 5,
        "ok": not needs_update,
        "needs_update": needs_update,
    }


# ── CLI ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="md_graph",
        description="Cross-file link-graph verifier for intel/ directories",
    )
    p.add_argument("--root", required=True, help="directory to scan")
    p.add_argument("--pretty", action="store_true", help="indent JSON")
    p.add_argument("--out", default=None, help="write report to file")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rep = verify_graph(args.root)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e), "ok": False}))
        return 2

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
