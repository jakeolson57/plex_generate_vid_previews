import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

from plex_generate_previews.web import database, infini_cache, main, scheduler as scheduler_module
from plex_generate_previews.web.models import AppSettings, MediaItem, MediaType, PreviewStatus
from plex_generate_previews.web.scheduler import Scheduler


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(scheduler_module, "engine", engine)
    yield engine
    engine.dispose()


def movie(key, **kwargs):
    return MediaItem(id=key, title=f"Movie {key}", library_name="Movies", media_type=MediaType.MOVIE, **kwargs)


def test_scope_filters_initial_and_refill_and_stops_on_setting_change(db, monkeypatch):
    with Session(db) as session:
        session.add(AppSettings(id=1, full_backlog=False))
        session.add(movie(1))
        session.add(movie(2, is_priority=True, priority_score=999))
        session.add(movie(3, status=PreviewStatus.QUEUED))
        session.commit()
    scheduler = Scheduler()
    scheduler.config = SimpleNamespace(gpu_threads=1, cpu_threads=0)
    scheduler.worker_pool = MagicMock()
    monkeypatch.setattr(scheduler, "validate_mount_paths", lambda: (True, []))
    monkeypatch.setattr(scheduler, "_check_rate_limit", lambda: False)
    monkeypatch.setattr(scheduler_module, "plex_server", lambda config: MagicMock())

    def process(items, *args, **kwargs):
        assert [int(row[0]) for row in items] == [2]
        with Session(db) as session:
            item = session.get(MediaItem, 2)
            item.status = PreviewStatus.PROCESSING
            session.add(item)
            session.commit()
        assert kwargs["fetch_more_items"]() == []  # No leakage of ordinary QUEUED work
        with Session(db) as session:
            settings = session.get(AppSettings, 1)
            settings.full_backlog = True
            session.add(settings)
            session.commit()
        assert kwargs["stop_condition"]()
        assert kwargs["fetch_more_items"]()  # Full backlog restores remaining work

    scheduler.worker_pool.process_items.side_effect = process
    scheduler.process_queue()
    with Session(db) as session:
        assert session.get(MediaItem, 1).status in (PreviewStatus.MISSING, PreviewStatus.QUEUED)
        assert len(session.exec(select(MediaItem)).all()) == 3


def test_settings_round_trip_and_pending_ui_scope(db):
    with Session(db) as session:
        assert asyncio.run(main.get_settings(session=session, user="test"))["full_backlog"] is False
        session.add(movie(1))
        session.add(movie(2, is_priority=True))
        session.commit()
        limited = asyncio.run(main.get_items(session=session, user="test"))
        assert limited["total"] == 1
        stats = asyncio.run(main.get_stats(session=session, user="test"))
        assert stats["missing"] == 1
        asyncio.run(main.update_settings({"full_backlog": True}, session=session, user="test"))
        assert asyncio.run(main.get_settings(session=session, user="test"))["full_backlog"] is True
        assert asyncio.run(main.get_items(session=session, user="test"))["total"] == 2
        with pytest.raises(main.HTTPException):
            asyncio.run(main.update_settings({"full_backlog": "false"}, session=session, user="test"))


@pytest.mark.parametrize("full_backlog", [False, True])
def test_cached_work_precedes_queued_playback_work(db, full_backlog):
    with Session(db) as session:
        session.add(AppSettings(id=1, full_backlog=full_backlog))
        session.add(movie(1, is_priority=True, priority_score=999, status=PreviewStatus.QUEUED))
        session.add(movie(2, is_priority=True, priority_score=1000))
        session.commit()
        scheduler = Scheduler()
        assert [item.id for item in session.exec(scheduler._queue_candidates(session, 10)).all()] == [2, 1]


def test_priority_refresh_interval_accounts_for_scan_runtime(db, monkeypatch):
    scheduler = Scheduler()
    scheduler.config = SimpleNamespace()
    scheduler.last_sync_time = datetime.utcnow()
    scheduler.stop_event = MagicMock()
    scheduler.stop_event.is_set.side_effect = [False, True]
    scheduler.sync_wake_event = MagicMock()
    scheduler.refresh_priority_metadata = MagicMock()
    scheduler.sync_library = MagicMock()
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(monotonic=MagicMock(side_effect=[100, 145])))
    scheduler._sync_loop()
    scheduler.refresh_priority_metadata.assert_called_once()
    scheduler.sync_library.assert_not_called()
    scheduler.sync_wake_event.wait.assert_called_once_with(15)


def test_existing_settings_migrate_to_priority_only(db, monkeypatch):
    with Session(db) as session:
        session.add(AppSettings(id=1, full_backlog=True))
        session.commit()
    with db.connect() as connection:
        connection.execute(text("ALTER TABLE appsettings DROP COLUMN full_backlog"))
        connection.commit()
    monkeypatch.setattr(database, "engine", db)
    database.create_db_and_tables()
    database.create_db_and_tables()  # Idempotent
    with Session(db) as session:
        assert session.get(AppSettings, 1).full_backlog is False


def test_active_playback_selects_video_sessions_only():
    plex = MagicMock()
    plex.sessions.return_value = [
        SimpleNamespace(ratingKey=str(i), type=kind, players=[SimpleNamespace(state=state)])
        for i, kind, state in [(1, "movie", "playing"), (2, "episode", "paused"),
                               (3, "episode", "buffering"), (4, "track", "playing"),
                               (5, "movie", "stopped")]
    ]
    assert Scheduler()._collect_active_playback_rating_keys(plex) == {1, 2, 3}


def test_detection_combines_playback_and_cache_with_existing_sources(db, monkeypatch):
    scheduler = Scheduler()
    with Session(db) as session:
        session.add(movie(2, file_path="/cached"))
        session.commit()
    monkeypatch.setattr(scheduler, "_read_priority_watch_events", lambda limit: [])
    monkeypatch.setattr(scheduler, "_read_priority_episode_rows", lambda: [])
    monkeypatch.setattr(scheduler, "_collect_priority_hub_items", lambda plex: {})
    monkeypatch.setattr(scheduler, "_collect_on_deck_rating_keys", lambda plex: {3})
    monkeypatch.setattr(scheduler, "_collect_active_playback_rating_keys", lambda plex: {1, 99})
    monkeypatch.setattr(scheduler_module, "fully_cached_paths", lambda: {"/cached"})
    result = scheduler.detect_priority_items(MagicMock(), {1, 2, 3})
    assert set(result) == {1, 2, 3}
    assert result[2].score > result[1].score > result[3].score
    assert result[2].reasons[0]["type"] == "fully_cached"


def test_cache_catalogue_paginates_and_requires_verified_current_size(monkeypatch):
    monkeypatch.setenv("INFINIDYSK_URL", "http://infini")
    monkeypatch.setenv("INFINIDYSK_API_KEY", "test-key")
    ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(1, 5)]
    responses = [
        {"folders": [{"id": "online", "online": True}, {"id": "offline", "online": False}]},
        {"entries": [{"itemId": ids[0], "length": 100, "verifiedBytes": 100},
                     {"itemId": ids[1], "length": 100, "verifiedBytes": 99, "allocatedBytes": 100},
                     {"itemId": ids[2], "length": 0, "verifiedBytes": 0}], "nextAfter": "cursor"},
        {"entries": [{"itemId": ids[3], "length": 100, "verifiedBytes": 100}], "nextAfter": None},
        {"size": 100, "contentPath": "/content/file.mkv",
         "mappings": [{"status": "valid", "linkPath": "/mnt/plex/movie.mkv"}]},
        {"size": 101, "contentPath": "/content/stale.mkv"},
    ]
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.side_effect = [MagicMock(json=lambda value=value: value) for value in responses]
    monkeypatch.setattr(infini_cache.requests, "Session", lambda: client)
    paths = infini_cache.fully_cached_paths()
    assert paths == {f"/mnt/remote/infinidysk/.ids/{ids[0]}",
                     "/mnt/remote/infinidysk/content/file.mkv", "/mnt/plex/movie.mkv"}
    assert client.get.call_args_list[2].kwargs["params"]["after"] == "cursor"
    client.get.side_effect = requests.ConnectionError("private error")
    assert infini_cache.fully_cached_paths() == set()


def test_cache_item_requires_all_parts_and_matches_symlink_uuid(tmp_path):
    key = "00000000-0000-0000-0000-000000000001"
    link = tmp_path / "movie.mkv"
    link.symlink_to("/mnt/remote/infinidysk/.ids/" + key.replace("-", "") + ".mkv")
    item = movie(1, file_path=str(link))
    cached = {"/mnt/remote/infinidysk/.ids/" + key}
    assert infini_cache.item_is_fully_cached(item, cached)
    item.media_parts_info = json.dumps([{"file_path": str(link)}, {"file_path": "/cold"}])
    assert not infini_cache.item_is_fully_cached(item, cached)
    item.media_parts_info = "invalid"
    assert not infini_cache.item_is_fully_cached(item, cached)


def test_deleted_cached_item_does_not_hide_other_fully_cached_files(monkeypatch):
    monkeypatch.setenv("INFINIDYSK_URL", "http://infini")
    monkeypatch.setenv("INFINIDYSK_API_KEY", "test-key")
    ids = [f"00000000-0000-0000-0000-{i:012d}" for i in (1, 2)]
    response = lambda payload: MagicMock(status_code=200, json=lambda: payload)
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.side_effect = [
        response({"folders": [{"id": "online", "online": True}]}),
        response({"entries": [{"itemId": key, "length": 100, "verifiedBytes": 100} for key in ids], "nextAfter": None}),
        MagicMock(status_code=404),
        response({"size": 100, "contentPath": "/content/valid.mkv"}),
    ]
    monkeypatch.setattr(infini_cache.requests, "Session", lambda: client)
    paths = infini_cache.fully_cached_paths()
    assert paths == {f"/mnt/remote/infinidysk/.ids/{ids[1]}", "/mnt/remote/infinidysk/content/valid.mkv"}


def test_relative_infini_library_mapping_uses_library_root(monkeypatch):
    monkeypatch.setenv("INFINIDYSK_URL", "http://infini")
    monkeypatch.setenv("INFINIDYSK_API_KEY", "test-key")
    monkeypatch.setenv("INFINIDYSK_LIBRARY_PATH", "/library")
    key = "00000000-0000-0000-0000-000000000001"
    client = MagicMock()
    client.__enter__.return_value = client
    responses = [
        {"folders": [{"id": "online", "online": True}]},
        {"entries": [{"itemId": key, "length": 100, "verifiedBytes": 100}], "nextAfter": None},
        {"size": 100, "mappings": [{"status": "valid", "linkPath": "TV-HD/show/episode.mkv"}]},
    ]
    client.get.side_effect = [MagicMock(status_code=200, json=lambda value=value: value) for value in responses]
    monkeypatch.setattr(infini_cache.requests, "Session", lambda: client)
    paths = infini_cache.fully_cached_paths()
    assert "/library/TV-HD/show/episode.mkv" in paths


def test_sharded_infini_library_link_matches_without_fuse_stat(tmp_path, monkeypatch):
    key = "402c443b-2ffc-4d04-bc81-51d456fa0f5e"
    link = tmp_path / "movie.mkv"
    link.symlink_to("/mnt/remote/infinidysk/.ids/4/0/2/c/4/" + key)
    cached = {"/mnt/remote/infinidysk/.ids/" + key}
    monkeypatch.setattr(infini_cache.os.path, "realpath", lambda path: pytest.fail("Unnecessary FUSE stat"))
    assert infini_cache.item_is_fully_cached(movie(1, file_path=str(link)), cached)
    assert not infini_cache.item_is_fully_cached(movie(1, file_path=str(link)), {"/other/cached"})
