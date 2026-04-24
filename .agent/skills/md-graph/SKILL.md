---
name: md-graph
description: Cross-file link-graph verifier for intel directories.
  Detects bare mentions (entity named as plaintext where it should be
  linked), one-way references (A links B but B doesn't link back),
  tri-directional completeness (triangles), and orphan files. Use
  PROACTIVELY after any intel restructure or when user says "check
  bi-directional" / "find orphans" / "verify cross-refs" / "link test".
---

You are the md-graph skill. You build a graph across every markdown
file in a directory, treat each file's H1 as the entity name, and
verify the reference graph is consistent.

## What it catches

- **Bare mentions** — plaintext "Prabha Industries" in doc B when Prabha
  has its own file → should be a link, not a string
- **One-way links** — A has `[B](b.md)` but B has no link back to A
- **Incomplete triangles** — 2/3 bi-directional pairs among three
  entities (e.g., A↔B and A↔C, but B and C don't link each other)
- **Orphans** — files that no one links to from anywhere in the corpus

## Use this skill when

- After moving/renaming files (internal links break silently)
- Before publishing a knowledge base externally
- When the user says "are all my docs cross-linked"
- Proactively to catch bare company/entity name drift

## Do not use this skill when

- You only need to verify ONE file's links (use `md-check` instead)
- You need per-company OSINT scoring (use `osint-verify`)

## Core command

```bash
python3 md_graph.py --root intel/ --pretty
python3 md_graph.py --root intel/markets/india/ --out graph.json
```

Exit codes:
- `0` clean — all bi-dir, no orphans, no bare mentions
- `1` warnings — one-way links / orphans / bare mentions / incomplete triangles
- `2` root not found

## Weight policy

Per `ops/CROSS-REFERENCE-PROTOCOL.md`:
- Provided markdown = **source_tier 5**
- Proven edges (bi-dir links + complete triangles + zero orphans) → evidence_score 5
- No-edge claims (plaintext mentions) → flagged `needs_update`

## Output shape

```json
{
  "root": "/abs/intel",
  "checked_at": "2026-04-24T...",
  "files": {
    "INDEX.md": {"title": "Intel Index", "links_out": ["markets/india/README.md", ...]}
  },
  "bare_mentions": [
    {"file": "INDEX.md", "mentions_entity": "Prabha Industries", "entity_at": "markets/india/.../prabha.md"}
  ],
  "one_way_pairs": [
    {"from": "INDEX.md", "to": "STRATEGY.md", "from_title": "Intel Index", "to_title": "Electricity Moat"}
  ],
  "triangles": [
    {"nodes": ["a.md", "b.md", "c.md"], "pair_connections": 2, "bidir_pairs": 1, "complete": false}
  ],
  "orphans": ["markets/india/pipeline/cold-outreach-template.md"],
  "stats": {
    "file_count": 46,
    "orphan_count": 19,
    "one_way_count": 37,
    "bare_mention_count": 13,
    "complete_triangle_count": 4,
    "incomplete_triangle_count": 159
  },
  "evidence_score": 1,
  "source_tier": 5,
  "ok": false,
  "needs_update": true
}
```

## Heading handling

- Extracts first `# H1` per file as the entity name
- Strips trailing emoji markers (🔴, 🎯, country flags, etc.)
- Filters generic names (`README`, `Index`, `Notes`, `TODO`) — too ambiguous
- Requires name length ≥4 OR at least one space (single letters not linkable)

## Relationship to sibling skills

| Skill | Granularity | Answers |
|---|---|---|
| `md-check` | Single file | Are MY links/emails/phones valid? |
| `md-graph` | Directory | Are my REFERENCES cross-linked, bi-dir, complete? |
| `osint-verify` | Single dossier | Is each COMPANY's URL + email live? |
| `exhibition-explorer/validate.py` | Single JSON | Is my catalog schema OK with proven edges? |
| `intel-observer` | Directory over time | What CHANGED since last scan? |

## Tests

24 unit tests. Stdlib only. Run with `python3 -m unittest test_md_graph -v`.

Real-world output on mechas-os `intel/`: evidence 1/5, 19 orphans (41%),
37 one-way pairs, 13 bare mentions — captures the Taiwan restructure
fallout honestly.
