"""Tests for the loudness undo CLI (journal replay)."""
import json
import sqlite3

import pytest

from plex_generate_previews.loudness import plex_db, undo
from plex_generate_previews.loudness.errors import DatabaseBusyError, GuardError
from plex_generate_previews.loudness.extra_data import decode_extra_data
from plex_generate_previews.loudness.plex_db import (
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
def written(tmp_path):
    """A Plex DB with one loudness write + its journal."""
    db_path = make_plex_db(str(tmp_path / "plex.db"))
    media = make_media_file(tmp_path)
    journal = str(tmp_path / "loudness-writes.jsonl")
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        write_stream(
            con, stream_id=1, fields=dict(FIELDS),
            journal_path=journal, db_path=db_path,
            expected_part_hash="h1", expected_part_size=1000,
            local_path=media, fingerprint=fingerprint_file(media))
    finally:
        con.close()
    return {"db": db_path, "media": media, "journal": journal}


def _extra(db_path, stream_id=1):
    con = sqlite3.connect(db_path)
    try:
        return decode_extra_data(read_stream(con, stream_id).extra_data)
    finally:
        con.close()


def test_undo_restores_previous_values(written):
    assert _extra(written["db"])["ln:loudness"] == "-16.32"
    summary = undo.run_undo(written["db"], written["journal"])
    assert summary["restored"] == 1
    assert summary["skipped"] == 0
    assert _extra(written["db"]) == {}


def test_undo_restores_metadata_version(written):
    undo.run_undo(written["db"], written["journal"])
    con = sqlite3.connect(written["db"])
    try:
        meta = con.execute(
            "SELECT extra_data FROM metadata_items WHERE id=10").fetchone()[0]
        assert decode_extra_data(meta) == {}
    finally:
        con.close()


def test_undo_dry_run_writes_nothing(written):
    summary = undo.run_undo(written["db"], written["journal"], dry_run=True)
    assert summary["restored"] == 1
    assert summary["dry_run"] is True
    assert _extra(written["db"])["ln:loudness"] == "-16.32"
    # journal untouched by a dry run
    assert len(plex_db.read_journal(written["journal"])) == 2


def test_undo_is_idempotent(written):
    undo.run_undo(written["db"], written["journal"])
    second = undo.run_undo(written["db"], written["journal"])
    assert second["restored"] == 0
    assert second["skipped"] == 1


def test_undo_skips_when_values_changed_since(written):
    # Plex re-analyzed the track with different values after our write.
    con = sqlite3.connect(written["db"])
    con.execute(
        "UPDATE media_streams SET extra_data=? WHERE id=1",
        (json.dumps({**FIELDS, "ln:loudness": "-20.0"}),))
    con.commit()
    con.close()
    summary = undo.run_undo(written["db"], written["journal"])
    assert summary["restored"] == 0
    assert summary["skipped"] == 1
    # the newer values are untouched
    assert _extra(written["db"])["ln:loudness"] == "-20.0"


def test_undo_refuses_different_database(written, tmp_path):
    other_db = make_plex_db(str(tmp_path / "other.db"))
    with pytest.raises(GuardError, match="different database file"):
        undo.run_undo(other_db, written["journal"])


def test_undo_refuses_locked_database(written):
    holder = sqlite3.connect(written["db"], isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE media_streams SET codec='X' WHERE id=1")
    try:
        with pytest.raises(DatabaseBusyError):
            undo.run_undo(written["db"], written["journal"], lock_deadline_s=1.0)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    # nothing was undone
    assert _extra(written["db"])["ln:loudness"] == "-16.32"


def test_undo_empty_journal(tmp_path):
    db_path = make_plex_db(str(tmp_path / "plex.db"))
    journal = str(tmp_path / "empty.jsonl")
    summary = undo.run_undo(db_path, journal)
    assert summary == {"restored": 0, "skipped": 0, "dry_run": False,
                       "entries": 0, "details": []}


def test_undo_cli_dry_run(capsys, written):
    rc = undo.main(["--db", written["db"], "--log", written["journal"],
                    "--dry-run"])
    assert rc == 0
    assert "would restore 1" in capsys.readouterr().out
    assert _extra(written["db"])["ln:loudness"] == "-16.32"


def test_undo_cli_refuses_wrong_db(capsys, written, tmp_path):
    other_db = make_plex_db(str(tmp_path / "other.db"))
    rc = undo.main(["--db", other_db, "--log", written["journal"]])
    assert rc == 1
    assert "undo refused" in capsys.readouterr().err
