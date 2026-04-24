---
name: osint-verify
description: Verify every link + email in an OSINT dossier via live DNS,
  HTTP, and MX-record checks. Use PROACTIVELY before outreach, before
  scoring companies, or whenever the user says "is this dossier still
  accurate" or "check the contacts before sending". Emits a JSON report
  with evidence scores (0-5) per company.
---

You are the osint-verify skill. You consume an OSINT dossier in
markdown, extract every company section with URLs + emails, and verify
each signal live:

- **DNS**: does the domain resolve (A record)?
- **HTTP**: does the website return 200-399?
- **MX**: does the email domain have mail exchangers?

Each company gets an evidence_score (0-5) reflecting how many
live-signal edges verified. Weak entries (<3) get `needs_update: true`.

## Use this skill when

- Before any cold outreach campaign
- Before adding company data to a graph / index / SPA
- When the user asks "is this contact still valid"
- When the user asks "is this dossier stale"
- As a daily cron to keep dossiers fresh

## Do not use this skill when

- The user wants to look UP new companies (this verifies existing ones)
- The dossier is not yet in markdown form (convert first, or parse JSON directly)

## Weight policy

Implements `ops/CROSS-REFERENCE-PROTOCOL.md`:
- Dossier data = **tier 5** (provided = highest trust)
- A company with live DNS + HTTP + MX = edges proven = evidence_score 5
- A company listed only by name with no URL/email = no edges = evidence_score 0
- Weak entries should not be used for outreach without enrichment

## Core command

```bash
python3 osint_verify.py \
    --dossier /path/to/dossier.md \
    --pretty
```

Optional:
- `--out report.json` — write to file instead of stdout
- `--min-score N` — filter to companies scoring ≥ N

Exit codes:
- `0` — all companies passed their evidence threshold
- `1` — some entries flagged as `needs_update`
- `2` — dossier file missing / unreadable

## Output shape

```json
{
  "source": "/path/to/dossier.md",
  "checked_at": "2026-04-24T02:07:39+00:00",
  "companies": [
    {
      "name": "CG Power and Industrial Solutions Ltd",
      "category": "Category A: TRANSFORMER BUYERS",
      "urls": ["https://www.cgglobal.com"],
      "emails": ["help@cgglobal.com", "..."],
      "url_reports": [{"url": "...", "dns": {"ok": true, "a": "x"}, "http": {"ok": true, "status": 200}}],
      "email_reports": [{"email": "...", "dns": {"ok": true}, "mx": {"ok": true, "records": ["..."]}}],
      "evidence_score": 4,
      "source_tier": 5,
      "needs_update": false
    }
  ],
  "ok": false,
  "needs_update": true,
  "stats": {"total": 12, "passing": 7, "weak": 5, "weak_slugs": ["..."]}
}
```

## Parallelism

Runs up to 6 companies in parallel via `ThreadPoolExecutor`. Each
company runs its URL + email checks sequentially (MX de-dup per
domain). Stdlib only.

## Subprocess: `dig`

MX checks shell out to `dig +short MX`. If `dig` is not installed the
check is reported as `ok: false` with `error` populated — no crash.

## Tests

- 23 unit tests + 1 E2E (gated `OSINT_E2E=1`)
- Real-world verified against `intel/markets/india/electrical-osint-dossiers.md`
  (12 companies extracted, 5 flagged as weak)

## Linked from

`intel/markets/india/electrical-osint-dossiers.md` — has a
"Automated Verification" section that calls this skill directly.
