"""Tests for the loudness guard (version gate, identity, WAL)."""
import io
import os
import sqlite3

import pytest

from plex_generate_previews.loudness import guard
from plex_generate_previews.loudness.errors import GuardError


def test_version_gate():
    assert guard.is_tested_pms_version("1.43.4.1000") is True
    assert guard.is_tested_pms_version("1.43.5.10737-abc") is True
    assert guard.is_tested_pms_version("1.43.4") is True
    assert guard.is_tested_pms_version("1.42.0.1000") is False
    assert guard.is_tested_pms_version("1.44.0.1") is False
    assert guard.is_tested_pms_version("1.43.6.1") is False
    assert guard.is_tested_pms_version("") is False
    assert guard.is_tested_pms_version(None) is False
    assert guard.is_tested_pms_version("garbage") is False


def test_verify_identity_ok(tmp_path, monkeypatch):
    prefs = tmp_path / "Preferences.xml"
    prefs.write_text('<Preferences ProcessedMachineIdentifier="machine-1" />')
    monkeypatch.setattr(
        guard, "get_server_identity",
        lambda url, token, timeout_s=10: ("1.43.5.10737", "machine-1"))
    version, machine = guard.verify_server_identity(
        "http://plex:32400", "tok", str(tmp_path))
    assert version == "1.43.5.10737"
    assert machine == "machine-1"


def test_verify_identity_mismatch(tmp_path, monkeypatch):
    prefs = tmp_path / "Preferences.xml"
    prefs.write_text('<Preferences ProcessedMachineIdentifier="machine-2" />')
    monkeypatch.setattr(
        guard, "get_server_identity",
        lambda url, token, timeout_s=10: ("1.43.5.10737", "machine-1"))
    with pytest.raises(GuardError, match="does not match"):
        guard.verify_server_identity("http://plex:32400", "tok", str(tmp_path))


def test_verify_untested_version(tmp_path, monkeypatch):
    prefs = tmp_path / "Preferences.xml"
    prefs.write_text('<Preferences ProcessedMachineIdentifier="machine-1" />')
    monkeypatch.setattr(
        guard, "get_server_identity",
        lambda url, token, timeout_s=10: ("1.44.0.5", "machine-1"))
    with pytest.raises(GuardError, match="not schema-verified"):
        guard.verify_server_identity("http://plex:32400", "tok", str(tmp_path))


def test_verify_missing_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(
        guard, "get_server_identity",
        lambda url, token, timeout_s=10: ("1.43.5.1", "machine-1"))
    with pytest.raises(GuardError, match="Preferences.xml not found"):
        guard.verify_server_identity("http://plex:32400", "tok", str(tmp_path))


def test_get_server_identity_unreachable(monkeypatch):
    import urllib.error
    monkeypatch.setattr(
        guard.urlrequest, "urlopen",
        lambda req, timeout=10: (_ for _ in ()).throw(
            urllib.error.URLError("boom")))
    with pytest.raises(GuardError, match="could not reach"):
        guard.get_server_identity("http://plex:32400", "tok")


def test_get_server_identity_parses_xml(monkeypatch):
    xml = (b'<MediaContainer machineIdentifier="abc-123" version="1.43.4.99" '
           b'friendlyName="Plex" />')
    monkeypatch.setattr(
        guard.urlrequest, "urlopen",
        lambda req, timeout=10: io.BytesIO(xml))
    assert guard.get_server_identity("http://plex:32400", "tok") == (
        "1.43.4.99", "abc-123")


def test_check_wal_mode(tmp_path):
    wal_path = str(tmp_path / "wal.db")
    con = sqlite3.connect(wal_path)
    con.execute("PRAGMA journal_mode=WAL")
    guard.check_wal_mode(con)
    con.close()
    # delete-mode database is refused
    del_path = str(tmp_path / "del.db")
    con = sqlite3.connect(del_path)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    with pytest.raises(GuardError, match="not 'wal'"):
        guard.check_wal_mode(con)
    con.close()


def test_database_identity(tmp_path):
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    assert guard.database_identity(str(a)) == guard.database_identity(str(a))
    assert guard.database_identity(str(a)) != guard.database_identity(str(b))
    assert guard.database_identity(str(a))[0] == os.stat(str(a)).st_dev


def test_check_db_file(tmp_path):
    with pytest.raises(GuardError, match="not found"):
        guard.check_db_file(str(tmp_path / "nope.db"))
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(GuardError, match="empty"):
        guard.check_db_file(str(empty))
    ok = tmp_path / "ok.db"
    ok.write_bytes(b"data")
    guard.check_db_file(str(ok))  # no raise
