"""Publish measured loudness values into Plex's database (Phase 2).

The web endpoint (``POST /api/loudness/publish``) is a thin wrapper
over :func:`publish_measured`. The flow, in order:

1. ffmpeg must have the ``loudnorm`` filter (checked at runtime, so a
   bad image fails loudly instead of writing nothing);
2. server identity + Plex version gate (see ``guard``);
3. one writable connection; WAL + schema checked once;
4. per measured stream: :func:`plex_db.write_stream` — idempotent,
   fingerprint-pinned, journaled, read-back verified;
5. lock contention defers the stream (``deferred``), never fails it.

``dry_run=True`` runs every check and reports what *would* be
written, without touching Plex or the journal — it is the readiness
check for the demo.
"""
from loguru import logger

from . import ANALYSIS_VERSION, analyze as loudness_analyze
from .errors import (
    DatabaseBusyError,
    FingerprintMismatchError,
    GuardError,
    LoudnessWriteError,
)
from .extra_data import has_loudness
from .guard import check_wal_mode, verify_server_identity
from .paths import translate_plex_path
from .plex_db import check_schema, read_stream, verify_fingerprint, write_stream
from ..plex_client import _get_plex_db_path, open_plex_db_writable


def publish_measured(rows, *, plex_config_folder, plex_url, plex_token,
                     ffmpeg="ffmpeg", journal_path=None,
                     path_mappings=(), legacy_remote="", legacy_local="",
                     limit=10, dry_run=False):
    """Publish measured streams to Plex. Returns a summary dict.

    ``rows``: dicts with plex_stream_id, file_path (Plex-side),
    part_hash, part_size, ln_* fields, and fingerprint (a
    ``SourceFingerprint`` or None). Rows are consumed in order up to
    ``limit``.
    """
    results = []
    summary = {"published": 0, "already": 0, "deferred": 0, "failed": 0,
               "dry_run": dry_run, "results": results}
    if not rows:
        return summary

    if not loudness_analyze.has_loudnorm_filter(ffmpeg):
        raise GuardError(
            f"ffmpeg ({ffmpeg}) has no loudnorm filter — refusing writes"
        )
    verify_server_identity(plex_url, plex_token, plex_config_folder)

    plex_db = open_plex_db_writable(plex_config_folder)
    try:
        check_wal_mode(plex_db)
        check_schema(plex_db)
        for row in rows[:limit]:
            stream_id = row["plex_stream_id"]
            outcome = {"stream_id": stream_id, "title": row.get("title")}
            try:
                local_path = translate_plex_path(
                    row.get("file_path") or "", path_mappings,
                    legacy_remote, legacy_local)
                fields = {
                    "ln:loudness": row["ln_loudness"],
                    "ln:peak": row["ln_peak"],
                    "ln:lra": row["ln_lra"],
                    "ln:threshold": row["ln_threshold"],
                    "ln:gainOffset": row["ln_gain_offset"],
                    "ln:loudnessAnalysisVersion": ANALYSIS_VERSION,
                }
                if dry_run:
                    stream = read_stream(plex_db, stream_id)
                    if stream is None:
                        raise LoudnessWriteError(
                            f"stream {stream_id} not found in Plex DB")
                    if has_loudness(stream.extra_data):
                        outcome["status"] = "already"
                    else:
                        if row.get("fingerprint") is None:
                            raise FingerprintMismatchError(
                                "no source fingerprint recorded — re-run analyze")
                        verify_fingerprint(local_path, row["fingerprint"])
                        outcome["status"] = "would-write"
                else:
                    res = write_stream(
                        plex_db,
                        stream_id=stream_id,
                        fields=fields,
                        journal_path=journal_path,
                        db_path=_get_plex_db_path(plex_config_folder),
                        expected_part_hash=row.get("part_hash"),
                        expected_part_size=row.get("part_size"),
                        local_path=local_path,
                        fingerprint=row.get("fingerprint"),
                    )
                    outcome["status"] = res["status"]
            except DatabaseBusyError as e:
                outcome["status"] = "deferred"
                outcome["error"] = str(e)[:200]
            except (FingerprintMismatchError, GuardError, LoudnessWriteError) as e:
                outcome["status"] = "failed"
                outcome["error"] = str(e)[:200]
            if outcome["status"] in ("written", "would-write"):
                summary["published"] += 1
            elif outcome["status"] == "already":
                summary["already"] += 1
            elif outcome["status"] == "deferred":
                summary["deferred"] += 1
            else:
                summary["failed"] += 1
            results.append(outcome)
    finally:
        plex_db.close()
    logger.info(
        f"loudness publish (dry_run={dry_run}): "
        f"{summary['published']} published, {summary['already']} already, "
        f"{summary['deferred']} deferred, {summary['failed']} failed")
    return summary
