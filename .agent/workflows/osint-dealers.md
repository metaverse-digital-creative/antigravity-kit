---
description: Full dealer OSINT scan — parse dossier, verify every URL/email/phone via live signals, create stubs for unlinked candidates, produce dealers/INDEX.md, re-run graph to measure delta.
---

# /osint-dealers — Dealer OSINT pipeline

$ARGUMENTS

---

## Purpose

Run the full intel-recon pipeline on a dealer scan markdown dossier and produce:

1. Per-company evidence scores
2. A consolidated `dealers/INDEX.md` hub file
3. Stub dossiers for any candidate without their own file
4. An updated parent README pointing at the INDEX
5. Before/after `md-graph` metrics proving the graph tightened

Delegates to the **`dealer-osint-researcher`** agent for all the heavy lifting.

---

## When to trigger

- User says: `/osint-dealers`, `run dealer OSINT`, `verify dealer contacts`, `scan dealers in <country>`, `make dealer-scan actionable`
- After a new dealer scan lands under `intel/markets/<country>/dealers/osint-dealer-scan.md`
- After a restructure that broke internal dealer links

---

## Behavior

When `/osint-dealers` fires:

1. **Locate the input scan**
   - Default: `intel/markets/india/dealers/osint-dealer-scan.md`
   - Override with argument: `/osint-dealers intel/markets/turkey/dealers/osint-dealer-scan.md`

2. **Invoke dealer-osint-researcher agent** with the scan path

3. **Agent runs the tool chain (stdlib only):**
   - `osint-verify/osint_verify.py --dossier <scan>` → company-level scores
   - `md-check/md_check.py --file <scan> --country <CC>` → link/email/phone validation
   - `md-graph/md_graph.py --root <dealers-dir>` → one-way pairs, orphans

4. **Agent produces:**
   - New or updated `dealers/INDEX.md` hub file
   - New stub dossiers where needed
   - Updated parent README §Dealer Expansion section
   - Summary report (delta in orphans / one-way / evidence_score)

5. **Agent commits** with message:
   `intel: /osint-dealers run — <N> candidates scored, <M> stubs created, evidence <old>/5 → <new>/5`

---

## Scoring rubric (applied per dealer)

| Edges proven | Score | Meaning |
|---|---|---|
| DNS + HTTP + MX + phone format + CIN | 5 | Ready for outreach |
| DNS + HTTP + MX | 4 | Ready, pending formal identity check |
| DNS + HTTP + partial contact | 3 | Usable, flag for enrichment |
| DNS only | 2 | Needs enrichment before outreach |
| Name only | 0-1 | Must not be used for outreach without more data |

Weight policy per `ops/CROSS-REFERENCE-PROTOCOL.md`:
provided dossier data = **tier 5**. Live-verified edges > no-edge plaintext claims.

---

## Arguments

- **(no args)** — run on default India dealer scan
- **`<path-to-scan.md>`** — run on a specific scan file
- **`--dry-run`** — print what would change, don't write
- **`--country <CC>`** — pass country code (IN / US / TW / CN) to `md-check`

---

## Expected output shape

```
🔍 dealer-osint-researcher started
   input: intel/markets/india/dealers/osint-dealer-scan.md

📊 Pre-scan metrics
   md-graph(dealers/): 10 files, 5 orphans, 3 one-way, evidence 2/5

🌐 osint-verify (live signals per company)
   10 companies extracted
   ✅ 8 passing (evidence ≥ 3)
   ⚠️  2 weak (JJ self-ref; Machintra SSL)

🔗 md-check (links / emails / phones)
   internal 8/8, external 9/10, MX 7/7, phones 10/10
   evidence 5/5

📦 Stubs created
   ✓ avi-oilless.md (already existed)
   ✓ imdc-faridabad.md
   ✓ datum-tools.md
   ✓ practic-industries.md
   ✓ stiack-engineering.md
   ✓ machintra-chennai.md

📝 dealers/INDEX.md produced
   - Active dealers table (2 rows)
   - Candidate dealers table (8 rows) with per-entity scores
   - Completeness gap matrix (6 gap types)
   - Priority Action quick-reference

📈 Post-repair metrics
   md-graph(dealers/): 11 files, 0 orphans, 1 one-way, evidence 4/5
   Δ: orphans -5, one-way -2, evidence +2

Done. Run /ship to commit + push.
```

---

## Pairs with

- Skill: [`osint-verify`](../skills/osint-verify/SKILL.md)
- Skill: [`md-check`](../skills/md-check/SKILL.md)
- Skill: [`md-graph`](../skills/md-graph/SKILL.md)
- Skill: [`intel-observer`](../skills/intel-observer/SKILL.md) — pipes file-change events in for cron mode
- Agent: [`dealer-osint-researcher`](../agents/dealer-osint-researcher.md)

---

## Never do

- Never fabricate contact data — if inferring, mark `inferred: true` in the stub
- Never mark a dealer "active" without verified edges
- Never skip the md-graph step — silent link-rot is the whole point of this pipeline
