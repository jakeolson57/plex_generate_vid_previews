"""Loudness analysis for Plex audio tracks.

Phase 1 (this package): read-only discovery + ffmpeg loudnorm analysis.
No writes to Plex's database — measurements are stored in the app's own
database, ready for the guarded write path (Phase 2).

Measures with the exact filter Plex Media Server uses for its
"Normalize Loudness" feature so the values are in Plex's own format.
"""

ANALYSIS_VERSION = "0.02"

# Plex movie / episode metadata types (metadata_items.metadata_type)
METADATA_TYPE_MOVIE = 1
METADATA_TYPE_EPISODE = 4

# media_streams.stream_type_id for audio
STREAM_TYPE_AUDIO = 2
