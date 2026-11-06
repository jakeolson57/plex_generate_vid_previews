"""Tests for loudness extra_data decoding + undecodable store + paths."""
import json
from urllib.parse import quote

from plex_generate_previews.loudness import extra_data
from plex_generate_previews.loudness import undecodable
from plex_generate_previews.loudness.paths import translate_plex_path

JSON_FORM = json.dumps({
    "ln:loudness": "-16.32",
    "ln:peak": "-1.24",
    "ln:lra": "7.12",
    "ln:threshold": "-34.21",
    "ln:gainOffset": "-0.32",
    "ln:loudnessAnalysisVersion": "0.02",
    "title": "Some Movie",
})
URL_JSON_FORM = quote(JSON_FORM)
QUERY_FORM = "ln%3Aloudness=-16.32&ln%3AloudnessAnalysisVersion=0.02&title=Some+Movie"


def test_decode_json():
    assert extra_data.decode_extra_data(JSON_FORM)["ln:loudness"] == "-16.32"


def test_decode_url_encoded_json():
    assert extra_data.decode_extra_data(URL_JSON_FORM)["ln:loudness"] == "-16.32"


def test_decode_query_string():
    decoded = extra_data.decode_extra_data(QUERY_FORM)
    assert decoded["ln:loudness"] == "-16.32"


def test_decode_garbage_returns_empty():
    assert extra_data.decode_extra_data("not json at all {{{") == {}
    assert extra_data.decode_extra_data("") == {}
    assert extra_data.decode_extra_data(None) == {}
    assert extra_data.decode_extra_data({"already": "dict"}) == {"already": "dict"}


def test_has_loudness():
    assert extra_data.has_loudness(JSON_FORM) is True
    assert extra_data.has_loudness(URL_JSON_FORM) is True
    assert extra_data.has_loudness(QUERY_FORM) is True
    assert extra_data.has_loudness(json.dumps({"title": "x"})) is False
    assert extra_data.has_loudness("") is False
    # wrong analysis version does not count
    old = json.dumps({"ln:loudness": "-16.0", "ln:loudnessAnalysisVersion": "0.01"})
    assert extra_data.has_loudness(old) is False


def test_undecodable_roundtrip(tmp_path):
    con = undecodable.connect(str(tmp_path / "u.db"))
    try:
        assert undecodable.is_undecodable(con, "hash1", 1, "AV3A", "ffmpeg 8.0") is False
        undecodable.mark_undecodable(con, "hash1", 1, "AV3A", "ffmpeg 8.0")
        assert undecodable.is_undecodable(con, "hash1", 1, "AV3A", "ffmpeg 8.0") is True
        # different stream index -> not marked
        assert undecodable.is_undecodable(con, "hash1", 2, "AV3A", "ffmpeg 8.0") is False
        # new ffmpeg identity -> fresh chance
        assert undecodable.is_undecodable(con, "hash1", 1, "AV3A", "ffmpeg 9.0") is False
        # empty hash never matches
        undecodable.mark_undecodable(con, "", 1, "AV3A", "ffmpeg 8.0")
        assert undecodable.is_undecodable(con, "", 1, "AV3A", "ffmpeg 8.0") is False
    finally:
        con.close()


def test_undecodable_clear_for_ffmpeg(tmp_path):
    con = undecodable.connect(str(tmp_path / "u.db"))
    try:
        undecodable.mark_undecodable(con, "h", 0, "X", "ffmpeg 8.0")
        undecodable.clear_for_ffmpeg(con, "ffmpeg 9.0")
        assert undecodable.is_undecodable(con, "h", 0, "X", "ffmpeg 8.0") is False
    finally:
        con.close()


def test_translate_plex_path():
    mappings = [("/data/media", "/media"), ("/data", "/mnt/data")]
    assert translate_plex_path("/data/media/a.mkv", mappings) == "/media/a.mkv"
    # longest prefix wins
    assert translate_plex_path("/data/other/b.mkv", mappings) == "/mnt/data/other/b.mkv"
    # legacy fallback
    assert translate_plex_path("/srv/x.mkv", (), "/srv", "/local") == "/local/x.mkv"
    # no match -> unchanged
    assert translate_plex_path("/elsewhere/x.mkv", mappings) == "/elsewhere/x.mkv"
    assert translate_plex_path("", mappings) == ""


# --- Phase 2: form-preserving encoding ---

FIELDS = {
    "ln:loudness": "-16.32",
    "ln:peak": "-1.24",
    "ln:lra": "7.12",
    "ln:threshold": "-34.21",
    "ln:gainOffset": "-0.32",
    "ln:loudnessAnalysisVersion": "0.02",
}


def test_detect_form():
    assert extra_data.detect_form(JSON_FORM) == "json"
    assert extra_data.detect_form(URL_JSON_FORM) == "url_json"
    assert extra_data.detect_form(QUERY_FORM) == "query"
    assert extra_data.detect_form("") == "empty"
    assert extra_data.detect_form(None) == "empty"


def test_encode_roundtrip_all_forms():
    decoded = json.loads(JSON_FORM)
    for form in ("json", "url_json", "query"):
        encoded = extra_data.encode_extra_data(decoded, form)
        assert extra_data.detect_form(encoded) == form
        assert extra_data.decode_extra_data(encoded) == decoded


def test_merge_preserves_form_and_other_keys():
    # JSON form: other keys survive, form stays JSON
    merged = extra_data.merge_loudness_fields(
        json.dumps({"title": "Some Movie"}), FIELDS)
    assert extra_data.detect_form(merged) == "json"
    out = extra_data.decode_extra_data(merged)
    assert out["title"] == "Some Movie"
    assert out["ln:loudness"] == "-16.32"
    # URL-encoded JSON form stays URL-encoded JSON
    merged = extra_data.merge_loudness_fields(
        quote(json.dumps({"title": "Some Movie"})), FIELDS)
    assert extra_data.detect_form(merged) == "url_json"
    assert extra_data.decode_extra_data(merged)["title"] == "Some Movie"
    # Query form stays query form
    merged = extra_data.merge_loudness_fields("title=Some+Movie", FIELDS)
    assert extra_data.detect_form(merged) == "query"
    assert extra_data.decode_extra_data(merged)["title"] == "Some Movie"
    # Empty original -> fresh JSON
    merged = extra_data.merge_loudness_fields("", FIELDS)
    assert extra_data.decode_extra_data(merged) == FIELDS


def test_merge_refuses_garbage():
    import pytest
    with pytest.raises(ValueError):
        extra_data.merge_loudness_fields("not json at all {{{", FIELDS)


def test_merge_with_control_chars_roundtrips():
    # Plex stores literal control chars in values like ma:title.
    original = json.dumps({"ma:title": "Line1\nLine2", "title": "T"})
    merged = extra_data.merge_loudness_fields(original, FIELDS)
    out = extra_data.decode_extra_data(merged)
    assert out["ma:title"] == "Line1\nLine2"
    assert out["ln:loudness"] == "-16.32"
