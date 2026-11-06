"""Undo CLI for loudness writes: replays the write journal in reverse.

Usage::

    python -m plex_generate_previews.loudness.undo \\
        --db /path/to/com.plexapp.plugins.library.db \\
        --log /path/to/loudness-writes.jsonl [--dry-run]

Only *confirmed* journal entries are replayed, newest first. Each
entry restores the exact ``extra_data`` values the stream and its
metadata item had before the write. Safety rails:

- the journal binds to the exact database file (device + inode);
  a copied or replaced database is refused;
- refuses while the database is locked (stop Plex first);
- skips entries whose current values no longer match what was
  written (e.g. Plex re-analyzed the track since) instead of
  clobbering newer data;
- ``--dry-run`` prints what would be restored and writes nothing.

Exit code 0 on success (including dry-run), 1 on refusal or error.
"""
import argparse
import sqlite3
import sys
import time

from loguru import logger

from . import ANALYSIS_VERSION
from .errors import DatabaseBusyError, GuardError, LoudnessWriteError
from .extra_data import decode_extra_data
from .guard import check_wal_mode, database_identity
from .plex_db import (
    _begin_immediate,
    check_schema,
    confirmed_writes,
    read_stream,
)


def _current_matches_written(stream, fields):
    """True when the row still holds exactly the values we wrote."""
    if stream is None:
        return False
    decoded = decode_extra_data(stream.extra_data)
    if decoded.get("ln:loudnessAnalysisVersion") != ANALYSIS_VERSION:
        return False
    return all(decoded.get(k) == v for k, v in fields.items())


def run_undo(db_path, journal_path, dry_run=False, lock_deadline_s=5.0):
    """Replay confirmed journal entries in reverse. Returns a summary dict.

    Raises GuardError / DatabaseBusyError / LoudnessWriteError on refusal.
    """
    db_dev, db_ino = database_identity(db_path)
    entries = confirmed_writes(journal_path)
    mismatched_db = [e for e in entries
                     if (e.get("db_dev"), e.get("db_ino")) != (db_dev, db_ino)]
    if mismatched_db:
        raise GuardError(
            "write journal belongs to a different database file "
            f"(device/inode mismatch on {len(mismatched_db)} entries) — "
            "refusing to undo against this database"
        )

    summary = {"restored": 0, "skipped": 0, "dry_run": dry_run,
               "entries": len(entries), "details": []}
    if not entries:
        return summary

    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout = 1000")
        check_wal_mode(con)
        if dry_run:
            for entry in reversed(entries):
                stream = read_stream(con, entry["stream_id"])
                action = ("would-restore" if _current_matches_written(
                    stream, entry.get("fields", {})) else "would-skip")
                summary["details"].append(
                    {"stream_id": entry["stream_id"], "action": action})
                if action == "would-restore":
                    summary["restored"] += 1
                else:
                    summary["skipped"] += 1
            return summary

        _begin_immediate(con, lock_deadline_s)
        try:
            check_schema(con)
            for entry in reversed(entries):
                stream = read_stream(con, entry["stream_id"])
                if not _current_matches_written(stream, entry.get("fields", {})):
                    summary["skipped"] += 1
                    summary["details"].append(
                        {"stream_id": entry["stream_id"], "action": "skipped",
                         "reason": "current values no longer match the write"})
                    continue
                con.execute(
                    "UPDATE media_streams SET extra_data = ?, updated_at = ? "
                    "WHERE id = ?",
                    (entry["prev_stream_extra_data"], str(int(time.time())),
                     entry["stream_id"]),
                )
                con.execute(
                    "UPDATE metadata_items SET extra_data = ? WHERE id = ?",
                    (entry["prev_metadata_extra_data"], entry["metadata_id"]),
                )
                summary["restored"] += 1
                summary["details"].append(
                    {"stream_id": entry["stream_id"], "action": "restored"})
            con.execute("COMMIT")
        except BaseException:
            try:
                if con.in_transaction:
                    con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        con.close()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Undo loudness writes recorded in loudness-writes.jsonl")
    parser.add_argument("--db", required=True,
                        help="path to Plex library database")
    parser.add_argument("--log", required=True,
                        help="path to loudness-writes.jsonl")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be restored; write nothing")
    args = parser.parse_args(argv)

    try:
        summary = run_undo(args.db, args.log, dry_run=args.dry_run)
    except (GuardError, DatabaseBusyError, LoudnessWriteError) as e:
        print(f"undo refused: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"undo failed: {e}", file=sys.stderr)
        return 1

    verb = "would restore" if args.dry_run else "restored"
    print(f"{verb} {summary['restored']} stream(s), "
          f"skipped {summary['skipped']} of {summary['entries']} confirmed entries")
    for detail in summary["details"]:
        print(f"  stream {detail['stream_id']}: {detail['action']}"
              + (f" ({detail['reason']})" if detail.get("reason") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
