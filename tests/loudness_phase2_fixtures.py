"""Shared fixtures for the loudness Phase 2 tests (guarded writes).

Builds a synthetic Plex library database as a real file in WAL mode
(WAL checks and device/inode identity binding need a real file, not
:memory:), plus a media file for fingerprint checks.
"""
import json
import os
import sqlite3

ANALYZED = json.dumps({
    "ln:loudness": "-16.1", "ln:peak": "-1.0", "ln:lra": "7.0",
    "ln:threshold": "-34.0", "ln:gainOffset": "-0.1",
    "ln:loudnessAnalysisVersion": "0.02",
})

FIELDS = {
    "ln:loudness": "-16.32",
    "ln:peak": "-1.24",
    "ln:lra": "7.12",
    "ln:threshold": "-34.21",
    "ln:gainOffset": "-0.32",
    "ln:loudnessAnalysisVersion": "0.02",
}

PLEX_SCHEMA = """
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
"""


def seed_plex_db(con, movie_file="/data/movie.mkv"):
    """Seed a movie (unanalyzed, 1 audio stream) + an analyzed episode."""
    con.executescript(PLEX_SCHEMA)
    con.execute("INSERT INTO library_sections VALUES (1, 'Movies', 1)")
    con.execute("INSERT INTO library_sections VALUES (2, 'TV Shows', 2)")
    con.execute("INSERT INTO metadata_items VALUES (10, '{}', 'g1', 'Movie A', 1, 1, '')")
    con.execute("INSERT INTO media_items VALUES (100, NULL, 7200000, 10, 0)")
    con.execute(
        "INSERT INTO media_parts VALUES (1000, 100, ?, NULL, 'h1', 1000, '')",
        (movie_file,))
    con.execute("INSERT INTO media_streams VALUES (1, 1000, 2, 1, 'AAC', '{}', '', '', 6)")
    con.execute("INSERT INTO metadata_items VALUES (11, '{}', 'g2', 'Show S01E01', 4, 2, '')")
    con.execute("INSERT INTO media_items VALUES (101, NULL, 3600000, 11, 0)")
    con.execute("INSERT INTO media_parts VALUES (1001, 101, '/data/ep.mkv', NULL, 'h2', 2000, '')")
    con.execute(
        "INSERT INTO media_streams VALUES (3, 1001, 2, 1, 'AAC', ?, '', '', 2)",
        (ANALYZED,))
    con.commit()


def make_plex_db(path, movie_file="/data/movie.mkv"):
    """Create a WAL-mode Plex DB file at ``path`` (parents created)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    seed_plex_db(con, movie_file=movie_file)
    con.close()
    return path


def make_plex_config_folder(tmp_path, movie_file="/data/movie.mkv"):
    """Create a fake Plex config folder with the library DB inside."""
    folder = tmp_path / "plex-config"
    db_path = folder / "Plug-in Support" / "Databases" / "com.plexapp.plugins.library.db"
    make_plex_db(str(db_path), movie_file=movie_file)
    return str(folder), str(db_path)


def make_media_file(tmp_path, name="movie.mkv", content=b"fake-media-bytes"):
    """Create a media file; returns its path."""
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)
