---
name: md-check
description: Markdown intel-doc verifier. Checks every internal link
  (file exists), external URL (HTTP 200), email domain (MX via public
  DNS fallback), and phone number (country-aware format). Use
  PROACTIVELY before acting on any intel README or when the user asks
  "is this doc still accurate" / "check links in X.md".
---

You are the md-check skill. You drive a stdlib-only Python tool that
validates any markdown intel doc's freshness along four axes:

- **Internal links** — `[text](./rel/path.md)` → target file exists?
- **External links** — `[text](https://...)` → HTTP 200-399?
- **Emails** — every `a@b.c` → MX records on `b.c` (falls back to
  8.8.8.8 / 1.1.1.1 when system resolver returns SERVFAIL, which
  happens under Tailscale MagicDNS)
- **Phones** — country-aware format check (IN / US / TW / CN / default)

Each axis contributes to a composite `evidence_score` (0-5). Any failure
flips `needs_update: true`.

## Use this skill when

- Before acting on any intel README / companion doc
- When the user says "is this doc stale" / "check the links"
- As cron maintenance to catch link rot across intel/
- After a repo restructure (internal links often break silently)

## Do not use this skill when

- You need to verify a specific company (use `osint-verify` instead —
  it's company-centric with per-entity scoring)
- You only need an exhibition catalog (use `exhibition-explorer/validate.py`)

## Weight policy (CROSS-REFERENCE-PROTOCOL.md)

- Provided markdown doc = **source_tier 5** (highest trust)
- Proven edges (link resolves, MX exists, phone formats) = each +1 toward evidence_score
- No-edge claims (dangling links, bare names) flagged `needs_update`

## Core command

```bash
python3 md_check.py --file intel/markets/india/README.md --country IN --pretty
```

Options:
- `--country IN` (default) — phone format rules; also `US`, `TW`, `CN`
- `--out report.json` — write to file
- `--pretty` — indent JSON

Exit codes:
- `0` all checks pass
- `1` some warnings (broken internal/external, missing MX, bad phone)
- `2` file not found

## MX resolution strategy

Some networks (Tailscale, corporate DNS) return SERVFAIL for MX queries.
The tool tries three resolvers in order:

1. System resolver (default)
2. `@8.8.8.8` (Google Public DNS)
3. `@1.1.1.1` (Cloudflare)

First that returns records wins. Documented in the JSON report via
`emails[].resolver` field.

## Output shape

```json
{
  "file": "intel/markets/india/README.md",
  "checked_at": "2026-04-24T...",
  "country": "IN",
  "internal_links": [{"url": "./...", "ok": true, "path": "/abs/..."}],
  "external_links": [{"url": "https://...", "ok": true, "status": 200}],
  "emails": [{"email": "a@b.c", "domain": "b.c", "ok": true, "records": ["10 mx..."], "resolver": "@8.8.8.8"}],
  "phones": [{"raw": "+91-80-...", "ok": true}],
  "stats": {
    "internal_total": 5, "internal_ok": 3,
    "external_total": 12, "external_ok": 9,
    "email_total": 7, "email_ok": 7,
    "phone_total": 10, "phone_ok": 10
  },
  "evidence_score": 4,
  "source_tier": 5,
  "ok": false,
  "needs_update": true
}
```

## Tests

- 28 unit tests + 1 E2E (gated `MD_CHECK_E2E=1`)
- All stdlib
- Real-world verified against `intel/markets/india/README.md`:
  5 internal / 12 external / 7 emails / 10 phones → evidence_score 4/5

## Linked from

`intel/markets/india/README.md` has an "Automated Verification" footer
that calls this skill directly + recommends a cron pattern.
