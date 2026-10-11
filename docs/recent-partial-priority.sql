-- Apply to PostgreSQL Plex deployments before rolling out recent partial-watch priority.
GRANT SELECT ON plex.metadata_item_settings TO plex_previews_reader;
