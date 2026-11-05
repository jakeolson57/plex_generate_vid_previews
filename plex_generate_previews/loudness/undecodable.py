"""Remembers audio streams ffmpeg cannot decode, so they are skipped.

Keyed by (part_hash, stream_index, codec, ffmpeg_identity): a new ffmpeg
binary gets a fresh chance. Self-contained SQLite store; the path is
chosen by the caller (the web app keeps it next to its database).
"""
import sqlite3
import threading
import time

from loguru import logger

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS undecodable (
    part_hash TEXT NOT NULL,
    stream_index INTEGER NOT NULL,
    codec TEXT NOT NULL,
    ffmpeg_identity TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (part_hash, stream_index, codec, ffmpeg_identity)
)
"""


def connect(path):
    """Open (and initialize) the undecodable store."""
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute(_SCHEMA)
    con.commit()
    return con


def is_undecodable(con, part_hash, stream_index, codec, ffmpeg_identity):
    """True if this exact stream was already found undecodable."""
    if not part_hash:
        return False
    with _lock:
        row = con.execute(
            "SELECT 1 FROM undecodable WHERE part_hash=? AND stream_index=? "
            "AND codec=? AND ffmpeg_identity=?",
            (part_hash, stream_index, codec or "", ffmpeg_identity or ""),
        ).fetchone()
    return row is not None


def mark_undecodable(con, part_hash, stream_index, codec, ffmpeg_identity):
    """Record a stream as undecodable. Never raises."""
    if not part_hash:
        return
    try:
        with _lock:
            con.execute(
                "INSERT OR IGNORE INTO undecodable "
                "(part_hash, stream_index, codec, ffmpeg_identity, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (part_hash, stream_index, codec or "",
                 ffmpeg_identity or "", time.time()),
            )
            con.commit()
    except Exception as e:
        logger.warning(f"loudness: failed to record undecodable stream: {e}")


def clear_for_ffmpeg(con, ffmpeg_identity):
    """Forget marks made by older ffmpeg builds (e.g. after an upgrade)."""
    with _lock:
        con.execute(
            "DELETE FROM undecodable WHERE ffmpeg_identity != ?",
            (ffmpeg_identity or "",),
        )
        con.commit()
