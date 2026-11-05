"""Tests for the read-only loudness overview page and the status sample limit."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from plex_generate_previews.web import main
from plex_generate_previews.web import scheduler as scheduler_module


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(db):
    main.app.dependency_overrides[main.get_session] = lambda: db
    test_client = TestClient(main.app)
    test_client.cookies.set("token", main.create_token("admin"))
    yield test_client
    main.app.dependency_overrides.clear()


def test_loudness_page_requires_login(db):
    main.app.dependency_overrides[main.get_session] = lambda: db
    try:
        anon = TestClient(main.app)
        response = anon.get("/loudness", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/login"
    finally:
        main.app.dependency_overrides.clear()


def test_loudness_page_renders_for_admin(client):
    response = client.get("/loudness")
    assert response.status_code == 200
    assert "Loudness" in response.text
    assert "/static/loudness.js?v=1" in response.text
    # Read-only page: no analyze button, no scheduler wiring.
    assert "loudness/analyze" not in response.text


def _fake_scan(monkeypatch):
    streams = [
        {
            "stream_id": i,
            "stream_index": 0,
            "codec": "eac3",
            "title": f"Movie {i}",
            "library": "Movies" if i % 2 else "TV",
        }
        for i in range(15)
    ]
    summary = {
        "total_audio": 20,
        "analyzed_in_plex": 5,
        "pending": 15,
        "undecodable_skipped": 1,
        "per_library": {
            "Movies": {"total": 12, "analyzed": 3, "pending": 9},
            "TV": {"total": 8, "analyzed": 2, "pending": 6},
        },
    }
    scan_mock = MagicMock(return_value=(streams, summary))
    monkeypatch.setattr(
        "plex_generate_previews.loudness.scanner.scan", scan_mock
    )
    monkeypatch.setattr(
        "plex_generate_previews.loudness.analyze.ffmpeg_identity",
        lambda: "test-ffmpeg",
    )
    monkeypatch.setattr(
        "plex_generate_previews.loudness.undecodable.connect",
        lambda path: MagicMock(),
    )
    monkeypatch.setattr(
        "plex_generate_previews.plex_client.open_plex_db",
        lambda folder: MagicMock(),
    )
    monkeypatch.setattr(
        scheduler_module.scheduler,
        "config",
        SimpleNamespace(
            plex_config_folder="/tmp",
            plex_path_mappings=[],
            plex_videos_path_mapping="",
            plex_local_videos_path_mapping="",
        ),
    )
    return scan_mock


def test_loudness_status_sample_limit(client, monkeypatch):
    scan_mock = _fake_scan(monkeypatch)
    default = client.get("/api/loudness/status").json()
    assert default["pending"] == 15
    assert len(default["sample_pending"]) == 10

    five = client.get("/api/loudness/status", params={"sample_limit": 5}).json()
    assert len(five["sample_pending"]) == 5

    everything = client.get(
        "/api/loudness/status", params={"sample_limit": 100}
    ).json()
    assert len(everything["sample_pending"]) == 15
    assert everything["per_library"]["Movies"]["pending"] == 9


def test_loudness_status_library_filter_passed_to_scanner(client, monkeypatch):
    scan_mock = _fake_scan(monkeypatch)
    client.get("/api/loudness/status", params={"library": "Movies"})
    assert scan_mock.call_args.kwargs["library_names"] == ["Movies"]
