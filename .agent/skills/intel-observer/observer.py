#!/usr/bin/env python3
"""
observer.py — file-system watcher for intel/ directory coordination.

Multiple AI agents write into mechas-os/intel/ (scrapers, validators,
enrichers). Without a shared signal, every downstream step re-runs against
the full tree. Observer snapshots the tree, diffs against last run, and
emits JSON events so agents run ONLY on what changed.

Usage:
    python3 observer.py scan --root intel/ --state intel/.observer-state.json
    python3 observer.py scan --root intel/ --state .../state.json --dispatch --pretty

Output (JSON):
    {
      "root": "/abs/path/to/intel",
      "state": "/abs/path/.state.json",
      "scanned_at": "2026-04-24T12:34:56+00:00",
      "events": [
        {
          "type": "modified",
          "path": "exhibitions.json",
          "size": 4821,
          "mtime": 1761234567.0,
          "sha256": "abc123...",
          "sha256_prev": "def456...",
          "suggested_command": "python3 .../validate.py"
        }
      ]
    }

Design:
    - Stdlib only (hashlib, json, pathlib, argparse)
    - Idempotent: second scan with no changes returns []
    - Safe defaults: ignore __pycache__/, *.pyc, .DS_Store, .observer-state.json
    - Dispatcher: known paths → suggested command (static table, extendable)
    - Exit 0 always (empty events ≠ failure)

Integrates with mechas-os via cron:
    */5 * * * * cd mechas-os && python3 .../observer.py scan \\
        --root intel/ --state intel/.observer-state.json \\
        --dispatch > /tmp/intel-events.jsonl
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

# ── Config ────────────────────────────────────────────────────────────

IGNORE_GLOBS: tuple[str, ...] = (
    "__pycache__",
    "*.pyc",
    ".DS_Store",
    ".observer-state.json",
    ".git",
    "*.swp",
    "*~",
    ".last-scan.json",
    ".credentials.env",
)

# Dispatcher — path glob → shell command hint
DISPATCH_RULES: tuple[tuple[str, str], ...] = (
    ("*exhibitions.json",
     "python3 antigravity-kit/.agent/skills/exhibition-explorer/validate.py"),
    ("*hebei-cluster/new-entrants.csv",
     "python3 email-os/src/tools/hebei-enrich.py"),
    ("*quotes/*.xlsx",
     "python3 email-os/src/tools/build-quotes-history.py"),
    ("*intel/markets/*/segments.md",
     "python3 tools/rebuild-segment-index.py"),
    ("*intel/markets/*.md",
     "python3 tools/rebuild-segment-index.py"),
    ("*intel/supply/*.md",
     "python3 tools/rebuild-supply-index.py"),
    ("*intel/INDEX.md",
     "python3 intel/build-spa.py"),
    ("*intel/STRATEGY.md",
     "python3 intel/build-spa.py"),
)


# ── State ─────────────────────────────────────────────────────────────

@dataclasses.dataclass
class FileFingerprint:
    size: int
    mtime: float
    sha256: str

    def to_dict(self) -> dict:
        return {"size": self.size, "mtime": self.mtime, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, d: dict) -> "FileFingerprint":
        return cls(size=d["size"], mtime=d["mtime"], sha256=d["sha256"])


def _sha256_of_file(path: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _is_ignored(relpath: str) -> bool:
    parts = Path(relpath).parts
    for pattern in IGNORE_GLOBS:
        # Match against any path component
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
        if fnmatch.fnmatch(relpath, pattern):
            return True
    return False


def _walk(root: Path, extra_skip: Optional[set[str]] = None) -> list[tuple[str, Path]]:
    """Yield (relpath, abs_path) for every non-ignored file under root.

    extra_skip: absolute paths to skip even if not matching ignore globs
    (used to exclude the state file itself when it sits inside root).
    """
    skip_abs = {Path(p).resolve() for p in (extra_skip or set())}
    out: list[tuple[str, Path]] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories in-place
        dirnames[:] = [d for d in dirnames if not _is_ignored(d)]
        for f in filenames:
            abs_p = Path(dirpath) / f
            if abs_p.resolve() in skip_abs:
                continue
            rel = str(abs_p.relative_to(root))
            if _is_ignored(rel) or _is_ignored(f):
                continue
            out.append((rel, abs_p))
    return out


def _fingerprint_tree(
    root: Path,
    extra_skip: Optional[set[str]] = None,
) -> dict[str, FileFingerprint]:
    fp: dict[str, FileFingerprint] = {}
    for rel, abs_p in _walk(root, extra_skip=extra_skip):
        try:
            st = abs_p.stat()
        except OSError:
            continue
        fp[rel] = FileFingerprint(
            size=st.st_size,
            mtime=st.st_mtime,
            sha256=_sha256_of_file(abs_p),
        )
    return fp


def _load_state(path: Optional[Union[str, Path]]) -> dict[str, FileFingerprint]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    files = raw.get("files") or {}
    return {k: FileFingerprint.from_dict(v) for k, v in files.items()}


def _save_state(
    path: Union[str, Path],
    fingerprints: dict[str, FileFingerprint],
) -> None:
    p = Path(path)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": {k: v.to_dict() for k, v in fingerprints.items()},
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Dispatcher ────────────────────────────────────────────────────────

def dispatch_for(relpath: str) -> Optional[str]:
    """Map a changed path to a suggested shell command, or None."""
    for pattern, command in DISPATCH_RULES:
        if fnmatch.fnmatch(relpath, pattern):
            return command
    return None


# ── Core scan ─────────────────────────────────────────────────────────

def scan(
    root: Union[str, Path],
    state_path: Optional[Union[str, Path]] = None,
    dispatch: bool = False,
) -> list[dict]:
    """Snapshot `root`, diff against persisted state, return event list.

    Events: {"type": "added"|"modified"|"deleted", "path": str, ...}
    If state_path is provided, updated state is written atomically on return.
    """
    root_p = Path(root).resolve()
    # Always exclude the state file itself from scans — regardless of filename
    skip: set[str] = set()
    if state_path:
        skip.add(str(Path(state_path).resolve()))
    current = _fingerprint_tree(root_p, extra_skip=skip)
    prev = _load_state(state_path)

    events: list[dict] = []

    # added + modified
    for rel, fp in current.items():
        if rel not in prev:
            e = {
                "type": "added",
                "path": rel,
                "size": fp.size,
                "mtime": fp.mtime,
                "sha256": fp.sha256,
            }
            if dispatch:
                cmd = dispatch_for(rel)
                if cmd:
                    e["suggested_command"] = cmd
            events.append(e)
        elif prev[rel].sha256 != fp.sha256 \
                or prev[rel].size != fp.size \
                or prev[rel].mtime != fp.mtime:
            e = {
                "type": "modified",
                "path": rel,
                "size": fp.size,
                "mtime": fp.mtime,
                "sha256": fp.sha256,
                "sha256_prev": prev[rel].sha256,
            }
            if dispatch:
                cmd = dispatch_for(rel)
                if cmd:
                    e["suggested_command"] = cmd
            events.append(e)

    # deleted
    for rel in prev:
        if rel not in current:
            events.append({
                "type": "deleted",
                "path": rel,
                "sha256_prev": prev[rel].sha256,
            })

    # Persist state for next scan
    if state_path:
        _save_state(state_path, current)

    return events


# ── CLI ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="observer",
        description="Intel directory change observer (stdlib-only)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    scan_p = sub.add_parser("scan", help="Snapshot + diff vs state")
    scan_p.add_argument("--root", required=True,
                        help="Directory to scan (e.g. intel/)")
    scan_p.add_argument("--state", required=False, default=None,
                        help="Path to state JSON (absent = stateless)")
    scan_p.add_argument("--dispatch", action="store_true",
                        help="Attach `suggested_command` to each event")
    scan_p.add_argument("--pretty", action="store_true",
                        help="Indent JSON output")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "scan":
        events = scan(args.root, state_path=args.state, dispatch=args.dispatch)
        payload = {
            "root": str(Path(args.root).resolve()),
            "state": str(Path(args.state).resolve()) if args.state else None,
            "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "events": events,
            "event_count": len(events),
        }
        if args.pretty:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(payload, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
