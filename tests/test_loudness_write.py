"""Tests for the guarded loudness write path (plex_db.write_stream)."""
import json
import sqlite3

import pytest

from plex_generate_previews.loudness import plex_db
from plex_generate_previews.loudness.errors import (
    DatabaseBusyError,
    FingerprintMismatchError,
    GuardError,
    LoudnessWriteError,
)
from plex_generate_previews.loudness.extra_data import decode_extra_data
from plex_generate_previews.loudness.plex_db import (
    check_schema,
    confirmed_writes,
    fingerprint_file,
    read_stream,
    write_stream,
)
from tests.loudness_phase2_fixtures import (
    FIELDS,
    make_media_file,
    make_plex_db,
)


@pytest.fixture
def plex(tmp_path):
    db_path = make_plex_db(str(tmp_path / "plex.db"))
    media = make_media_file(tmp_path)
    journal = str(tmp_path / "loudness-writes.jsonl")
    yield {"db": db_path, "media": media, "journal": journal}


def _open(path, busy_timeout_ms=5000):
    con = sqlite3.connect(path, isolation_level=None)
    con.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return con


def _write(plex, **overrides):
    busy_timeout_ms = overrides.pop("busy_timeout_ms", 5000)
    con = _open(plex["db"], busy_timeout_ms)
    try:
        fp = fingerprint_file(plex["media"])
        kwargs = dict(
            stream_id=1, fields=dict(FIELDS),
            journal_path=plex["journal"], db_path=plex["db"],
            expected_part_hash="h1", expected_part_size=1000,
            local_path=plex["media"], fingerprint=fp,
        )
        kwargs.update(overrides)
        return write_stream(con, **kwargs)
    finally:
        con.close()


def test_write_happy_path(plex):
    res = _write(plex)
    assert res["status"] == "written"
    con = _open(plex["db"])
    try:
        stream = read_stream(con, 1)
        decoded = decode_extra_data(stream.extra_data)
        assert decoded["ln:loudness"] == "-16.32"
        assert decoded["ln:loudnessAnalysisVersion"] == "0.02"
        assert stream.updated_at != ""
        meta = con.execute(
            "SELECT extra_data FROM metadata_items WHERE id=10").fetchone()[0]
        assert decode_extra_data(meta)["ln:loudnessAnalysisVersion"] == "0.02"
    finally:
        con.close()
    # journal: one intent + one confirmation
    entries = plex_db.read_journal(plex["journal"])
    assert [e["status"] for e in entries] == ["intent", "committed"]
    assert entries[0]["stream_id"] == 1
    assert entries[0]["prev_stream_extra_data"] == "{}"
    assert len(confirmed_writes(plex["journal"])) == 1


def test_write_preserves_other_extra_data_keys(plex):
    con = _open(plex["db"])
    con.execute("UPDATE media_streams SET extra_data=? WHERE id=1",
                (json.dumps({"title": "Movie A"}),))
    con.close()
    _write(plex)
    con = _open(plex["db"])
    try:
        decoded = decode_extra_data(read_stream(con, 1).extra_data)
        assert decoded["title"] == "Movie A"
        assert decoded["ln:loudness"] == "-16.32"
    finally:
        con.close()


def test_write_is_idempotent(plex):
    assert _write(plex)["status"] == "written"
    assert _write(plex)["status"] == "already"
    entries = plex_db.read_journal(plex["journal"])
    assert len(entries) == 2  # no new journal lines for the second write


def test_write_already_analyzed_is_already(plex):
    # stream 3 is already analyzed in the fixture
    con = _open(plex["db"])
    try:
        res = write_stream(
            con, stream_id=3, fields=dict(FIELDS),
            journal_path=plex["journal"], db_path=plex["db"])
        assert res["status"] == "already"
    finally:
        con.close()
    assert plex_db.read_journal(plex["journal"]) == []


def test_write_refuses_changed_source_file(plex):
    # fingerprint captured at analysis time, then the file changes
    fp = fingerprint_file(plex["media"])
    with open(plex["media"], "ab") as fh:
        fh.write(b"more bytes")  # fingerprint no longer matches
    con = _open(plex["db"])
    try:
        with pytest.raises(FingerprintMismatchError):
            write_stream(
                con, stream_id=1, fields=dict(FIELDS),
                journal_path=plex["journal"], db_path=plex["db"],
                expected_part_hash="h1", expected_part_size=1000,
                local_path=plex["media"], fingerprint=fp)
    finally:
        con.close()
    con = _open(plex["db"])
    try:
        assert decode_extra_data(read_stream(con, 1).extra_data) == {}
    finally:
        con.close()
    assert plex_db.read_journal(plex["journal"]) == []


def test_write_refuses_changed_part_hash(plex):
    with pytest.raises(FingerprintMismatchError, match="part hash changed"):
        _write(plex, expected_part_hash="different")
    assert plex_db.read_journal(plex["journal"]) == []


def test_write_refuses_garbage_extra_data(plex):
    con = _open(plex["db"])
    con.execute("UPDATE media_streams SET extra_data=? WHERE id=1",
                ("not json at all {{{",))
    con.close()
    with pytest.raises(LoudnessWriteError, match="cannot merge"):
        _write(plex)
    assert plex_db.read_journal(plex["journal"]) == []


def test_write_refuses_missing_stream(plex):
    with pytest.raises(LoudnessWriteError, match="not found"):
        _write(plex, stream_id=999)


def test_schema_refuses_trigger_on_media_streams(plex):
    con = _open(plex["db"])
    con.execute(
        "CREATE TRIGGER trg AFTER UPDATE ON media_streams BEGIN "
        "SELECT 1; END")
    con.close()
    with pytest.raises(GuardError, match="trigger .* on media_streams"):
        _write(plex)


def test_schema_refuses_extra_data_trigger_on_metadata(plex):
    con = _open(plex["db"])
    con.execute(
        "CREATE TRIGGER trg AFTER UPDATE OF extra_data ON metadata_items "
        "BEGIN SELECT 1; END")
    con.close()
    with pytest.raises(GuardError, match="could fire on an extra_data write"):
        _write(plex)


def test_schema_allows_unrelated_trigger_on_metadata(plex):
    con = _open(plex["db"])
    con.execute(
        "CREATE TRIGGER trg AFTER UPDATE OF title ON metadata_items "
        "BEGIN SELECT 1; END")
    con.close()
    assert _write(plex)["status"] == "written"


def test_schema_refuses_missing_column(plex):
    con = _open(plex["db"])
    # fresh DB without the channels column (not required) still passes;
    # simulate a missing required column via a minimal schema
    con.execute("DROP TABLE media_streams")
    con.execute(
        "CREATE TABLE media_streams (id INTEGER PRIMARY KEY, "
        "media_part_id INTEGER, extra_data TEXT)")
    con.close()
    with pytest.raises(GuardError, match="missing columns"):
        _write(plex)


def test_open_writable_refuses_missing_and_non_wal(tmp_path):
    from plex_generate_previews.plex_client import open_plex_db_writable
    # missing database file
    with pytest.raises(FileNotFoundError):
        open_plex_db_writable(str(tmp_path / "missing-config"))
    # existing database that is not in WAL mode, at the expected location
    folder = tmp_path / "plex-config"
    db_path = (folder / "Plug-in Support" / "Databases"
               / "com.plexapp.plugins.library.db")
    db_path.parent.mkdir(parents=True)
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="not 'wal'"):
        open_plex_db_writable(str(folder))


def test_write_defers_on_lock(plex):
    holder = _open(plex["db"], busy_timeout_ms=200)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE media_streams SET codec='X' WHERE id=1")
    try:
        with pytest.raises(DatabaseBusyError, match="deferring"):
            _write(plex, lock_deadline_s=1.0, busy_timeout_ms=200)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    # nothing was written
    con = _open(plex["db"])
    try:
        assert decode_extra_data(read_stream(con, 1).extra_data) == {}
    finally:
        con.close()
    assert plex_db.read_journal(plex["journal"]) == []


def test_check_schema_ok(plex):
    con = _open(plex["db"])
    try:
        check_schema(con)  # no raise
    finally:
        con.close()


def test_unconfirmed_intent_is_not_replayed(plex):
    _write(plex)
    # append a dangling intent with no confirmation
    import time
    import uuid
    plex_db._log_write(plex["journal"], {
        "id": str(uuid.uuid4()), "status": "intent", "ts": time.time(),
        "stream_id": 3})
    assert len(confirmed_writes(plex["journal"])) == 1
