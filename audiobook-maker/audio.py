"""
Audio joining and export.

Chunks are generated as individual WAV files on disk (see app.py). This
module combines a chapter's WAV chunks into a single chapter MP3, and can
optionally combine all chapter MP3s into one M4B audiobook.

The filesystem itself is the checkpoint: a chunk WAV that already exists
on disk is treated as "done" and skipped on resume. No database needed.
"""

import os
import re
import subprocess
import wave
import shutil


_ffmpeg_exe_cache = None


def _resolve_ffmpeg_exe():
    """
    Find an ffmpeg binary to use: prefer a system install on PATH (may have
    better/updated codec support), falling back to the static binary
    bundled via the imageio-ffmpeg pip package (used by the packaged
    desktop app, which can't assume Homebrew/winget are available). Result
    is cached for the process lifetime since it can't change mid-run.
    """
    global _ffmpeg_exe_cache
    if _ffmpeg_exe_cache is not None:
        return _ffmpeg_exe_cache

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        _ffmpeg_exe_cache = system_ffmpeg
        return _ffmpeg_exe_cache

    try:
        import imageio_ffmpeg
        _ffmpeg_exe_cache = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _ffmpeg_exe_cache = None
    return _ffmpeg_exe_cache


def ffmpeg_available():
    return _resolve_ffmpeg_exe() is not None


def ffprobe_available():
    # imageio-ffmpeg only bundles the ffmpeg binary, not ffprobe, so M4B
    # chapter-duration probing always requires a system ffprobe. Callers
    # check this to degrade gracefully (skip M4B, keep MP3 chapters)
    # instead of crashing when only the bundled ffmpeg is available.
    return shutil.which("ffprobe") is not None


def chunk_done(chunk_path):
    """A chunk is considered complete if the WAV file exists and is non-empty
    and not truncated (i.e. it opens cleanly as a WAV file)."""
    if not os.path.exists(chunk_path) or os.path.getsize(chunk_path) == 0:
        return False
    try:
        with wave.open(chunk_path, "rb") as f:
            return f.getnframes() > 0
    except (wave.Error, EOFError):
        return False


REAL_BOUNDARY_PAUSE_MS = 600
FORCED_BOUNDARY_PAUSE_MS = 200


def join_wavs_to_mp3(wav_paths, output_mp3_path, bitrate="128k",
                      pause_ms=None, boundary_is_real=None,
                      loudnorm=True):
    """
    Join a list of WAV files (in order) into a single MP3 file using ffmpeg.
    Uses the concat demuxer for speed and to avoid loading all audio into RAM.

    A short silence is inserted between chunks so chapter audio doesn't
    sound abruptly cut between chunk boundaries. By default the pause
    length varies per gap: boundary_is_real (one bool per gap, i.e.
    len(wav_paths) - 1 entries) marks which gaps fall on a real paragraph/
    chapter/speaker-turn break (a longer, more natural pause) versus a
    forced length-limit split from chunk_text (a shorter pause, since it's
    not really a break in the text at all). Pass pause_ms for the old
    flat-pause behavior instead. This is purely a listening-quality
    improvement, not a functional requirement, so if silence generation
    fails for any reason the chunks are still joined back-to-back rather
    than the whole export failing.

    loudnorm applies ffmpeg's EBU R128 loudness normalization so volume
    doesn't visibly jump between separately-synthesized chunks.
    """
    if not wav_paths:
        raise ValueError("No audio chunks to join.")

    num_gaps = len(wav_paths) - 1
    if pause_ms is not None:
        gap_pauses = [pause_ms] * num_gaps
    elif boundary_is_real is not None:
        if len(boundary_is_real) != num_gaps:
            raise ValueError(
                f"boundary_is_real has {len(boundary_is_real)} entries, "
                f"expected {num_gaps} (len(wav_paths) - 1)."
            )
        gap_pauses = [REAL_BOUNDARY_PAUSE_MS if real else FORCED_BOUNDARY_PAUSE_MS
                      for real in boundary_is_real]
    else:
        gap_pauses = [REAL_BOUNDARY_PAUSE_MS] * num_gaps

    list_file = output_mp3_path + ".concat.txt"
    silence_paths = {}  # duration_ms -> path, reused across matching gaps
    for duration_ms in sorted(set(d for d in gap_pauses if d > 0)):
        path = f"{output_mp3_path}.silence_{duration_ms}.wav"
        if _make_silence_wav(path, wav_paths[0], duration_ms):
            silence_paths[duration_ms] = path

    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for i, path in enumerate(wav_paths):
                abs_path = os.path.abspath(path)
                escaped = abs_path.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
                if i < num_gaps:
                    silence_path = silence_paths.get(gap_pauses[i])
                    if silence_path:
                        silence_abs = os.path.abspath(silence_path)
                        silence_escaped = silence_abs.replace("'", "'\\''")
                        f.write(f"file '{silence_escaped}'\n")

        cmd = [
            _resolve_ffmpeg_exe() or "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-ar", "44100", "-ac", "1",
        ]
        if loudnorm:
            cmd += ["-af", "loudnorm=I=-18:TP=-2:LRA=11"]
        cmd += ["-b:a", bitrate, output_mp3_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed while joining audio: {result.stderr[-500:]}")
    finally:
        if os.path.exists(list_file):
            os.remove(list_file)
        for path in silence_paths.values():
            if os.path.exists(path):
                os.remove(path)


def trim_silence_wav(wav_path, threshold_db=-45, min_silence_s=0.1):
    """
    Trim leading/trailing silence from a synthesized chunk WAV in place, so
    Piper's own edge silence doesn't stack with the pause inserted between
    chunks at join time. Best-effort: if ffmpeg isn't available or the
    filter fails for any reason, the WAV is left untouched (never blocks
    the run over a purely cosmetic pacing improvement). Returns True if a
    trim was applied.
    """
    ffmpeg_exe = _resolve_ffmpeg_exe()
    if not ffmpeg_exe:
        return False

    tmp_path = wav_path + ".trim.tmp.wav"
    cmd = [
        ffmpeg_exe, "-y", "-i", wav_path,
        "-af",
        f"silenceremove=start_periods=1:start_duration={min_silence_s}:"
        f"start_threshold={threshold_db}dB:"
        f"stop_periods=-1:stop_duration={min_silence_s}:"
        f"stop_threshold={threshold_db}dB",
        tmp_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    # A chunk that is entirely silence (e.g. text that synthesized to
    # nothing audible) trims down to a valid but zero-frame WAV. Keeping
    # that would make chunk_done() report the chunk as unfinished forever,
    # so it'd be regenerated on every resume and contribute an empty file
    # to the concat -- keep the untrimmed original in that case.
    try:
        with wave.open(tmp_path, "rb") as trimmed:
            trimmed_frames = trimmed.getnframes()
    except (wave.Error, EOFError):
        trimmed_frames = 0
    if trimmed_frames <= 0:
        os.remove(tmp_path)
        return False

    os.replace(tmp_path, wav_path)
    return True


def _make_silence_wav(silence_path, reference_wav_path, duration_ms):
    """
    Generate a short silent WAV matching the sample rate / channels / sample
    width of reference_wav_path, so it concatenates cleanly with the real
    chunks. Returns True on success, False if it couldn't be created (in
    which case the caller should just skip inserting pauses).
    """
    try:
        with wave.open(reference_wav_path, "rb") as ref:
            n_channels = ref.getnchannels()
            sampwidth = ref.getsampwidth()
            framerate = ref.getframerate()

        n_frames = int(framerate * duration_ms / 1000)
        silence_bytes = b"\x00" * (n_frames * n_channels * sampwidth)

        with wave.open(silence_path, "wb") as out:
            out.setnchannels(n_channels)
            out.setsampwidth(sampwidth)
            out.setframerate(framerate)
            out.writeframes(silence_bytes)
        return True
    except (wave.Error, OSError):
        return False


_FFMETADATA_SPECIAL = re.compile(r"([=;#\\\n])")


def _escape_ffmetadata(value):
    """Escape a value for the FFMETADATA1 format, per ffmpeg docs: '=', ';',
    '#', '\\', and newline must be backslash-escaped inside metadata values."""
    return _FFMETADATA_SPECIAL.sub(r"\\\1", value)


def combine_mp3s_to_m4b(mp3_paths, chapter_titles, output_m4b_path, book_title="Audiobook"):
    """
    Combine chapter MP3s into a single M4B audiobook with chapter markers.
    Falls back gracefully (raises) if ffmpeg/ffprobe is unavailable; caller
    should catch and simply skip M4B creation, keeping the MP3s as the
    deliverable.
    """
    if not mp3_paths:
        raise ValueError("No chapter MP3s to combine.")

    if not ffprobe_available():
        raise RuntimeError(
            "ffprobe not found (needed to measure chapter durations for M4B "
            "markers). Install ffmpeg with ffprobe (e.g. 'brew install "
            "ffmpeg') -- the bundled packaged-app ffmpeg alone doesn't "
            "include it."
        )

    work_dir = os.path.dirname(output_m4b_path)
    list_file = os.path.join(work_dir, "_m4b_concat.txt")
    chapters_file = os.path.join(work_dir, "_m4b_chapters.txt")

    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for path in mp3_paths:
                abs_path = os.path.abspath(path)
                escaped = abs_path.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # Build chapter metadata by probing each MP3's duration.
        with open(chapters_file, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n")
            f.write(f"title={_escape_ffmetadata(book_title)}\n\n")
            start_ms = 0
            for path, title in zip(mp3_paths, chapter_titles):
                duration_ms = _get_duration_ms(path)
                end_ms = start_ms + duration_ms
                f.write("[CHAPTER]\n")
                f.write("TIMEBASE=1/1000\n")
                f.write(f"START={start_ms}\n")
                f.write(f"END={end_ms}\n")
                f.write(f"title={_escape_ffmetadata(title)}\n\n")
                start_ms = end_ms

        cmd = [
            _resolve_ffmpeg_exe() or "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-i", chapters_file,
            "-map_metadata", "1",
            "-c:a", "aac", "-b:a", "96k",
            "-f", "mp4",
            output_m4b_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed while creating M4B: {result.stderr[-500:]}")
    finally:
        for f in (list_file, chapters_file):
            if os.path.exists(f):
                os.remove(f)


def _get_duration_ms(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return 0
    try:
        return int(float(result.stdout.strip()) * 1000)
    except ValueError:
        return 0
