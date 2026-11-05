"""Tests for loudness analyze.py (pure functions + ffmpeg integration)."""
import json
import shutil

import pytest

from plex_generate_previews.loudness import analyze, ANALYSIS_VERSION

SAMPLE_REPORT = {
    "input_i": "-16.32",
    "input_tp": "-1.24",
    "input_lra": "7.12",
    "input_thresh": "-34.21",
    "output_i": "-16.00",
    "output_tp": "-1.00",
    "output_lra": "7.00",
    "output_thresh": "-34.21",
    "normalization_type": "dynamic",
    "target_offset": "-0.32",
}

SAMPLE_STDERR = (
    "ffmpeg version 8.0 Copyright ...\n"
    "  Stream #0:1: Audio: aac\n"
    "[Parsed_loudnorm_0 @ 0x1234]\n"
    + json.dumps(SAMPLE_REPORT, indent=2)
    + "\nvideo:0kB audio:0kB done\n"
)


def test_command_single():
    args = analyze.command("ffmpeg", "/media/a.mkv", 1, "AAC")
    assert args[:3] == ["ffmpeg", "-hide_banner", "-nostats"]
    assert "-drc_scale" not in args
    assert args[args.index("-map") + 1] == "0:1"
    assert "loudnorm=I=-16:TP=-1:LRA=9:print_format=json" in args


def test_command_eac3_gets_drc_scale():
    args = analyze.command("ffmpeg", "/media/a.mkv", 2, "EAC3")
    i = args.index("-i")
    assert args[i - 2:i] == ["-drc_scale", "0"]


def test_command_many_batch():
    streams = [("s1", 1, "AAC"), ("s2", 2, "EAC3")]
    args, names = analyze.command_many("ffmpeg", "/media/a.mkv", streams)
    assert names == ["s1", "s2"]
    assert "loudnorm@track0=" in " ".join(args)
    assert "loudnorm@track1=" in " ".join(args)
    # shared input: single global -drc_scale 0 because one track is EAC3
    assert args.count("-drc_scale") == 1
    assert args.index("-drc_scale") < args.index("-i")


def test_command_many_rejects_empty_and_too_many():
    with pytest.raises(ValueError):
        analyze.command_many("ffmpeg", "/x", [])
    with pytest.raises(ValueError):
        analyze.command_many("ffmpeg", "/x", [(str(i), i, "AAC") for i in range(4)])


def test_timeout_for():
    assert analyze.timeout_for(0) == 600.0
    assert analyze.timeout_for(None) == 600.0
    assert analyze.timeout_for(3_600_000) == 10_800.0  # 1h -> 3h
    assert analyze.timeout_for(10_000_000_000) == 14_400.0  # clamped to 4h
    assert analyze.timeout_for(10_000) == 600.0  # 10s -> clamped to 10min


def test_parse_finds_last_report():
    report = analyze.parse(SAMPLE_STDERR)
    assert report is not None
    assert report["input_i"] == "-16.32"
    assert report["target_offset"] == "-0.32"


def test_parse_returns_none_without_report():
    assert analyze.parse("ffmpeg version 8.0\nnothing here\n") is None
    assert analyze.parse("") is None
    assert analyze.parse(None) is None


def test_parse_many():
    stderr = SAMPLE_STDERR + SAMPLE_STDERR.replace("-16.32", "-20.00")
    found = analyze.parse_many(stderr, ["t0", "t1"])
    assert set(found) == {"t0", "t1"}
    assert found["t0"]["input_i"] == "-16.32"
    assert found["t1"]["input_i"] == "-20.00"


def test_ln_fields_mapping():
    fields = analyze.ln_fields(SAMPLE_REPORT)
    assert fields == {
        "ln:loudness": "-16.32",
        "ln:peak": "-1.24",
        "ln:lra": "7.12",
        "ln:threshold": "-34.21",
        "ln:gainOffset": "-0.32",
        "ln:loudnessAnalysisVersion": ANALYSIS_VERSION,
    }


def test_valid_measurements():
    assert analyze.valid_measurements(analyze.ln_fields(SAMPLE_REPORT)) is True
    silence = dict(analyze.ln_fields(SAMPLE_REPORT))
    silence["ln:loudness"] = "-inf"
    silence["ln:gainOffset"] = "inf"
    assert analyze.valid_measurements(silence) is True
    bad = dict(analyze.ln_fields(SAMPLE_REPORT))
    bad["ln:peak"] = "nan"
    assert analyze.valid_measurements(bad) is False
    bad2 = dict(analyze.ln_fields(SAMPLE_REPORT))
    bad2["ln:loudnessAnalysisVersion"] = "0.01"
    assert analyze.valid_measurements(bad2) is False
    assert analyze.valid_measurements({}) is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_run_on_synthetic_audio(tmp_path):
    """End-to-end: generate 5s of audio, analyze it, validate fields."""
    wav = str(tmp_path / "tone.wav")
    import subprocess
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-c:a", "pcm_s16le", wav],
        check=True, timeout=60,
    )
    fields = analyze.run("ffmpeg", wav, 0, "PCM", 5000)
    assert analyze.valid_measurements(fields)
    assert float(fields["ln:loudness"]) < 0  # a quiet sine is below -16 LUFS


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_run_many_on_synthetic_audio(tmp_path):
    import subprocess
    mkv = str(tmp_path / "two.mkv")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=5",
         "-map", "0:a", "-map", "1:a", "-c:a", "aac", mkv],
        check=True, timeout=60,
    )
    results = analyze.run_many(
        "ffmpeg", mkv, [("a", 0, "AAC"), ("b", 1, "AAC")], 5000)
    assert set(results) == {"a", "b"}
    for fields in results.values():
        assert analyze.valid_measurements(fields)


def test_run_missing_file_raises():
    with pytest.raises(analyze.LoudnessError):
        analyze.run("ffmpeg", "/nonexistent/file.mkv", 0, "AAC", 5000)


def test_ffmpeg_identity():
    ident = analyze.ffmpeg_identity()
    assert isinstance(ident, str) and len(ident) > 0
