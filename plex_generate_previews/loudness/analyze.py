"""ffmpeg loudnorm analysis — the same filter Plex Media Server runs.

Runs ``loudnorm=I=-16:TP=-1:LRA=9:print_format=json`` over an audio
stream, parses the JSON report from stderr, and maps it to Plex's
``ln:*`` extra_data fields. Read-only: never touches Plex's database.
"""
import json
import math
import re
import subprocess
import threading

from loguru import logger

from . import ANALYSIS_VERSION

# loudnorm settings matching Plex's native analysis
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1:LRA=9:print_format=json"

# Timeout bounds: 3x realtime, clamped to [10 min, 4 h]
_TIMEOUT_MIN = 600.0
_TIMEOUT_MAX = 4 * 3600.0

# Codecs where Plex uses its own decoder without dynamic range compression;
# ffmpeg must match with -drc_scale 0 (input option, before -i).
_DRC_ZERO_CODECS = {"EAC3"}


class LoudnessError(Exception):
    """Base error for loudness analysis failures."""


class UndecodableError(LoudnessError):
    """ffmpeg could not decode the stream at all."""


class ParseError(LoudnessError):
    """ffmpeg ran but no usable loudnorm report was produced."""


def timeout_for(duration_ms):
    """Analysis timeout: 3x realtime, clamped to [10 min, 4 h]."""
    if not duration_ms or duration_ms <= 0:
        return _TIMEOUT_MIN
    return min(max(duration_ms / 1000.0 * 3.0, _TIMEOUT_MIN), _TIMEOUT_MAX)


def command(ffmpeg, file, stream_index, codec):
    """Build the ffmpeg argv for a single stream."""
    args = [ffmpeg, "-hide_banner", "-nostats"]
    if codec and codec.upper() in _DRC_ZERO_CODECS:
        args += ["-drc_scale", "0"]
    args += [
        "-i", file,
        "-map", f"0:{stream_index}",
        "-af", LOUDNORM_FILTER,
        "-f", "null", "-",
    ]
    return args


def command_many(ffmpeg, file, streams):
    """Build one ffmpeg argv analyzing up to 3 streams of the same file.

    ``streams``: list of (track_name, stream_index, codec).
    Uses named loudnorm@trackN filters so each track's JSON report can be
    told apart on stderr.
    """
    if not streams:
        raise ValueError("command_many requires at least one stream")
    if len(streams) > 3:
        raise ValueError("command_many supports at most 3 streams per invocation")
    args = [ffmpeg, "-hide_banner", "-nostats"]
    if any(codec and codec.upper() in _DRC_ZERO_CODECS for _, _, codec in streams):
        # Shared input decode: one global -drc_scale 0 when any track needs it.
        args += ["-drc_scale", "0"]
    args += ["-i", file]
    for n, (track_name, stream_index, codec) in enumerate(streams):
        args += ["-map", f"0:{stream_index}"]
        filt = f"loudnorm@track{n}={LOUDNORM_FILTER.split('=', 1)[1]}"
        # -filter:a:N applies to the Nth mapped audio output
        args += [f"-filter:a:{n}", filt]
    args += ["-f", "null", "-"]
    return args, [name for name, _, _ in streams]


_JSON_CANDIDATE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}")


def parse(stderr_text):
    """Extract the loudnorm JSON report from ffmpeg stderr.

    Returns the parsed dict, or None if no report was found.
    """
    if not stderr_text:
        return None
    # The report is the last JSON object containing input_i.
    matches = _JSON_CANDIDATE.findall(stderr_text)
    for raw in reversed(matches):
        try:
            report = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict) and "input_i" in report:
            return report
    return None


def parse_many(stderr_text, track_names):
    """Extract per-track reports from a batched run. Returns {track_name: report}."""
    found = {}
    if not stderr_text:
        return found
    # Each named filter prints its own JSON; attribute by order of appearance.
    matches = _JSON_CANDIDATE.findall(stderr_text)
    reports = []
    for raw in matches:
        try:
            report = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict) and "input_i" in report:
            reports.append(report)
    for name, report in zip(track_names, reports):
        found[name] = report
    return found


def ln_fields(report):
    """Map a loudnorm report to Plex's ln:* extra_data fields."""
    return {
        "ln:loudness": str(report["input_i"]),
        "ln:peak": str(report["input_tp"]),
        "ln:lra": str(report["input_lra"]),
        "ln:threshold": str(report["input_thresh"]),
        "ln:gainOffset": str(report["target_offset"]),
        "ln:loudnessAnalysisVersion": ANALYSIS_VERSION,
    }


def valid_measurements(fields):
    """Check measurements are finite, or Plex's -inf/inf silence form."""
    try:
        if fields.get("ln:loudnessAnalysisVersion") != ANALYSIS_VERSION:
            return False
        loudness = fields["ln:loudness"]
        gain = fields["ln:gainOffset"]
        if loudness == "-inf" and gain == "inf":
            return True  # Plex's form for silent / too-short audio
        for key in ("ln:loudness", "ln:peak", "ln:lra", "ln:threshold", "ln:gainOffset"):
            if not math.isfinite(float(fields[key])):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _run_ffmpeg(args, timeout_s, cancel_event=None):
    """Run ffmpeg, return stderr text. Raises on timeout / failure."""
    if cancel_event is not None and cancel_event.is_set():
        raise LoudnessError("cancelled before ffmpeg start")
    logger.debug(f"loudness ffmpeg: {' '.join(args[:6])} …")
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as e:
        raise LoudnessError(f"ffmpeg not found: {e}")
    try:
        _, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
        raise LoudnessError(f"ffmpeg timed out after {timeout_s:.0f}s")
    if cancel_event is not None and cancel_event.is_set():
        raise LoudnessError("cancelled during ffmpeg run")
    return proc.returncode, stderr or ""


def run(ffmpeg, file, stream_index, codec, duration_ms, cancel_event=None):
    """Analyze one stream. Returns ln:* fields. Raises LoudnessError."""
    args = command(ffmpeg, file, stream_index, codec)
    returncode, stderr = _run_ffmpeg(args, timeout_for(duration_ms), cancel_event)
    report = parse(stderr)
    if report is None:
        # Distinguish "couldn't decode" from "decoded but no report".
        if "Invalid data" in stderr or "could not find codec" in stderr.lower():
            raise UndecodableError(f"ffmpeg could not decode stream {stream_index}")
        raise ParseError(
            f"no loudnorm report for stream {stream_index} "
            f"(rc={returncode}): {stderr[-500:]}"
        )
    fields = ln_fields(report)
    if not valid_measurements(fields):
        raise ParseError(f"loudnorm report failed validation: {report}")
    return fields


def run_many(ffmpeg, file, streams, duration_ms, cancel_event=None):
    """Analyze up to 3 streams of one file in a single ffmpeg.

    ``streams``: list of (key, stream_index, codec). Returns {key: ln-fields}.
    Streams that fail parsing are skipped (not fatal to the batch).
    """
    args, track_names = command_many(ffmpeg, file, streams)
    keys = [key for key, _, _ in streams]
    _, stderr = _run_ffmpeg(args, timeout_for(duration_ms), cancel_event)
    reports = parse_many(stderr, track_names)
    results = {}
    for key, track_name in zip(keys, track_names):
        report = reports.get(track_name)
        if report is None:
            logger.warning(f"loudness: no report for {key} in batch run")
            continue
        fields = ln_fields(report)
        if not valid_measurements(fields):
            logger.warning(f"loudness: invalid report for {key}: {report}")
            continue
        results[key] = fields
    return results


def ffmpeg_identity(ffmpeg="ffmpeg"):
    """Short identity string for the ffmpeg binary (undecodable keying)."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-version"], capture_output=True, text=True, timeout=10
        )
        first_line = (proc.stdout or "").splitlines()
        return (first_line[0].strip() if first_line else "unknown")[:120]
    except Exception:
        return "unknown"


def has_loudnorm_filter(ffmpeg="ffmpeg"):
    """True when the ffmpeg binary ships the loudnorm filter.

    The write path checks this at runtime so a bad image fails loudly
    instead of silently writing nothing.
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return False
    return re.search(r"\bloudnorm\b", proc.stdout or "") is not None


def new_cancel_event():
    return threading.Event()
