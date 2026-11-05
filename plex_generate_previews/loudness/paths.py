"""Standalone Plex→local path translation for loudness ffmpeg runs.

Mirrors Scheduler._translate_plex_path without importing the scheduler:
mappings are passed in as (remote, local) prefix pairs, longest match wins.
"""


def translate_plex_path(plex_path, mappings=(), legacy_remote="", legacy_local=""):
    """Translate a Plex-side path to the local path ffmpeg can read."""
    if not plex_path:
        return plex_path
    for remote, local in sorted(mappings or [], key=lambda x: len(x[0]), reverse=True):
        if remote and plex_path.startswith(remote):
            return local + plex_path[len(remote):]
    if legacy_remote and legacy_local and plex_path.startswith(legacy_remote):
        return legacy_local + plex_path[len(legacy_remote):]
    return plex_path
