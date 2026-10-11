def test_recent_partial_items_use_resume_state_and_seven_day_window(tmp_path):
    import sqlite3
    db_dir = tmp_path / 'Plug-in Support' / 'Databases'
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / 'com.plexapp.plugins.library.db')
    conn.executescript("""
        CREATE TABLE metadata_items (id INTEGER PRIMARY KEY, guid TEXT, metadata_type INTEGER);
        CREATE TABLE metadata_item_settings (id INTEGER PRIMARY KEY, guid TEXT, account_id INTEGER,
            view_offset INTEGER, view_count INTEGER, last_viewed_at BIGINT);
        INSERT INTO metadata_items VALUES (1,'movie-guid',1),(4,'episode-guid',4),
            (5,'undated',1),(2,'show-guid',2);
    """)
    now = 1700000000
    since = now - 7 * 86400
    rows = [
        (1, 'movie-guid', 7, 1000, 0, since),  # Inclusive seven-day boundary.
        (2, 'movie-guid', 8, 2000, 0, now),  # Multiple viewers do not duplicate keys.
        (3, 'episode-guid', 8, 1000, 2, now),  # A partial rewatch counts.
        (4, 'undated', 7, 1000, 0, since - 1),
        (5, 'undated', 8, 0, 1, now),  # Completed playback has no resume offset.
        (6, 'undated', 9, None, 0, now),
        (7, 'undated', 10, 1000, 0, None),
        (8, 'undated', 11, 1000, 0, now + 1),
        (9, 'undated', None, 1000, 0, now),
        (10, 'show-guid', 7, 1000, 0, now),
    ]
    conn.executemany('INSERT INTO metadata_item_settings VALUES (?,?,?,?,?,?)', rows)
    conn.commit()
    conn.close()
    from types import SimpleNamespace
    from plex_generate_previews.web.resume_priority import recent_partial_rating_keys
    config = SimpleNamespace(plex_config_folder=str(tmp_path), plex_db_backend='sqlite')
    assert recent_partial_rating_keys(config, since, now) == {1, 4}
    assert recent_partial_rating_keys(config, now + 2, now + 7 * 86400 + 2) == set()
