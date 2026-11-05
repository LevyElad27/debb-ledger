# debb-ledger

An outside copy of [DEBB](https://debb.fyi)'s public token log. DEBB records a sample of new Solana tokens
and, for the ones it scores, the score it gave at the time. Each closed UTC day is frozen once into a file,
and each day's digest chains to the day before. A scheduled GitHub Action copies new days here, so
a day's digest gets a timestamp in this repository's commit history.

## What this proves

Once a day is committed here, a change to that day's file breaks its digest and every later one.

## What this does not prove

- That the list is complete. DEBB chooses what goes into the log.
- When a row was written. A row's `scored_at` is DEBB's own claim.
- Anything about independence. DEBB's owner also owns this repository. Force-pushes and branch deletion are
  blocked by a ruleset, and that same owner could remove the ruleset. Forks and clones you keep are the real
  protection, so keep one.
- Anything about the days already frozen when the first commit here was made, including every day in that
  commit. They were published after the fact, and some come from a database that was breached on 2026-10-22.
  Coverage starts at `anchor_start` in `ledger/index.json`: the first day frozen after that commit.

`deployer` and `deployer_rug_rate` come from heuristic labels. They are not findings about any person or wallet.

## Check it yourself

```
python verify_ledger.py ledger --expect-head <a digest you saved earlier>
```

The script uses only the standard library. OK means the files match their index and the chain contains the
digest you gave it. Without `--expect-head`, OK only means the files agree with each other.

## Layout

- `ledger/YYYY-MM-DD.jsonl`: one day, byte for byte as DEBB serves it. The format is described in `verify_ledger.py`.
- `ledger/index.json`: DEBB's `/v1/ledger/days` response.
- `mismatches/`: anything the API served that disagreed with what is already here. A day file is never overwritten.
- `sync_ledger.py` and `.github/workflows/anchor.yml`: the daily copy. It uses no secrets.
