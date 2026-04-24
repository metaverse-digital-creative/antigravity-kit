---
name: exhibition-explorer
description: Explore, filter, score, and validate a JSON catalog of
  industry exhibitions (trade shows). Use PROACTIVELY when the user
  mentions exhibitions, trade shows, DMC, IMTEX, ELECRAMA, DMI,
  exhibition scouting, or says "which show should I attend".
---

You are the exhibition-explorer skill. You drive a stdlib-only Python
tool that consumes a JSON catalog of exhibitions and supports:

- `list` — every exhibition
- `show <slug>` — one entry with editions + relevance scores
- `filter --country IN --role demand --after 2026-12-31 --min-score 4`
- `score --goal supply|demand|competitor_intel` — ranked
- `ics --out cal.ics` — RFC 5545 iCalendar export
- `--json` output mode on every command

## Use this skill when

- User asks "which exhibition should I attend"
- User wants to score or filter trade shows by market / date / role
- User wants to export upcoming editions to a calendar
- User asks about ByteTCM's exhibition pipeline (DMC, IMTEX, DMI, ELECRAMA, Aero India, etc.)

## Do not use this skill when

- The data lives somewhere OTHER than an exhibitions.json catalog
- The user is asking for general web search (use search instead)

## Companion tool: validate.py

Always-runnable catalog linter. Emits JSON report. Exit code:
`0` clean · `1` warnings (stale entries, old file, weak evidence) · `2` errors
(missing fields, duplicate slugs, malformed dates).

Implements the weight policy from `ops/CROSS-REFERENCE-PROTOCOL.md`:
provided JSON = tier 5, items with proven edges (domain + WHOIS +
organizer + editions + relevance) score HIGHER than no-edge claims.
Weak items are flagged as `needs_enrichment`.

## Data location

Default search order:
1. `--data` CLI flag
2. `$EXHIBITIONS_DATA` env var
3. `mechas-os/intel/exhibitions.json` (when run from inside antigravity-kit)
4. `./intel/exhibitions.json` (cwd)
5. `./exhibitions.json`

## Instructions

1. Locate the catalog (above order).
2. Choose the command matching the user's intent.
3. Prefer `--json` when piping into another agent or scoring tool.
4. For exhibition-scouting workflows, start with `score --goal demand`
   (or supply / competitor_intel) then `show <slug>` for the top entry.
5. Run `validate.py` before any outreach campaign — it flags low-evidence
   entries that shouldn't be used as-is.

## Tests

- `test_explorer.py` — 34 unit + 7 E2E (gated behind `EXPLORER_E2E=1`)
- `test_validate.py` — 27 unit + 3 subprocess

All tests stdlib; zero pip deps.
