"""Plex extra_data decoding and form-preserving encoding.

Plex stores ``extra_data`` as JSON *or* URL-encoded form. The decoder
finds the measurements; the encoder (Phase 2) writes measurements back
**in whichever form the row already used** — never converting forms,
because Plex can tell the difference.

Forms:
- ``json``: plain JSON object, e.g. ``{"ln:loudness": "-16.32", ...}``
- ``url_json``: percent-encoded JSON, e.g. ``%7B%22ln%3Aloudness%22...``
- ``query``: URL query-string form, e.g. ``ln%3Aloudness=-16.32&...``

The read path never raises (returns ``{}``); the write path is strict
and refuses to clobber a row it cannot decode.
"""
import json
from urllib.parse import unquote, parse_qsl, quote, urlencode

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


def detect_form(raw):
    """Which form ``raw`` extra_data is stored in.

    Returns "json", "url_json", "query", or "empty" for blank values.
    Undecodable non-empty values report "json" (the write path refuses
    them separately via :func:`decode_extra_data_strict`).
    """
    if not raw:
        return "empty"
    if isinstance(raw, dict):
        return "json"
    text = str(raw).strip()
    if not text:
        return "empty"
    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return "json"
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        decoded = json.loads(unquote(text))
        if isinstance(decoded, dict):
            return "url_json"
    except (json.JSONDecodeError, ValueError):
        pass
    if "=" in text:
        try:
            if parse_qsl(text, keep_blank_values=True):
                return "query"
        except ValueError:
            pass
    return "json"


def decode_extra_data_strict(raw):
    """Decode extra_data, raising ValueError on non-empty garbage.

    The write path uses this so a row it cannot understand is refused
    instead of silently clobbered.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    decoded = decode_extra_data(text)
    if not decoded and text not in ("{}", ""):
        raise ValueError(f"undecodable extra_data: {text[:120]!r}")
    return decoded


def encode_extra_data(decoded, form="json"):
    """Encode a decoded extra_data dict back to its storage form."""
    if form == "url_json":
        return quote(
            json.dumps(decoded, ensure_ascii=False, separators=(",", ":")),
            safe="",
        )
    if form == "query":
        return urlencode(list(decoded.items()))
    return json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))


def merge_loudness_fields(raw, fields):
    """Merge ln:* fields into existing extra_data, preserving its form.

    Other keys (e.g. ``ma:title``) are kept untouched. Returns the new
    raw string. Raises ValueError when the existing value is non-empty
    but undecodable — the caller must refuse that row, not overwrite it.
    """
    form = detect_form(raw)
    decoded = decode_extra_data_strict(raw)
    merged = dict(decoded)
    merged.update(fields)
    if form == "empty":
        form = "json"
    return encode_extra_data(merged, form)


def _same_extra_data(a, b):
    """True when two raw extra_data values decode to the same dict."""
    return decode_extra_data(a) == decode_extra_data(b)


def has_loudness(extra_data):
    """True if the stream's extra_data already carries Plex loudness fields."""
    decoded = decode_extra_data(extra_data)
    return (
        decoded.get("ln:loudnessAnalysisVersion") == ANALYSIS_VERSION
        and "ln:loudness" in decoded
    )
