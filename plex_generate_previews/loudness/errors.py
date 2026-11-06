"""Error types for the guarded loudness write path (Phase 2).

These are deliberately distinct from the analysis errors in
``analyze.py``: a write failure must never be confused with a
measurement failure, and "defer, don't fail" outcomes (a busy Plex
database) get their own type so callers can retry later instead of
marking the item failed.
"""


class LoudnessWriteError(Exception):
    """Base error for the guarded write path."""


class GuardError(LoudnessWriteError):
    """A safety gate refused the write (version, identity, WAL, schema)."""


class DatabaseBusyError(LoudnessWriteError):
    """Plex (or another writer) holds the write lock.

    This is a *defer* signal, not a failure: the item should be
    retried later, never marked failed because of it.
    """


class FingerprintMismatchError(LoudnessWriteError):
    """The source file or Plex part changed since analysis. Abort."""


class JournalError(LoudnessWriteError):
    """The write journal could not be written or read."""
