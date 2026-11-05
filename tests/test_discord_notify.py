"""Tests for Discord webhook notifications (notify.py + scheduler hooks)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

from plex_generate_previews.web import scheduler as scheduler_module
from plex_generate_previews.web import notify as notify_module
from plex_generate_previews.web.models import AppSettings, MediaItem, PreviewStatus

WEBHOOK = "https://discord.com/api/webhooks/fake/test"


@pytest.fixture
def engine_fx():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def reset_debounce(monkeypatch):
    monkeypatch.setattr(notify_module, '_last_sent_at', 0.0)


def _item(**kwargs):
    base = dict(id=1, title="Test Movie", media_type="movie", rating_key="1",
                library_name="Movies", status=PreviewStatus.QUEUED)
    base.update(kwargs)
    return MediaItem(**base)


def _mgr(monkeypatch, engine):
    monkeypatch.setattr(scheduler_module, 'engine', engine)
    return scheduler_module.DbProgressManager()


# --- notify.send_discord ---

def test_send_discord_no_url_sends_nothing():
    with patch.object(notify_module.requests, 'post') as mock_post:
        assert notify_module.send_discord(None, "hi") is False
        assert notify_module.send_discord("", "hi") is False
    mock_post.assert_not_called()


def test_send_discord_posts_content():
    with patch.object(notify_module.requests, 'post') as mock_post:
        mock_post.return_value = SimpleNamespace(status_code=204)
        assert notify_module.send_discord(WEBHOOK, "hello") is True
        # Fire-and-forget runs on a daemon thread; give it a beat.
        import time as _t
        for _ in range(50):
            if mock_post.called:
                break
            _t.sleep(0.02)
        assert mock_post.called
        args, kwargs = mock_post.call_args
        assert args[0] == WEBHOOK
        assert kwargs["json"] == {"content": "hello"}
        assert kwargs["timeout"] == 5


def test_send_discord_debounce():
    with patch.object(notify_module.requests, 'post') as mock_post:
        mock_post.return_value = SimpleNamespace(status_code=204)
        assert notify_module.send_discord(WEBHOOK, "one", debounce_seconds=60) is True
        assert notify_module.send_discord(WEBHOOK, "two", debounce_seconds=60) is False
        assert notify_module.send_discord(WEBHOOK, "three", debounce_seconds=0) is True


def test_send_discord_never_raises():
    with patch.object(notify_module.requests, 'post', side_effect=RuntimeError("boom")):
        assert notify_module.send_discord(WEBHOOK, "hi") is True  # dispatched; thread absorbs error
    with patch.object(notify_module.requests, 'post') as mock_post:
        mock_post.return_value = SimpleNamespace(status_code=500)
        assert notify_module.send_discord(WEBHOOK, "hi") is True


# --- scheduler._maybe_notify ---

def _settings_with_webhook(engine):
    with Session(engine) as s:
        s.add(AppSettings(id=1, discord_webhook_url=WEBHOOK))
        s.commit()


def test_maybe_notify_no_webhook_configured(engine_fx, monkeypatch):
    mgr = _mgr(monkeypatch, engine_fx)
    with Session(engine_fx) as s:
        s.add(AppSettings(id=1))
        s.commit()
    with Session(engine_fx) as s:
        item = _item(status=PreviewStatus.FAILED, error_message="x")
        with patch.object(scheduler_module, 'send_discord') as mock_send:
            mgr._maybe_notify(s, item, PreviewStatus.PROCESSING)
    mock_send.assert_not_called()


def test_maybe_notify_failure_pings_once(engine_fx, monkeypatch):
    _settings_with_webhook(engine_fx)
    mgr = _mgr(monkeypatch, engine_fx)
    with Session(engine_fx) as s:
        item = _item(status=PreviewStatus.FAILED, error_message="ffmpeg exploded")
        with patch.object(scheduler_module, 'send_discord') as mock_send:
            mgr._maybe_notify(s, item, PreviewStatus.PROCESSING)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == WEBHOOK
        assert "❌" in args[1] and "Test Movie" in args[1] and "ffmpeg exploded" in args[1]
        assert kwargs["debounce_seconds"] == 60
    # Repeat failure updates don't re-ping.
    with Session(engine_fx) as s:
        item = _item(status=PreviewStatus.FAILED, error_message="ffmpeg exploded")
        with patch.object(scheduler_module, 'send_discord') as mock_send:
            mgr._maybe_notify(s, item, PreviewStatus.FAILED)
    mock_send.assert_not_called()


def test_maybe_notify_slow_failed_pings(engine_fx, monkeypatch):
    _settings_with_webhook(engine_fx)
    mgr = _mgr(monkeypatch, engine_fx)
    with Session(engine_fx) as s:
        item = _item(status=PreviewStatus.SLOW_FAILED, error_message="too slow, aborting")
        with patch.object(scheduler_module, 'send_discord') as mock_send:
            mgr._maybe_notify(s, item, PreviewStatus.PROCESSING)
    mock_send.assert_called_once()


def test_maybe_notify_backlog_clear(engine_fx, monkeypatch):
    _settings_with_webhook(engine_fx)
    mgr = _mgr(monkeypatch, engine_fx)
    # Only the completing item exists; after it completes nothing is pending.
    with Session(engine_fx) as s:
        s.add(_item(id=1, status=PreviewStatus.COMPLETED))
        s.commit()
    with Session(engine_fx) as s:
        item = _item(id=1, status=PreviewStatus.COMPLETED)
        with patch.object(scheduler_module, 'send_discord') as mock_send:
            mgr._maybe_notify(s, item, PreviewStatus.PROCESSING)
        mock_send.assert_called_once()
        assert "✅" in mock_send.call_args[0][1]


def test_maybe_notify_no_ping_when_items_remain(engine_fx, monkeypatch):
    _settings_with_webhook(engine_fx)
    mgr = _mgr(monkeypatch, engine_fx)
    with Session(engine_fx) as s:
        s.add(_item(id=1, status=PreviewStatus.COMPLETED))
        s.add(_item(id=2, status=PreviewStatus.QUEUED))
        s.commit()
    with Session(engine_fx) as s:
        item = _item(id=1, status=PreviewStatus.COMPLETED)
        with patch.object(scheduler_module, 'send_discord') as mock_send:
            mgr._maybe_notify(s, item, PreviewStatus.PROCESSING)
    mock_send.assert_not_called()


def test_maybe_notify_no_ping_on_repeat_completion(engine_fx, monkeypatch):
    _settings_with_webhook(engine_fx)
    mgr = _mgr(monkeypatch, engine_fx)
    with Session(engine_fx) as s:
        item = _item(status=PreviewStatus.COMPLETED)
        with patch.object(scheduler_module, 'send_discord') as mock_send:
            mgr._maybe_notify(s, item, PreviewStatus.COMPLETED)
    mock_send.assert_not_called()
