---
name: intel-observer
description: File-system change observer for multi-agent coordination.
  Use when multiple AI agents write into a shared intel/ directory and
  you need to dispatch only the relevant downstream tools on what
  actually changed. Emits JSON events per file diff. Trigger keywords:
  "what changed", "run only what's new", "re-run downstream", "observe
  intel".
---

You are the intel-observer skill. You snapshot a directory, diff it
against the last run, and emit JSON events so the next agent in the
pipeline runs ONLY what's needed.

## Use this skill when

- Multiple agents write into `intel/` and you need to know what's new
- A cron job wants to run validators / enrichers only for changed files
- You want to audit "who (or what) touched intel/ since last scan"
- You want a dispatcher: "file X changed → run tool Y"

## Do not use this skill when

- You just want to validate a single known file (use validate.py directly)
- You need real-time (sub-second) file watching — this is poll-based

## Core command

```bash
python3 observer.py scan \
    --root intel/ \
    --state intel/.observer-state.json \
    --dispatch --pretty
```

Output: JSON with `events` array (`added` / `modified` / `deleted`). Each
event carries `path`, `size`, `mtime`, `sha256`, `sha256_prev` (for
modified), and `suggested_command` (when `--dispatch` is set).

## Ignore patterns

`__pycache__`, `*.pyc`, `.DS_Store`, `.git`, `*.swp`, `*~`,
`.observer-state.json`, `.last-scan.json`, `.credentials.env` — plus
the state file itself (regardless of name).

## Dispatcher rules (extendable in `observer.py`)

| Changed path glob | Suggested command |
|---|---|
| `*exhibitions.json` | `validate.py` |
| `*hebei-cluster/new-entrants.csv` | `hebei-enrich.py` |
| `*quotes/*.xlsx` | `build-quotes-history.py` |
| `*intel/markets/*.md` | `rebuild-segment-index.py` |
| `*intel/INDEX.md` | `build-spa.py` |
| `*intel/STRATEGY.md` | `build-spa.py` |

## State file

Default: `intel/.observer-state.json` (gitignored). Contains
`{path: {size, mtime, sha256}}` for every tracked file plus an
`updated_at` ISO timestamp.

## Safety

- Zero writes outside `--state` path
- No network calls
- Stdlib only (hashlib, json, pathlib, argparse)

## Cron pattern

```cron
*/5 * * * * cd ~/Desktop/first-priciple/mechas-os && \
    python3 antigravity-kit/.agent/skills/intel-observer/observer.py scan \
        --root intel/ --state intel/.observer-state.json --dispatch \
    >> /tmp/intel-events.jsonl
```

Downstream agents tail `/tmp/intel-events.jsonl` and react to events
carrying `suggested_command`.

## Tests

23 unit tests (`test_observer.py`). Zero pip deps. Run with
`python3 -m unittest test_observer -v`.
