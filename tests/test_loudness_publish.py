"""Tests for loudness publish orchestration (publish_measured)."""
import os
import sqlite3

import pytest

from plex_generate_previews.loudness import publish as loudness_publish
from plex_generate_previews.loudness.errors import GuardError
from plex_generate_previews.loudness.extra_data import decode_extra_data
from plex_generate_previews.loudness.plex_db import (
    fingerprint_file,
    read_stream,
)
from tests.loudness_phase2_fixtures import (
    FIELDS,
    make_media_file,
    make_plex_config_folder,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fake Plex config folder + media file + patched gates."""
    config_folder, db_path = make_plex_config_folder(tmp_path)
    media = make_media_file(tmp_path)
    journal = str(tmp_path / "loudness-writes.jsonl")
    # gates that need a live Plex / ffmpeg are stubbed here; the
    # loudnorm-presence check is covered by test_publish_refuses_no_loudnorm
    monkeypatch.setattr(
        loudness_publish, "verify_server_identity",
        lambda url, token, folder: ("1.43.5.10737", "machine-1"))
    monkeypatch.setattr(
        "plex_generate_previews.loudness.analyze.has_loudnorm_filter",
        lambda ffmpeg="ffmpeg": True)
    return {"config_folder": config_folder, "db": db_path,
            "media": media, "journal": journal, "tmp": tmp_path}


def _row(env, stream_id=1, fingerprint=True, **overrides):
    row = {
        "plex_stream_id": stream_id,
        "title": "Movie A",
        "file_path": "/data/movie.mkv",
        "part_hash": "h1",
        "part_size": 1000,
        "ln_loudness": FIELDS["ln:loudness"],
        "ln_peak": FIELDS["ln:peak"],
        "ln_lra": FIELDS["ln:lra"],
        "ln_threshold": FIELDS["ln:threshold"],
        "ln_gain_offset": FIELDS["ln:gainOffset"],
        "fingerprint": fingerprint_file(env["media"]) if fingerprint else None,
    }
    row.update(overrides)
    return row


def _call(env, rows, **kwargs):
    kwargs.setdefault("plex_config_folder", env["config_folder"])
    kwargs.setdefault("plex_url", "http://plex:32400")
    kwargs.setdefault("plex_token", "tok")
    kwargs.setdefault("journal_path", env["journal"])
    # /data maps to the tmp dir, where movie.mkv lives
    kwargs.setdefault("path_mappings", [("/data", str(env["tmp"]))])
    return loudness_publish.publish_measured(rows, **kwargs)


def _extra(env, stream_id=1):
    con = sqlite3.connect(env["db"])
    try:
        return decode_extra_data(read_stream(con, stream_id).extra_data)
    finally:
        con.close()


def test_publish_happy_path(env):
    summary = _call(env, [_row(env)])
    assert summary["published"] == 1
    assert summary["failed"] == 0
    assert summary["results"][0]["status"] == "written"
    assert _extra(env)["ln:loudness"] == "-16.32"
    assert os.path.exists(env["journal"])


def test_publish_dry_run_writes_nothing(env):
    summary = _call(env, [_row(env)], dry_run=True)
    assert summary["published"] == 1  # would-write counts as published
    assert summary["results"][0]["status"] == "would-write"
    assert _extra(env) == {}
    assert not os.path.exists(env["journal"])


def test_publish_dry_run_reports_already(env):
    _call(env, [_row(env)])
    summary = _call(env, [_row(env)], dry_run=True)
    assert summary["already"] == 1
    assert summary["results"][0]["status"] == "already"


def test_publish_second_call_is_already(env):
    _call(env, [_row(env)])
    summary = _call(env, [_row(env)])
    assert summary["already"] == 1
    assert summary["published"] == 0


def test_publish_refuses_missing_fingerprint(env):
    summary = _call(env, [_row(env, fingerprint=False)])
    assert summary["failed"] == 1
    assert "re-run analyze" in summary["results"][0]["error"]
    assert _extra(env) == {}


def test_publish_failed_fingerprint_mismatch(env):
    row = _row(env)  # fingerprint captured at analysis time
    with open(env["media"], "ab") as fh:
        fh.write(b"changed")
    summary = _call(env, [row])
    assert summary["failed"] == 1
    assert _extra(env) == {}


def test_publish_refuses_no_loudnorm(env, monkeypatch):
    monkeypatch.setattr(
        "plex_generate_previews.loudness.analyze.has_loudnorm_filter",
        lambda ffmpeg="ffmpeg": False)
    with pytest.raises(GuardError, match="no loudnorm filter"):
        _call(env, [_row(env)])


def test_publish_refuses_untested_version(env, monkeypatch):
    monkeypatch.setattr(
        loudness_publish, "verify_server_identity",
        lambda url, token, folder: (_ for _ in ()).throw(
            GuardError("Plex version 1.44.0 not schema-verified")))
    with pytest.raises(GuardError, match="not schema-verified"):
        _call(env, [_row(env)])


def test_publish_empty_rows(env):
    assert _call(env, []) == {
        "published": 0, "already": 0, "deferred": 0, "failed": 0,
        "dry_run": False, "results": []}


def test_publish_limit(env):
    rows = [_row(env), _row(env, stream_id=3, part_hash="h2",
                           part_size=2000, file_path="/data/ep.mkv")]
    summary = _call(env, rows, limit=1)
    assert len(summary["results"]) == 1
    assert summary["published"] == 1
