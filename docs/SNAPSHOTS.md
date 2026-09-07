# Pre-migration snapshots

A snapshot is a verified, self-contained copy of the JARVIS database, taken so
that a schema migration has something to fall back to.

It is **not** a rollback system. It creates a recovery point and tells you where
it is; putting one back is the manual procedure below. Automating that would
mean deciding, in code, that the live database should be discarded — a decision
that belongs to the user.

## Taking one

```bash
python3 -m jarvis snapshot create --reason "before Task 2 migration" --label pre-task2
python3 -m jarvis snapshot list
python3 -m jarvis snapshot verify
python3 -m jarvis doctor          # reports the recovery point
```

By default snapshots land in `snapshots/` beside the database:

```
.jarvis/
  jarvis.db
  snapshots/
    jarvis-v1-20260907T160422Z-snp_4d5c6abdfe1b-pre-task2.db
    jarvis-v1-20260907T160422Z-snp_4d5c6abdfe1b-pre-task2.db.json
```

The filename carries the schema version, so which state a file represents is
readable without opening anything.

## How the copy is made

**SQLite's online backup API** (`Connection.backup()`), never a file copy.

This matters. A live SQLite database in WAL mode keeps recent committed data in
a separate write-ahead log, so `cp jarvis.db backup.db` can capture the main
file without the WAL — producing a backup that is stale, or torn across a
half-applied transaction, while looking perfectly fine. The backup API reads a
consistent point-in-time image even while the database is being written to.

Two further steps make the result trustworthy:

1. **Checkpointed into a single file.** The copy inherits WAL mode, which would
   leave `-wal` and `-shm` files beside it. The destination is switched to
   `journal_mode = DELETE` so the snapshot is one self-contained file. A
   recovery point split across three files is one that someone will eventually
   copy incompletely.
2. **Verified immediately, by opening it alone.** `PRAGMA integrity_check` and
   `PRAGMA foreign_key_check` run against a fresh read-only connection to the
   snapshot, every expected table is confirmed present, and rows are counted per
   table. A snapshot that does not pass is reported as `failed` and raises — the
   file and its metadata are kept so the failure stays inspectable, but it is
   never returned as if it were usable.

The snapshot does not contain the audit event announcing its own creation: the
event is recorded after the copy is taken. A snapshot holds the state as it was
immediately before the snapshot operation.

## Metadata

Each snapshot has a JSON sidecar (`<snapshot>.db.json`) recording its id,
creation timestamp, source database, schema version, integrity and foreign-key
results, location, size, per-table row counts, the actor, the reason, and the
creation result.

Metadata lives in a sidecar rather than a database table for two reasons: a
snapshot must be usable independently of the database it came from, so its
description has to travel with it; and recording snapshots in a table would mean
a schema migration — an awkward thing to require of the mechanism that exists to
make migrations safe. `jarvis snapshot list` and `doctor` therefore read the
directory, reporting what is actually on disk rather than what the database
claims exists.

## Recovery procedure

A snapshot is a complete SQLite database. Restoring is a file move — but do it
deliberately:

```bash
# 1. Confirm the snapshot verifies BEFORE relying on it.
python3 -m jarvis snapshot verify --path <snapshot>.db
#    Expect: "integrity": "ok", "foreign_key_check": "ok", "ok": true

# 2. Stop everything using the database. SQLite has no way to stop you
#    restoring under a live process, and doing so corrupts both.

# 3. Keep the current database. It is evidence of what went wrong, and the
#    only copy of anything written after the snapshot.
mv .jarvis/jarvis.db .jarvis/jarvis.db.failed-migration
rm -f .jarvis/jarvis.db-wal .jarvis/jarvis.db-shm

# 4. Put the snapshot in place (copy, so the snapshot itself survives).
cp <snapshot>.db .jarvis/jarvis.db

# 5. Confirm the restored state.
python3 -m jarvis doctor
```

`doctor` should report the schema version the snapshot recorded, `integrity:
ok`, and the work that existed at that point.

### What restoring costs you

Everything written after the snapshot was taken. There is no merge and no
partial restore. Check the per-table row counts in the sidecar against the
current database first, so you know what you are giving up.

## When to take one

- **Before any migration that transforms existing rows.** This is the case the
  mechanism exists for.
- Before a bulk memory operation you cannot easily reverse.
- Before an experiment that writes to the live store.

`doctor` warns when a populated database has no verified snapshot for its
current schema version, and stays quiet on an empty store — there is nothing to
recover yet.

## Limitations

- **Restore is manual.** Deliberately: see the top of this document.
- **No down-migrations.** A snapshot returns the database to a previous *state*,
  not a previous *schema* on today's data.
- **Snapshots are full copies.** No incremental or differential support; size
  grows with the database.
- **Snapshots are not pruned.** Nothing deletes old ones; that is a judgement
  about what is safe to lose.
- **The snapshot directory is not itself protected.** A snapshot on the same
  disk as the database survives a bad migration, not a lost disk.
