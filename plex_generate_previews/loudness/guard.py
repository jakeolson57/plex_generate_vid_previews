"""Safety gates for the loudness write path (Phase 2).

Nothing here writes to Plex. Every function either returns a verified
fact or raises :class:`GuardError`. The publish path runs these before
opening any write transaction:

1. **Plex version gate** — only the schema-verified 1.43.4.x / 1.43.5.x
   releases may receive writes. The version list is a constant that is
   easy to extend after a schema re-verification.
2. **Server identity** — the connected server's ``machineIdentifier``
   (from ``/identity``) must match ``ProcessedMachineIdentifier`` in
   ``Preferences.xml``, proving this database belongs to this server.
3. **WAL mode** — the live database must be in WAL mode; refuses
   network-mounted or copied databases that aren't.
"""
import os
import re
import xml.etree.ElementTree as ET
from urllib import request as urlrequest
from urllib.error import URLError

from loguru import logger

from .errors import GuardError

# Plex Media Server releases whose library schema was verified for
# loudness writes (major.minor.patch prefix match; build suffix ignored).
TESTED_PMS_VERSIONS = ("1.43.4", "1.43.5")

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def is_tested_pms_version(version):
    """True when ``version`` is one of the schema-verified releases."""
    if not version:
        return False
    match = _VERSION_RE.match(str(version).strip())
    if not match:
        return False
    return ".".join(match.groups()) in TESTED_PMS_VERSIONS


def get_server_identity(plex_url, plex_token, timeout_s=10):
    """Query Plex ``/identity``. Returns (version, machine_identifier).

    Raises GuardError when Plex cannot be reached or the reply is
    unusable — the write path must not proceed on an unknown server.
    """
    if not plex_url:
        raise GuardError("Plex URL is not configured; cannot verify server identity")
    url = plex_url.rstrip("/") + "/identity"
    req = urlrequest.Request(url)
    if plex_token:
        req.add_header("X-Plex-Token", plex_token)
    try:
        with urlrequest.urlopen(req, timeout=timeout_s) as resp:
            payload = resp.read()
    except (URLError, OSError, TimeoutError) as e:
        raise GuardError(f"could not reach Plex /identity at {plex_url}: {e}")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as e:
        raise GuardError(f"Plex /identity returned unparseable XML: {e}")
    version = root.get("version")
    machine_id = root.get("machineIdentifier")
    if not version or not machine_id:
        raise GuardError("Plex /identity reply is missing version/machineIdentifier")
    return version, machine_id


def read_processed_machine_identifier(plex_config_folder):
    """Read ProcessedMachineIdentifier from Preferences.xml."""
    prefs_path = os.path.join(plex_config_folder, "Preferences.xml")
    if not os.path.exists(prefs_path):
        raise GuardError(f"Preferences.xml not found at {prefs_path}")
    try:
        root = ET.parse(prefs_path).getroot()
    except ET.ParseError as e:
        raise GuardError(f"Preferences.xml is unparseable: {e}")
    identifier = root.get("ProcessedMachineIdentifier")
    if not identifier:
        raise GuardError("Preferences.xml has no ProcessedMachineIdentifier")
    return identifier


def verify_server_identity(plex_url, plex_token, plex_config_folder):
    """Verify version gate + identity match. Returns (version, machine_id).

    Refuses when the server is unreachable, the identity does not match
    the local Preferences.xml (wrong database), or the Plex version is
    not one of the schema-verified releases.
    """
    version, machine_id = get_server_identity(plex_url, plex_token)
    prefs_id = read_processed_machine_identifier(plex_config_folder)
    if machine_id != prefs_id:
        raise GuardError(
            "Plex machineIdentifier does not match Preferences.xml "
            "(wrong database for this server?)"
        )
    if not is_tested_pms_version(version):
        raise GuardError(
            f"Plex version {version} is not schema-verified for loudness "
            f"writes (verified: {', '.join(TESTED_PMS_VERSIONS)}). "
            "Refusing to write until the schema is re-verified."
        )
    logger.info(f"loudness guard: Plex {version} identity verified")
    return version, machine_id


def check_db_file(db_path):
    """Basic liveness checks on the database file itself."""
    if not db_path or not os.path.exists(db_path):
        raise GuardError(f"Plex database not found at: {db_path}")
    if not os.path.isfile(db_path):
        raise GuardError(f"Plex database path is not a file: {db_path}")
    if os.path.getsize(db_path) == 0:
        raise GuardError(f"Plex database is empty: {db_path}")


def check_wal_mode(con):
    """Require WAL journal mode on the live connection."""
    try:
        row = con.execute("PRAGMA journal_mode").fetchone()
    except Exception as e:
        raise GuardError(f"could not read journal_mode: {e}")
    mode = (row[0] if row else "") or ""
    if mode.lower() != "wal":
        raise GuardError(
            f"Plex database journal_mode is {mode!r}, not 'wal' — refusing "
            "writes (copied or network-mounted databases are not safe to write)"
        )


def database_identity(db_path):
    """(device, inode) identifying the exact database file.

    Journal entries bind to this identity; undo refuses a database
    file that is not the one the writes were made against.
    """
    st = os.stat(db_path)
    return (st.st_dev, st.st_ino)
