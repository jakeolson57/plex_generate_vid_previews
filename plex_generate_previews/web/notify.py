"""Discord webhook notifications.

Fire-and-forget by design: every send runs on a daemon thread and any
failure is logged, never raised — a notification must never break the
scheduler loop that triggered it.
"""
import threading
import time

import requests
from loguru import logger

_lock = threading.Lock()
_last_sent_at = 0.0


def send_discord(webhook_url, content, debounce_seconds=0):
    """POST ``content`` to a Discord webhook.

    Returns True if the send was dispatched, False if there is no URL
    or the call was debounced. Never raises.
    """
    if not webhook_url:
        return False
    if debounce_seconds > 0:
        global _last_sent_at
        with _lock:
            now = time.monotonic()
            if now - _last_sent_at < debounce_seconds:
                logger.debug("Discord notification debounced")
                return False
            _last_sent_at = now

    def _post():
        try:
            resp = requests.post(webhook_url, json={"content": content}, timeout=5)
            if resp.status_code not in (200, 204):
                logger.warning(f"Discord webhook returned HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Discord webhook failed: {e}")

    threading.Thread(target=_post, daemon=True, name="discord-notify").start()
    return True
