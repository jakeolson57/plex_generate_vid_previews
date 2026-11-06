"""Guarded writes of loudness measurements into Plex's database.

This is the only module that writes to Plex. Every write goes through
:func:`write_stream`, which enforces the spec's safety rails:

- schema check on every write transaction (required columns; no
  trigger on ``media_streams``; no ``extra_data``-affecting trigger
  on ``metadata_items``);
- ``BEGIN IMMEDIATE`` with a deadline — lock contention raises
  :class:`DatabaseBusyError` (defer, retry later), never a failure;
- source pinning: the Plex part hash/size and the on-disk file
  fingerprint are re-verified *inside* the transaction;
- idempotency: an already-analyzed stream rolls back, not errors;
- journal-before-commit: the intent is appended to
  ``loudness-writes.jsonl`` with fsync before COMMIT, and a
  confirmation line after; ``undo`` replays only confirmed entries;
- read-back verification after commit.

Writes are ``ln:*`` fields merged into ``media_streams.extra_data``
(form-preserving) plus ``ln:loudnessAnalysisVersion`` merged into the
owning ``metadata_items.extra_data``. Nothing else in either row is
touched.
"""
import json
import os
import sqlite3
import time
import uuid
from collections import namedtuple

from loguru import logger

from . import ANALYSIS_VERSION
from .errors import (
    DatabaseBusyError,
    FingerprintMismatchError,
    GuardError,
    JournalError,
    LoudnessWriteError,
)
from .extra_data import has_loudness, merge_loudness_fields
from .guard import check_wal_mode, database_identity

AudioStream = namedtuple(
    "AudioStream",
    ["stream_id", "part_id", "item_id", "metadata_id", "stream_index",
     "codec", "extra_data", "file", "part_hash", "part_size",
     "metadata_extra_data", "updated_at"],
)

SourceFingerprint = namedtuple(
    "SourceFingerprint", ["device", "inode", "size", "mtime"])

# Tables/columns the write path depends on (schema-verified vs Plex 1.43.4).
_REQUIRED_COLUMNS = {
    "media_streams": {
        "id", "media_part_id", "stream_type_id", "index", "codec",
        "extra_data", "updated_at",
    },
    "media_parts": {"id", "media_item_id", "file", "hash", "size"},
    "media_items": {"id", "metadata_item_id", "deleted_at"},
    "metadata_items": {"id", "extra_data"},
}

# Lock deadline for BEGIN IMMEDIATE: Plex holds the DB open and third
# party tools have been observed holding the write lock 30s+.
_LOCK_DEADLINE_S = 30.0
_LOCK_RETRY_S = 0.5


def write_log_path(db_dir):
    """Path of the write journal next to the app's database."""
    return os.path.join(db_dir, "loudness-writes.jsonl")


def fingerprint_file(path):
    """Capture (device, inode, size, mtime) for a local media file."""
    st = os.stat(path)
    return SourceFingerprint(
        device=st.st_dev, inode=st.st_ino, size=st.st_size, mtime=st.st_mtime)


def verify_fingerprint(path, fingerprint):
    """Re-check a file against a fingerprint captured at analysis time."""
    if fingerprint is None:
        raise FingerprintMismatchError(
            "no source fingerprint recorded at analysis — re-run analyze")
    try:
        current = fingerprint_file(path)
    except OSError as e:
        raise FingerprintMismatchError(f"source file unreadable: {e}")
    if current != fingerprint:
        raise FingerprintMismatchError(
            f"source file changed since analysis: {path} "
            f"(was size={fingerprint.size} mtime={fingerprint.mtime})"
        )


def check_schema(con):
    """Verify the Plex schema is the one writes were designed for.

    Raises GuardError on a missing table/column, any trigger on
    ``media_streams``, or any trigger on ``metadata_items`` that could
    fire on an ``extra_data`` write.
    """
    for table, columns in _REQUIRED_COLUMNS.items():
        try:
            present = {
                row[1] for row in con.execute(f"PRAGMA table_info({table})")
            }
        except sqlite3.Error as e:
            raise GuardError(f"schema check failed for {table}: {e}")
        missing = columns - present
        if missing:
            raise GuardError(
                f"Plex schema mismatch: {table} is missing columns "
                f"{sorted(missing)} — refusing writes"
            )
    try:
        triggers = con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    except sqlite3.Error as e:
        raise GuardError(f"schema check failed reading triggers: {e}")
    for name, tbl_name, sql in triggers:
        sql_text = (sql or "").lower()
        if tbl_name == "media_streams":
            raise GuardError(
                f"refusing writes: trigger {name!r} exists on media_streams"
            )
        if tbl_name == "metadata_items" and "extra_data" in sql_text:
            raise GuardError(
                f"refusing writes: trigger {name!r} on metadata_items "
                "could fire on an extra_data write"
            )


def read_stream(con, stream_id):
    """Read one audio stream plus its part/item/metadata rows."""
    row = con.execute(
        """
        SELECT ms.id, mp.id, mi.id, md.id, ms."index", ms.codec,
               ms.extra_data, mp.file, mp.hash, mp.size,
               md.extra_data, ms.updated_at
        FROM media_streams ms
        JOIN media_parts mp ON mp.id = ms.media_part_id
        JOIN media_items mi ON mi.id = mp.media_item_id
        JOIN metadata_items md ON md.id = mi.metadata_item_id
        WHERE ms.id = ?
        """,
        (stream_id,),
    ).fetchone()
    if row is None:
        return None
    return AudioStream(*row)


def _log_write(journal_path, entry):
    """Append one journal line with fsync. Raises JournalError."""
    try:
        with open(journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as e:
        raise JournalError(f"could not append to write journal: {e}")


def _begin_immediate(con, deadline_s=_LOCK_DEADLINE_S):
    """BEGIN IMMEDIATE with a deadline; lock contention -> DatabaseBusyError."""
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            con.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            if time.monotonic() >= deadline:
                raise DatabaseBusyError(
                    f"Plex database stayed locked for {deadline_s:.0f}s; "
                    "deferring write (retry later)"
                )
            time.sleep(_LOCK_RETRY_S)


def write_stream(con, *, stream_id, fields, journal_path, db_path,
                 expected_part_hash=None, expected_part_size=None,
                 local_path=None, fingerprint=None,
                 lock_deadline_s=_LOCK_DEADLINE_S):
    """Write measured ln:* fields for one stream into Plex's database.

    Args:
        con: writable sqlite3 connection (isolation_level=None).
        stream_id: Plex ``media_streams.id``.
        fields: ln:* dict from :func:`analyze.ln_fields` (validated).
        journal_path: ``loudness-writes.jsonl`` path.
        db_path: Plex database file path (for identity binding).
        expected_part_hash/size: Plex part values captured at analysis.
        local_path: translated local media path for fingerprint checks.
        fingerprint: SourceFingerprint captured at analysis time.

    Returns {"status": "written"|"already", ...}. Raises GuardError,
    DatabaseBusyError, FingerprintMismatchError or LoudnessWriteError.
    """
    db_dev, db_ino = database_identity(db_path)
    check_wal_mode(con)

    _begin_immediate(con, lock_deadline_s)
    try:
        check_schema(con)
        stream = read_stream(con, stream_id)
        if stream is None:
            raise LoudnessWriteError(f"stream {stream_id} not found in Plex DB")
        if has_loudness(stream.extra_data):
            con.execute("ROLLBACK")
            return {"status": "already", "stream_id": stream_id}
        if expected_part_hash is not None and stream.part_hash != expected_part_hash:
            raise FingerprintMismatchError(
                f"Plex part hash changed for stream {stream_id} "
                f"(was {expected_part_hash!r}, now {stream.part_hash!r})"
            )
        if expected_part_size is not None and stream.part_size != expected_part_size:
            raise FingerprintMismatchError(
                f"Plex part size changed for stream {stream_id}"
            )
        if local_path is not None:
            verify_fingerprint(local_path, fingerprint)

        try:
            new_stream_extra = merge_loudness_fields(stream.extra_data, fields)
            new_meta_extra = merge_loudness_fields(
                stream.metadata_extra_data,
                {"ln:loudnessAnalysisVersion": ANALYSIS_VERSION},
            )
        except ValueError as e:
            raise LoudnessWriteError(f"cannot merge extra_data: {e}")

        entry_id = str(uuid.uuid4())
        intent = {
            "id": entry_id,
            "status": "intent",
            "ts": time.time(),
            "db_dev": db_dev,
            "db_ino": db_ino,
            "stream_id": stream_id,
            "part_id": stream.part_id,
            "item_id": stream.item_id,
            "metadata_id": stream.metadata_id,
            "file": stream.file,
            "part_hash": stream.part_hash,
            "part_size": stream.part_size,
            "prev_stream_extra_data": stream.extra_data,
            "prev_metadata_extra_data": stream.metadata_extra_data,
            "fields": fields,
        }
        # Journal intent (fsync) BEFORE commit — an intent without a
        # confirmation is never replayed by undo.
        _log_write(journal_path, intent)

        now = str(int(time.time()))
        con.execute(
            "UPDATE media_streams SET extra_data = ?, updated_at = ? WHERE id = ?",
            (new_stream_extra, now, stream_id),
        )
        con.execute(
            "UPDATE metadata_items SET extra_data = ? WHERE id = ?",
            (new_meta_extra, stream.metadata_id),
        )
        con.execute("COMMIT")

        _log_write(journal_path, {
            "id": entry_id, "status": "committed", "ts": time.time(),
        })

        # Read-back verification after commit.
        after = read_stream(con, stream_id)
        if after is None or not has_loudness(after.extra_data):
            raise LoudnessWriteError(
                f"read-back verification failed for stream {stream_id}"
            )
        logger.info(f"loudness: wrote measurements for Plex stream {stream_id}")
        return {"status": "written", "stream_id": stream_id,
                "journal_id": entry_id}
    except BaseException:
        try:
            if con.in_transaction:
                con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def read_journal(journal_path):
    """Read journal entries (intent + committed pairs). Returns list of dicts."""
    entries = []
    if not os.path.exists(journal_path):
        return entries
    with open(journal_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(f"loudness journal: skipping corrupt line: {line[:80]!r}")
    return entries


def confirmed_writes(journal_path):
    """Intent entries that have a matching committed confirmation."""
    entries = read_journal(journal_path)
    committed_ids = {
        e["id"] for e in entries if e.get("status") == "committed"
    }
    return [e for e in entries
            if e.get("status") == "intent" and e.get("id") in committed_ids]
