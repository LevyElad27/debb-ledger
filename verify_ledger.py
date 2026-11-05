"""
Verify a downloaded copy of DEBB's public prediction ledger. Standard library
only, and independent of DEBB's code on purpose: anyone can read and run it.

Directory layout (what the debb-ledger repo stores):
  <dir>/index.json         [{"date": "YYYY-MM-DD", "row_count": N,
                             "file_sha256": "<hex>", "digest": "<hex>"}, ...]
                           (or the API's /v1/ledger/days response as is: its "days")
  <dir>/YYYY-MM-DD.jsonl   one file per day, exactly as served

Checks, from the genesis day on, with no gaps:
  - sha256(file bytes) == file_sha256
  - the header line names the same date, row_count and schema, and the file
    holds exactly row_count rows
  - digest == sha256(prev_digest + "\\n" + date + "\\n" + file_sha256),
    prev_digest = 64 zeros for the first day

What a pass means: the files are consistent with their own index. Pass the
latest digest from a copy you saved earlier (or a fork, or the anchor repo's
commit history, which has the same owner as DEBB) as --expect-head, so a
rewritten but self-consistent copy fails. It never proves the list of predictions is
complete, nor when a row was written.

Usage:  python -I scripts/verify_ledger.py <dir> [--expect-head <digest>]
        (exit 0 = OK, 1 = mismatch)
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

SCHEMA = "debb-shadow-ledger/1"
GENESIS = "2026-10-10"
COLUMNS = [
    "id", "token_address", "deployer", "launched_at", "scored_at", "pool_found",
    "pool_wait_seconds", "liquidity_usd_at_score", "token_age_hours", "deployer_known",
    "deployer_rug_rate", "lp_locked", "bundled_launch", "debb_score", "debb_verdict",
    "rug_prob", "model_sha256", "naive_would_buy", "naive_filter_reasons",
]


def _check_day(directory: Path, entry: dict, day: str, prev: str) -> tuple:
    """Returns (problem or None, this day's digest)."""
    path = directory / f"{day}.jsonl"
    if not path.is_file():
        return f"{day}: file missing", ""
    body = path.read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    if sha != entry.get("file_sha256"):
        return f"{day}: file sha256 mismatch", ""
    try:
        lines = body.decode("ascii").split("\n")
        header = json.loads(lines[0])
        rows = [json.loads(line) for line in lines[1:-1]]
    except (UnicodeDecodeError, ValueError):
        return f"{day}: file is not ASCII JSON lines", ""
    if lines[-1] != "":
        return f"{day}: file does not end with a newline", ""
    if not isinstance(header, dict) or header.get("date") != day or header.get("schema") != SCHEMA:
        return f"{day}: header date/schema mismatch", ""
    if header.get("columns") != COLUMNS:
        return f"{day}: header columns differ from the published schema", ""
    if any(not isinstance(r, list) or len(r) != len(COLUMNS) for r in rows):
        return f"{day}: a row does not match the header's columns", ""
    ids = [r[0] for r in rows]
    if any(not isinstance(i, int) for i in ids) or ids != sorted(set(ids)):
        return f"{day}: row ids are not unique ascending integers", ""
    scored = COLUMNS.index("scored_at")
    if any(not isinstance(r[scored], str) or not r[scored].startswith(day + "T") for r in rows):
        return f"{day}: a row's scored_at is outside the day", ""
    if header.get("row_count") != entry.get("row_count") or len(rows) != entry.get("row_count"):
        return f"{day}: row_count mismatch", ""
    if "prev_digest" in entry and entry["prev_digest"] != prev:
        return f"{day}: prev_digest does not match the previous day's digest", ""
    digest = hashlib.sha256(f"{prev}\n{day}\n{sha}".encode("ascii")).hexdigest()
    if digest != entry.get("digest"):
        return f"{day}: digest mismatch", ""
    return None, digest


def warning(directory: Path, expect_head: str | None) -> str | None:
    """What an OK does not prove, for this run."""
    if expect_head is not None:
        return None
    try:
        index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    unanchored = isinstance(index, dict) and index.get("anchor_start") is None
    return ("WARNING: no --expect-head given" + (" and the ledger says nothing is anchored yet" if unanchored else "")
            + ": OK only means the files agree with each other, not that they are unchanged.")


def verify(directory: Path, expect_head: str | None = None) -> str | None:
    try:
        index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        if isinstance(index, dict):  # the API's /v1/ledger/days response saved as is
            index = index.get("days")
            if index == []:
                return "index.json: the ledger has no frozen days yet (nothing to verify)"
        if not isinstance(index, list) or not index or not all(isinstance(e, dict) for e in index):
            raise TypeError("not a non-empty list of objects")
        entries = sorted(index, key=lambda e: str(e["date"]))
    except (OSError, ValueError, TypeError, KeyError):
        return "index.json: missing, empty, or not a list of {date, row_count, file_sha256, digest}"
    prev = "0" * 64
    expected = date.fromisoformat(GENESIS)
    head_seen = expect_head is None
    for entry in entries:
        day = entry["date"]
        # Only the exact next date is accepted, so an index entry can never
        # point the file read anywhere else (no paths, no traversal).
        if day != expected.isoformat():
            return f"{expected.isoformat()}: missing (next listed day is {day!r})"
        problem, prev = _check_day(directory, entry, day, prev)
        if problem:
            return problem
        if prev == expect_head:
            head_seen = True  # keep going: every later day must verify too
        expected += timedelta(days=1)
    if not head_seen:
        return f"expected head digest {expect_head!r} is not in this chain"
    return None


def main(argv: list[str]) -> int:
    args = argv[1:]
    expect_head = None
    if len(args) == 3 and args[1] == "--expect-head":
        expect_head = args[2]
    elif len(args) != 1:
        print("usage: python -I scripts/verify_ledger.py <dir> [--expect-head <digest>]", file=sys.stderr)
        return 2
    problem = verify(Path(args[0]), expect_head)
    if problem:
        print(f"FAIL {problem}")
        return 1
    print("OK")
    note = warning(Path(args[0]), expect_head)
    if note:
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
