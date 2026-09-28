"""
Text-to-speech using Piper TTS (local, free, no API key, no network calls
after the voice model is downloaded).
"""

import json
import os
import re
import wave

from piper import PiperVoice, SynthesisConfig


_voice_cache = {}


def load_voice(model_path):
    """Load (and cache) a Piper voice model from a .onnx file path."""
    if model_path not in _voice_cache:
        _voice_cache[model_path] = PiperVoice.load(model_path)
    return _voice_cache[model_path]


def text_to_speech(text, output_file, model_path, speed=1.0, speaker_id=None):
    """
    Generate speech for `text` and write it to `output_file` (WAV).

    speed: 1.0 = normal. >1.0 = faster, <1.0 = slower.
    Implemented via Piper's length_scale (inverse of speed).
    speaker_id: for multi-speaker models (e.g. libritts, vctk), which
    speaker to use. None uses the model's default speaker.
    """
    if not text or not text.strip():
        raise ValueError("Cannot synthesize empty or whitespace-only text.")

    voice = load_voice(model_path)
    length_scale = 1.0 / speed if speed > 0 else 1.0
    syn_config = SynthesisConfig(length_scale=length_scale, speaker_id=speaker_id)

    with wave.open(output_file, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_config)


def list_available_voices(voices_dir):
    """Return a list of .onnx voice model filenames found in voices_dir
    that have a matching .onnx.json config file. A voice missing its config
    would fail at generation time with a confusing error, so it's filtered
    out here instead."""
    if not os.path.isdir(voices_dir):
        return []
    result = []
    for f in sorted(os.listdir(voices_dir)):
        if not f.lower().endswith(".onnx"):
            continue
        config_path = os.path.join(voices_dir, f + ".json")
        if os.path.isfile(config_path):
            result.append(f)
    return result


def list_incomplete_voices(voices_dir):
    """Return .onnx filenames that are missing their .onnx.json config —
    useful for surfacing a clear warning instead of a silent omission."""
    if not os.path.isdir(voices_dir):
        return []
    result = []
    for f in sorted(os.listdir(voices_dir)):
        if not f.lower().endswith(".onnx"):
            continue
        config_path = os.path.join(voices_dir, f + ".json")
        if not os.path.isfile(config_path):
            result.append(f)
    return result


# ---------------------------------------------------------------------------
# Gender labeling for the voice picker
# ---------------------------------------------------------------------------
#
# Piper doesn't expose a gender field anywhere (not in the filename schema,
# not in the .onnx.json config), so this is a best-effort lookup based on
# the known speaker names Piper voices ship under. It only affects display
# grouping in the UI — it never changes which audio actually gets
# generated. Names not in this list are labeled "Unknown" rather than
# guessed, since a wrong guess would be misleading.
#
# Sources: the official Piper voice list (rhasspy/piper-voices) plus a few
# other-language voices with clearly gendered names.
_FEMALE_VOICE_NAMES = {
    "amy", "kathleen", "kristin", "hfc_female", "ljspeech", "libritts",
    "libritts_r", "kusal", "daniela", "carlfm", "mls", "cori", "lisa",
    "nathalie", "siwis", "tugba", "gyro", "sweetbbak", "ana",
    # Voices in the default download set that were falling through to
    # "Unknown", which stopped Mordret auto-assignment from using them
    # for female slots even though they're clearly female speakers.
    "lessac", "jenny_dioco", "jenny", "alba", "semaine",
}
_MALE_VOICE_NAMES = {
    "danny", "joe", "john", "ryan", "sam", "norman", "bryce", "arctic",
    "hfc_male", "l2arctic", "reza_ibrahim", "imre", "jirka", "kareem",
    "mc_speech", "davefx", "diego", "eva_k", "karlsson", "thorsten",
    "pavoque", "onyx", "yashpal", "rrbutani",
}


def guess_voice_gender(onnx_filename):
    """
    Best-effort gender guess for a Piper voice, based on its speaker name
    (the part between the language code and quality tier in Piper's
    standard naming, e.g. 'en_US-amy-medium.onnx' -> 'amy').

    Returns 'Female', 'Male', or 'Unknown'. This is for UI grouping only —
    if a name isn't recognized, 'Unknown' is returned rather than guessed,
    since a confident-looking wrong label is worse than an honest unknown.
    """
    name = onnx_filename
    if name.lower().endswith(".onnx"):
        name = name[:-5]

    parts = name.split("-")
    # Standard Piper naming is <lang>-<speaker>-<quality>, e.g.
    # en_US-amy-medium. Take the middle segment(s) as the speaker name.
    if len(parts) >= 3:
        speaker = "-".join(parts[1:-1])
    elif len(parts) == 2:
        speaker = parts[1]
    else:
        speaker = name

    speaker = speaker.lower()

    if speaker in _FEMALE_VOICE_NAMES:
        return "Female"
    if speaker in _MALE_VOICE_NAMES:
        return "Male"

    # Fall back to substring matching for names Piper sometimes suffixes
    # (e.g. "hfc_female" appearing inside a longer custom filename).
    for female_name in _FEMALE_VOICE_NAMES:
        if female_name in speaker:
            return "Female"
    for male_name in _MALE_VOICE_NAMES:
        if male_name in speaker:
            return "Male"

    return "Unknown"


def group_voices_by_gender(onnx_filenames):
    """
    Split a list of .onnx filenames into {'Female': [...], 'Male': [...],
    'Unknown': [...]} using guess_voice_gender.
    """
    groups = {"Female": [], "Male": [], "Unknown": []}
    for f in onnx_filenames:
        groups[guess_voice_gender(f)].append(f)
    return groups


# ---------------------------------------------------------------------------
# Broader categorization: locale, quality tier, multi-speaker detection
# ---------------------------------------------------------------------------

_VOICE_FILENAME_RE = re.compile(
    r"^(?P<locale>[a-z]{2,3}_[A-Z]{2})-(?P<speaker>.+)-(?P<quality>x_low|low|medium|high)\.onnx$"
)


def parse_voice_filename(onnx_filename):
    """
    Parse locale, speaker name, and quality tier from a Piper voice's
    standard filename convention (<lang>_<REGION>-<speaker>-<quality>.onnx).
    Any part that can't be parsed (non-standard filename) falls back to
    'unknown' rather than guessing.
    """
    match = _VOICE_FILENAME_RE.match(onnx_filename)
    if not match:
        return {"locale": "unknown", "speaker": onnx_filename, "quality": "unknown"}
    return {
        "locale": match.group("locale"),
        "speaker": match.group("speaker"),
        "quality": match.group("quality"),
    }


def _read_voice_config(voices_dir, onnx_filename):
    config_path = os.path.join(voices_dir, onnx_filename + ".json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def categorize_voices(files, voices_dir=None):
    """
    Return {filename: {"locale", "quality", "gender", "multi_speaker",
    "num_speakers", "speaker_id_map"}} for each .onnx filename in `files`.

    `voices_dir` is used to read each voice's .onnx.json config for
    speaker-count info (the "num_speakers"/"speaker_id_map" fields some
    multi-speaker Piper models like libritts/vctk expose); without it,
    every voice is treated as single-speaker.
    """
    result = {}
    for f in files:
        parsed = parse_voice_filename(f)
        num_speakers = 1
        speaker_id_map = {}
        if voices_dir:
            config = _read_voice_config(voices_dir, f)
            num_speakers = config.get("num_speakers") or 1
            speaker_id_map = config.get("speaker_id_map") or {}
        result[f] = {
            "locale": parsed["locale"],
            "quality": parsed["quality"],
            "gender": guess_voice_gender(f),
            "multi_speaker": num_speakers > 1,
            "num_speakers": num_speakers,
            "speaker_id_map": speaker_id_map,
        }
    return result
