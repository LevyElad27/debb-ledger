"""
Copy new frozen days from DEBB's public API into ledger/, for the anchor Action.
Standard library only.

Rules:
  - a day file already here is never rewritten or removed;
  - everything is checked in a scratch copy first, with verify_ledger.py and
    the digest this repo already holds as the expected head, so a rewritten but
    self-consistent history is refused;
  - on any conflict nothing in ledger/ changes: what the API served is kept as
    a new file under mismatches/ and the run exits 1, so the mismatch itself
    gets committed and the workflow fails visibly.

Exit: 0 = up to date (or nothing frozen yet), 1 = mismatch, 2 = API unreachable
(a network failure, a short read, a page that isn't a ledger response, or a
response over MAX_BYTES). A mismatch is a public accusation: never for those.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import verify_ledger

API = "https://api.debb.fyi"
LEDGER = Path("ledger")
MISMATCHES = Path("mismatches")
ATTEMPTS = 5
RETRY_SECONDS = 30
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BYTES = 32 * 1024 * 1024  # a day file is about 150 KB
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DAY_FILE_START = b'{"columns":'  # every day file's header line
# A day freezes at D+2: a real start can't be older than that, plus a late run.
ANCHOR_START_MAX_AGE_DAYS = 3
# It locks once stored, so a mistyped far-future year must not get in.
ANCHOR_START_MAX_AHEAD_DAYS = 14
ANCHORED_FIELDS = ("date", "row_count", "file_sha256", "prev_digest", "digest", "frozen_at", "schema")


class Unreachable(Exception):
    """The API kept failing after every retry."""


def fetch(path: str) -> bytes:
    request = urllib.request.Request(API + path, headers={"User-Agent": "debb-ledger-anchor/1"})
    problem = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_SECONDS * attempt)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read(MAX_BYTES + 1)
                length = response.headers.get("Content-Length")
            if len(body) > MAX_BYTES:
                raise Unreachable(f"{path}: response over {MAX_BYTES} bytes")
            # read(n) returns what it got when the connection drops, with no error.
            if length is not None and len(body) != int(length):
                problem = f"short read, {len(body)} of {length} bytes"
                continue
            return body
        except urllib.error.HTTPError as exc:
            problem = f"HTTP {exc.code}"
            if exc.code not in RETRY_STATUS:
                break
        except (OSError, http.client.HTTPException, ValueError) as exc:  # ValueError: a bad Content-Length
            problem = type(exc).__name__
    raise Unreachable(f"{path}: {problem}")


def stored_index() -> dict:
    path = LEDGER / "index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"days": []}


def record_mismatch(reason: str, served: bytes, bodies: dict) -> Path:
    """Keep what was served, in a new directory; never touches ledger/."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    MISMATCHES.mkdir(exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=MISMATCHES))  # two in one clock tick stay apart
    (target / "reason.txt").write_text(reason + "\n", encoding="utf-8")
    (target / "served_index.json").write_bytes(served)
    for day, body in bodies.items():
        (target / f"{day}.jsonl").write_bytes(body)
    return target


def well_formed(index) -> bool:
    days = index.get("days") if isinstance(index, dict) else None
    return (isinstance(days, list) and all(isinstance(e, dict) and isinstance(e.get("date"), str) for e in days)
            and isinstance(index.get("anchor_start"), (str, type(None))))


def conflict_with_anchored(anchored: list, days: list) -> str | None:
    served = {e["date"]: e for e in days}
    for entry in anchored:
        other = served.get(entry["date"])
        if other is None:
            return f"{entry['date']}: anchored here but no longer served"
        if any(other.get(k) != entry.get(k) for k in ANCHORED_FIELDS):
            return f"{entry['date']}: served entry differs from the anchored one"
    return None


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def conflict_with_anchor_start(stored: dict, served: dict) -> str | None:
    """Coverage starts once and never moves: it can only begin after the newest
    day already here, because those days were published after the fact. The API
    decides what counts as "here", so the start must also be recent by this
    runner's own clock: otherwise a shortened first list could put old days
    after the start, and a later run could backfill them as covered."""
    before, after = stored.get("anchor_start"), served.get("anchor_start")
    if before is not None:
        return None if after == before else f"anchor_start changed from {before!r} to {after!r}"
    if after is None:
        return None
    try:
        start = date.fromisoformat(after) if DATE_RE.fullmatch(after) else None
    except ValueError:
        start = None
    if start is None:
        return f"anchor_start {after!r} is not a date"
    # First sync: what it is about to commit counts as already here.
    newest = max((e["date"] for e in stored["days"] or served["days"]), default=None)
    if newest is not None and after <= newest:
        return f"anchor_start {after!r} is not after the newest anchored day {newest}"
    if start < today_utc() - timedelta(days=ANCHOR_START_MAX_AGE_DAYS):
        return f"anchor_start {after!r} is too old to be a new start on {today_utc()}"
    if start > today_utc() + timedelta(days=ANCHOR_START_MAX_AHEAD_DAYS):
        return f"anchor_start {after!r} is too far ahead of {today_utc()}"
    return None


def verify_copy(index_bytes: bytes, new_bodies: dict, head: str | None) -> str | None:
    """verify_ledger on a scratch copy of what ledger/ would become."""
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "ledger"
        if LEDGER.is_dir():
            shutil.copytree(LEDGER, copy)
        else:
            copy.mkdir()
        (copy / "index.json").write_bytes(index_bytes)
        for day, body in new_bodies.items():
            (copy / f"{day}.jsonl").write_bytes(body)
        return verify_ledger.verify(copy, head)


def sync() -> int:
    # The query only keeps a cached index, older than this repo, out of the check.
    index_bytes = fetch(f"/v1/ledger/days?at={int(time.time())}")
    try:
        index = json.loads(index_bytes)
    except ValueError as exc:  # an edge or maintenance page, not an answer from the API
        raise Unreachable("/v1/ledger/days: not JSON") from exc
    if not isinstance(index, dict) or "days" not in index:  # JSON, but not this API's answer
        raise Unreachable("/v1/ledger/days: not a ledger index")
    if not well_formed(index):
        print(f"FAIL {record_mismatch('index is not a list of days', index_bytes, {})}")
        return 1
    days = index["days"]
    stored = stored_index()
    anchored = stored["days"]
    head = max(anchored, key=lambda e: e["date"])["digest"] if anchored else None
    known = {e["date"] for e in anchored}

    problem = conflict_with_anchored(anchored, days) or conflict_with_anchor_start(stored, index)
    new_bodies: dict = {}
    if problem is None:
        for entry in days:
            day = entry["date"]
            if day in known:
                continue
            # The date names a file and a URL: nothing but a plain date gets through.
            if not DATE_RE.fullmatch(day):
                problem = f"bad date in index: {day!r}"
                break
            if (LEDGER / f"{day}.jsonl").exists():
                problem = f"{day}: file here without an index entry"
                break
            body = fetch(f"/v1/ledger/days/{day}")
            if hashlib.sha256(body).hexdigest() != entry.get("file_sha256"):
                body = fetch(f"/v1/ledger/days/{day}")  # a dropped connection, before calling it a mismatch
            if not body.startswith(DAY_FILE_START):  # an edge or maintenance page with a 200
                raise Unreachable(f"/v1/ledger/days/{day}: not a day file")
            new_bodies[day] = body
    if problem is None and days:
        problem = verify_copy(index_bytes, new_bodies, head)
    if problem is not None:
        print(f"FAIL {problem}: kept in {record_mismatch(problem, index_bytes, new_bodies)}")
        return 1

    if not days:
        print("nothing frozen yet")
        return 0
    LEDGER.mkdir(exist_ok=True)
    for day, body in new_bodies.items():
        with open(LEDGER / f"{day}.jsonl", "xb") as out:  # "x": refuses to overwrite
            out.write(body)
    (LEDGER / "index.json").write_bytes(index_bytes)
    print(f"OK {len(new_bodies)} new day(s), head {days[-1]['digest']}, "
          f"sha256 of index {hashlib.sha256(index_bytes).hexdigest()}")
    return 0


def main() -> int:
    try:
        return sync()
    except Unreachable as exc:
        print(f"UNREACHABLE {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
