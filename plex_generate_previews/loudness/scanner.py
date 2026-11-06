"""Loudness discovery: find audio streams needing analysis.

Read-only against Plex's database (via the fork's read-only ``open_plex_db``).
For each audio stream of movies/episodes: skip when Plex already has
loudness measurements (``has_loudness``) or the stream is known
undecodable. Returns work units the analyzer can consume.
"""
import sqlite3

from loguru import logger

from . import METADATA_TYPE_MOVIE, METADATA_TYPE_EPISODE, STREAM_TYPE_AUDIO
from .extra_data import has_loudness
from .undecodable import is_undecodable

# Audio streams of live (non-deleted) parts, movies/episodes only,
# excluding Plex's own optimized versions.
_DISCOVERY_SQL = """
SELECT ms.id, ms."index", ms.codec, ms.extra_data, mi.duration, mp.file,
       md.id, md.guid, md.title,
       mp.id, mi.id, mp.hash, mp.size,
       ms.channels, md.metadata_type, md.library_section_id
FROM media_parts mp
JOIN media_items mi ON mi.id = mp.media_item_id
JOIN metadata_items md ON md.id = mi.metadata_item_id
LEFT JOIN media_streams ms
  ON ms.media_part_id = mp.id
 AND ms.stream_type_id = ?
 AND ms."index" IS NOT NULL
WHERE mp.deleted_at IS NULL
  AND mi.deleted_at IS NULL
  AND md.metadata_type IN (?, ?)
  AND NOT (COALESCE(mi.proxy_type, 0) != 0 AND mp.file LIKE '%/Plex Versions/%')
"""


def _library_sections(plex_db):
    """Return {id: (name, section_type)} for library sections."""
    try:
        rows = plex_db.execute(
            "SELECT id, name, section_type FROM library_sections"
        ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}
    except sqlite3.Error:
        return {}


def scan(plex_db, undecodable_con=None, ffmpeg_identity="",
         library_names=None, rating_keys=None):
    """Discover audio streams needing loudness analysis.

    Args:
        plex_db: read-only sqlite3 connection to Plex's library DB.
        undecodable_con: undecodable store connection (or None to skip).
        ffmpeg_identity: current ffmpeg identity for undecodable keying.
        library_names: optional list of Plex library names to scope to.
        rating_keys: optional list of Plex rating keys (metadata_items.id).

    Returns:
        (streams, summary) where streams is a list of dicts:
        {stream_id, stream_index, codec, file, duration_ms, part_hash,
         metadata_id, title, library}
        and summary is {total_audio, analyzed_in_plex, pending,
                        undecodable_skipped, per_library: {...}}.
    """
    sections = _library_sections(plex_db)
    wanted_sections = None
    if library_names:
        wanted = {n.strip().lower() for n in library_names if n.strip()}
        wanted_sections = {
            sid for sid, (name, _stype) in sections.items()
            if name and name.strip().lower() in wanted
        }

    sql = _DISCOVERY_SQL
    params = [STREAM_TYPE_AUDIO, METADATA_TYPE_MOVIE, METADATA_TYPE_EPISODE]
    if rating_keys:
        placeholders = ",".join("?" for _ in rating_keys)
        sql += f" AND md.id IN ({placeholders})"
        params += list(rating_keys)

    streams = []
    summary = {
        "total_audio": 0,
        "analyzed_in_plex": 0,
        "pending": 0,
        "undecodable_skipped": 0,
        "per_library": {},
    }
    try:
        rows = plex_db.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        logger.warning(f"loudness scan failed: {e}")
        return streams, summary

    for row in rows:
        (stream_id, stream_index, codec, extra_data, duration_ms, file,
         metadata_id, guid, title,
         part_id, item_id, part_hash, part_size,
         channels, metadata_type, section_id) = row
        if stream_id is None:
            continue  # part with no audio streams
        if wanted_sections is not None and section_id not in wanted_sections:
            continue
        lib_name = sections.get(section_id, ("Unknown", None))[0]
        summary["total_audio"] += 1
        summary["per_library"].setdefault(
            lib_name, {"total": 0, "analyzed": 0, "pending": 0})
        summary["per_library"][lib_name]["total"] += 1

        if has_loudness(extra_data):
            summary["analyzed_in_plex"] += 1
            summary["per_library"][lib_name]["analyzed"] += 1
            continue
        if undecodable_con is not None and is_undecodable(
                undecodable_con, part_hash, stream_index, codec, ffmpeg_identity):
            summary["undecodable_skipped"] += 1
            continue
        summary["pending"] += 1
        summary["per_library"][lib_name]["pending"] += 1
        streams.append({
            "stream_id": stream_id,
            "stream_index": stream_index,
            "codec": codec,
            "file": file,
            "duration_ms": duration_ms,
            "part_hash": part_hash,
            "part_size": part_size,
            "metadata_id": metadata_id,
            "title": title,
            "library": lib_name,
        })
    return streams, summary
