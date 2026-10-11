"""Read per-viewer Plex resume state without changing catalogue-source configuration."""
from contextlib import closing


class ResumeStateError(RuntimeError):
    """Resume state is unavailable; preserve existing priorities."""


def recent_partial_rating_keys(config, since: int, now: int) -> set[int]:
    query = """
        SELECT DISTINCT mi.id
        FROM plex.metadata_item_settings settings
        JOIN plex.metadata_items mi ON mi.guid = settings.guid
        WHERE mi.metadata_type IN (1, 4) AND settings.account_id IS NOT NULL
            AND settings.view_offset > 0
            AND settings.last_viewed_at >= ? AND settings.last_viewed_at <= ?
        ORDER BY mi.id
    """
    try:
        try:
            from ..plex_database import get_data_source
        except ModuleNotFoundError as exc:
            if exc.name != "plex_generate_previews.plex_database":
                raise
            if getattr(config, "plex_db_backend", "sqlite") != "sqlite":
                raise ResumeStateError("Configured Plex database backend is unavailable")
            from ..plex_client import open_plex_db
            with closing(open_plex_db(config.plex_config_folder)) as conn:
                rows = conn.execute(query.replace("plex.", ""), (since, now)).fetchall()
        else:
            rows = get_data_source(config).query(query, (since, now))
        return {int(row[0]) for row in rows}
    except Exception:
        raise ResumeStateError("Plex resume state read failed") from None
