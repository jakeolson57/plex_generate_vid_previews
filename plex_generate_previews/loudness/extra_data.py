"""Plex extra_data decoding (read-only for Phase 1).

Plex stores ``extra_data`` as JSON *or* URL-encoded form. This module
decodes either form so discovery can check whether a stream already has
loudness measurements. The form-preserving *encoder* (needed for writes)
is Phase 2 work — nothing here writes to Plex.
"""
import json
from urllib.parse import unquote, parse_qsl

from . import ANALYSIS_VERSION

# The ln:* keys Plex writes when a track has been analyzed.
LOUDNESS_KEYS = (
    "ln:loudness",
    "ln:peak",
    "ln:lra",
    "ln:threshold",
    "ln:gainOffset",
    "ln:loudnessAnalysisVersion",
)


def decode_extra_data(raw):
    """Decode Plex extra_data to a dict. Returns {} when undecodable/empty.

    Tries (in order): JSON, URL-decoded JSON, URL query-string form.
    Never raises.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    # 1. Plain JSON: {"ln:loudness": "-16.32", ...}
    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return decoded
    except (json.JSONDecodeError, ValueError):
        pass
    # 2. URL-encoded JSON: %7B%22ln%3Aloudness%22%3A ...
    try:
        decoded = json.loads(unquote(text))
        if isinstance(decoded, dict):
            return decoded
    except (json.JSONDecodeError, ValueError):
        pass
    # 3. URL query-string form: ln%3Aloudness=-16.32&ln%3Apeak=-1.2
    # (only when it actually looks like key=value pairs)
    if "=" in text:
        try:
            pairs = parse_qsl(text, keep_blank_values=True)
            if pairs:
                return dict(pairs)
        except ValueError:
            pass
    return {}


def has_loudness(extra_data):
    """True if the stream's extra_data already carries Plex loudness fields."""
    decoded = decode_extra_data(extra_data)
    return (
        decoded.get("ln:loudnessAnalysisVersion") == ANALYSIS_VERSION
        and "ln:loudness" in decoded
    )
