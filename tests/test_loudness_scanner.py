"""Tests for the loudness scanner against a synthetic Plex database."""
import json
import sqlite3

import pytest

from plex_generate_previews.loudness import scanner
from plex_generate_previews.loudness import undecodable

ANALYZED = json.dumps({
    "ln:loudness": "-16.1", "ln:peak": "-1.0", "ln:lra": "7.0",
    "ln:threshold": "-34.0", "ln:gainOffset": "-0.1",
    "ln:loudnessAnalysisVersion": "0.02",
})


@pytest.fixture
def plex_db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE library_sections (id INTEGER PRIMARY KEY, name TEXT, section_type INTEGER);
        CREATE TABLE metadata_items (id INTEGER PRIMARY KEY, extra_data TEXT, guid TEXT,
            title TEXT, metadata_type INTEGER, library_section_id INTEGER, created_at TEXT);
        CREATE TABLE media_items (id INTEGER PRIMARY KEY, deleted_at TEXT, duration INTEGER,
            metadata_item_id INTEGER, proxy_type INTEGER);
        CREATE TABLE media_parts (id INTEGER PRIMARY KEY, media_item_id INTEGER, file TEXT,
            deleted_at TEXT, hash TEXT, size INTEGER, updated_at TEXT);
        CREATE TABLE media_streams (id INTEGER PRIMARY KEY, media_part_id INTEGER,
            stream_type_id INTEGER, "index" INTEGER, codec TEXT, extra_data TEXT,
            updated_at TEXT, created_at TEXT, channels INTEGER);
    """)
    con.execute("INSERT INTO library_sections VALUES (1, 'Movies', 1)")
    con.execute("INSERT INTO library_sections VALUES (2, 'TV Shows', 2)")
    # Movie, unanalyzed, 2 audio streams
    con.execute("INSERT INTO metadata_items VALUES (10, '{}', 'g1', 'Movie A', 1, 1, '')")
    con.execute("INSERT INTO media_items VALUES (100, NULL, 7200000, 10, 0)")
    con.execute("INSERT INTO media_parts VALUES (1000, 100, '/data/Movie A.mkv', NULL, 'h1', 1, '')")
    con.execute("INSERT INTO media_streams VALUES (1, 1000, 2, 1, 'AAC', '{}', '', '', 6)")
    con.execute("INSERT INTO media_streams VALUES (2, 1000, 2, 2, 'AC3', '{}', '', '', 6)")
    # Episode, already analyzed
    con.execute("INSERT INTO metadata_items VALUES (11, '{}', 'g2', 'Show S01E01', 4, 2, '')")
    con.execute("INSERT INTO media_items VALUES (101, NULL, 3600000, 11, 0)")
    con.execute("INSERT INTO media_parts VALUES (1001, 101, '/data/ep.mkv', NULL, 'h2', 1, '')")
    con.execute(f"INSERT INTO media_streams VALUES (3, 1001, 2, 1, 'AAC', '{ANALYZED}', '', '', 2)")
    # Deleted part -> excluded
    con.execute("INSERT INTO metadata_items VALUES (12, '{}', 'g3', 'Gone', 1, 1, '')")
    con.execute("INSERT INTO media_items VALUES (102, NULL, 1000, 12, 0)")
    con.execute("INSERT INTO media_parts VALUES (1002, 102, '/data/gone.mkv', '2024-01-01', 'h3', 1, '')")
    con.execute("INSERT INTO media_streams VALUES (4, 1002, 2, 1, 'AAC', '{}', '', '', 2)")
    # Subtitle stream on Movie A -> excluded (stream_type_id 3)
    con.execute("INSERT INTO media_streams VALUES (5, 1000, 3, 0, 'SRT', '{}', '', '', 0)")
    con.commit()
    yield con
    con.close()


def test_scan_finds_pending_and_analyzed(plex_db):
    streams, summary = scanner.scan(plex_db)
    assert summary["total_audio"] == 3
    assert summary["analyzed_in_plex"] == 1
    assert summary["pending"] == 2
    assert {s["stream_id"] for s in streams} == {1, 2}
    assert streams[0]["title"] == "Movie A"
    assert streams[0]["library"] == "Movies"
    assert streams[0]["file"] == "/data/Movie A.mkv"
    assert streams[0]["part_hash"] == "h1"
    assert summary["per_library"]["Movies"]["pending"] == 2
    assert summary["per_library"]["TV Shows"]["analyzed"] == 1


def test_scan_library_filter(plex_db):
    streams, summary = scanner.scan(plex_db, library_names=["TV Shows"])
    assert summary["total_audio"] == 1
    assert summary["pending"] == 0
    assert streams == []


def test_scan_rating_key_filter(plex_db):
    streams, summary = scanner.scan(plex_db, rating_keys=[10])
    assert {s["stream_id"] for s in streams} == {1, 2}


def test_scan_skips_undecodable(plex_db, tmp_path):
    con = undecodable.connect(str(tmp_path / "u.db"))
    try:
        undecodable.mark_undecodable(con, "h1", 1, "AAC", "ffid")
        streams, summary = scanner.scan(
            plex_db, undecodable_con=con, ffmpeg_identity="ffid")
        assert {s["stream_id"] for s in streams} == {2}
        assert summary["undecodable_skipped"] == 1
        assert summary["pending"] == 1
    finally:
        con.close()


def test_scan_broken_db_returns_empty():
    con = sqlite3.connect(":memory:")  # no tables at all
    streams, summary = scanner.scan(con)
    assert streams == []
    assert summary["total_audio"] == 0
    con.close()
