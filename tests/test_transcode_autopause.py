"""Tests for transcode auto-pause (pause_on_transcode setting)."""
from types import SimpleNamespace
import pytest
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool
from plex_generate_previews.web import scheduler as scheduler_module
from plex_generate_previews.web.models import AppSettings

Scheduler = scheduler_module.Scheduler


@pytest.fixture
def sched(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(scheduler_module, 'engine', engine)
    s = Scheduler()
    yield s, engine
    engine.dispose()


def _db_with_setting(engine, **kwargs):
    with Session(engine) as session:
        session.add(AppSettings(id=1, **kwargs))
        session.commit()


def _session_item(key, state="playing", transcoding=False):
    player = SimpleNamespace(state=state, title="Living Room TV")
    return SimpleNamespace(
        type="movie",
        ratingKey=str(key),
        sessionKey=f"s{key}",
        players=[player],
        title=f"Movie {key}",
        usernames=["jake"],
        viewOffset=0,
        duration=1000,
        transcodeSession=SimpleNamespace() if transcoding else None,
    )


def _fake_plex(*items):
    return SimpleNamespace(sessions=lambda: list(items))


def test_transcode_flag_detected_in_playback(sched):
    s, _ = sched
    plex = _fake_plex(
        _session_item(1, transcoding=True),
        _session_item(2, transcoding=False),
    )
    snapshot = s.get_active_playback(plex)
    assert snapshot["available"] is True
    by_key = {entry["rating_key"]: entry for entry in snapshot["sessions"]}
    assert by_key[1]["transcoding"] is True
    assert by_key[2]["transcoding"] is False


def test_autopause_engages_on_playing_transcode(sched, monkeypatch):
    s, engine = sched
    _db_with_setting(engine, pause_on_transcode=True)
    monkeypatch.setattr(s, "get_active_playback", lambda plex=None: {
        "available": True, "sessions": [{"transcoding": True, "state": "playing"}], "updated_at": "x"})
    s._update_transcode_pause()
    assert s.auto_paused is True


def test_autopause_ignores_paused_player_transcode(sched, monkeypatch):
    s, engine = sched
    _db_with_setting(engine, pause_on_transcode=True)
    monkeypatch.setattr(s, "get_active_playback", lambda plex=None: {
        "available": True, "sessions": [{"transcoding": True, "state": "paused"}], "updated_at": "x"})
    s._update_transcode_pause()
    assert s.auto_paused is False


def test_autopause_stays_off_when_setting_disabled(sched, monkeypatch):
    s, engine = sched
    _db_with_setting(engine)  # pause_on_transcode defaults False
    monkeypatch.setattr(s, "get_active_playback", lambda plex=None: {
        "available": True, "sessions": [{"transcoding": True, "state": "playing"}], "updated_at": "x"})
    s._update_transcode_pause()
    assert s.auto_paused is False


def test_autopause_fails_open_when_plex_unavailable(sched, monkeypatch):
    s, engine = sched
    _db_with_setting(engine, pause_on_transcode=True)
    monkeypatch.setattr(s, "get_active_playback", lambda plex=None: {
        "available": False, "sessions": [], "updated_at": None})
    s._update_transcode_pause()
    assert s.auto_paused is False


def test_autopause_hysteresis_before_resuming(sched, monkeypatch):
    s, engine = sched
    _db_with_setting(engine, pause_on_transcode=True)
    clear = {"available": True, "sessions": [], "updated_at": "x"}
    monkeypatch.setattr(s, "get_active_playback", lambda plex=None: {
        "available": True, "sessions": [{"transcoding": True, "state": "playing"}], "updated_at": "x"})
    s._update_transcode_pause()
    assert s.auto_paused is True
    monkeypatch.setattr(s, "get_active_playback", lambda plex=None: clear)
    s._update_transcode_pause()
    assert s.auto_paused is True  # 1st clear poll: still paused
    s._update_transcode_pause()
    assert s.auto_paused is True  # 2nd clear poll: still paused
    s._update_transcode_pause()
    assert s.auto_paused is False  # 3rd clear poll: resumed


def test_autopause_clears_when_setting_turned_off(sched, monkeypatch):
    s, engine = sched
    _db_with_setting(engine, pause_on_transcode=True)
    monkeypatch.setattr(s, "get_active_playback", lambda plex=None: {
        "available": True, "sessions": [{"transcoding": True, "state": "playing"}], "updated_at": "x"})
    s._update_transcode_pause()
    assert s.auto_paused is True
    with Session(engine) as session:
        session.get(AppSettings, 1).pause_on_transcode = False
        session.commit()
    s._update_transcode_pause()
    assert s.auto_paused is False
