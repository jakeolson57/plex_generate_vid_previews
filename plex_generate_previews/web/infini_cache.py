"""Read InfiniDysk's verified native-cache catalogue without downloading media."""

import json
import os
import re
from uuid import UUID

import requests
from loguru import logger


def fully_cached_paths() -> set[str]:
    url = os.environ.get("INFINIDYSK_URL", "").rstrip("/")
    key = os.environ.get("INFINIDYSK_API_KEY", "")
    if not url or not key:
        return set()

    try:
        with requests.Session() as client:
            client.headers["x-api-key"] = key

            def get(path, allow_missing=False, **params):
                response = client.get(url + path, params=params, timeout=(5, 15))
                if allow_missing and response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()

            status = get("/api/native-cache")
            if status.get("initializationPending") or status.get("initializationError"):
                return set()
            complete: dict[str, int] = {}
            for folder in status["folders"]:
                if not folder.get("online"):
                    continue
                after = ""
                seen = set()
                while True:
                    page = get("/api/native-cache/entries", folderId=folder["id"], after=after, limit=200)
                    for entry in page["entries"]:
                        length = entry.get("length")
                        if isinstance(length, int) and length > 0 and entry.get("verifiedBytes") == length:
                            complete[str(UUID(entry["itemId"]))] = length
                    after = page.get("nextAfter")
                    if not after:
                        break
                    if after in seen:
                        raise ValueError("Repeated cache cursor")
                    seen.add(after)

            paths = set()
            mount = os.environ.get("INFINIDYSK_MOUNT_PATH", "/mnt/remote/infinidysk").rstrip("/")
            library = os.environ.get("INFINIDYSK_LIBRARY_PATH", "/mnt/plex")
            for item_id, length in complete.items():
                details = get("/api/get-library-file-details", allow_missing=True, davItemId=item_id)
                if not details or details.get("size") != length:
                    continue
                paths.add(mount + "/.ids/" + item_id)
                content = details.get("contentPath")
                if content:
                    paths.add(mount + "/" + content.lstrip("/"))
                for mapping in details.get("mappings", []):
                    if mapping.get("status") == "valid" and mapping.get("linkPath"):
                        link = mapping["linkPath"]
                        paths.add(link if os.path.isabs(link) else os.path.normpath(os.path.join(library, link)))
            return paths
    except (requests.RequestException, ValueError, KeyError, TypeError):
        # Do not include URL, headers or response text: they may contain credentials.
        logger.warning("InfiniDysk verified cache catalogue unavailable; skipping cached-only eligibility")
        return set()


def item_is_fully_cached(item, cached_paths: set[str]) -> bool:
    if not cached_paths:
        return False
    try:
        parts = json.loads(item.media_parts_info) if item.media_parts_info else [{"file_path": item.file_path}]
        paths = [part.get("file_path") for part in parts]
    except (ValueError, TypeError, AttributeError):
        return False
    if not paths or not all(paths):
        return False
    mount = os.environ.get("INFINIDYSK_MOUNT_PATH", "/mnt/remote/infinidysk").rstrip("/")

    def identity_cached(path):
        if not path.startswith(mount + "/.ids/"):
            return None
        # InfiniDysk also shards UUIDs into directories such as .ids/4/0/2/c/4/UUID.
        match = re.fullmatch(r"([0-9a-fA-F-]{32,36})(?:\.[^/]*)?", os.path.basename(path))
        if match:
            try:
                return mount + "/.ids/" + str(UUID(match[1])) in cached_paths
            except ValueError:
                pass
        return False

    def cached(path):
        if path in cached_paths:
            return True
        identity_match = identity_cached(path)
        if identity_match is not None:
            return identity_match
        # Library symlinks commonly point straight to a sharded InfiniDysk UUID.
        # Read that link first rather than stat every directory through NFS/FUSE.
        try:
            target = os.readlink(path)
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(os.path.dirname(path), target))
            if target in cached_paths:
                return True
            identity_match = identity_cached(target)
            if identity_match is not None:
                return identity_match
        except OSError:
            pass
        resolved = os.path.realpath(path)
        if resolved in cached_paths:
            return True
        return bool(identity_cached(resolved))

    return all(cached(path) for path in paths)
