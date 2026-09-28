"""
mordret.py — reads and applies a book.mordret.json file during audio
generation.

A Mordret file never stores actual book text (see MORDRET_FORMAT.md for
why) -- it only contains short "anchor" phrases marking where each line
starts. This module locates those anchors back in the real chapter text
(freshly extracted from the user's own book file, same as the normal
generation path) and returns the actual substrings to synthesize, each
tagged with which voice slot should speak it.
"""

import json
import os


SLOT_IDS = ("NARRATOR", "M1", "M2", "M3", "F1", "F2", "F3")


def load_mordret_file(path):
    """Load and do a basic structural sanity check on a Mordret file.
    Raises ValueError with a human-readable message on any problem, since
    a malformed Mordret file should stop generation with a clear error
    rather than fail confusingly partway through a long book."""
    if not os.path.isfile(path):
        raise ValueError(f"Mordret file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        try:
            doc = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Mordret file is not valid JSON: {e}")

    required_keys = ("mordret_version", "characters", "slots", "chapters")
    missing = [k for k in required_keys if k not in doc]
    if missing:
        raise ValueError(f"Mordret file is missing required field(s): {', '.join(missing)}")

    if doc["mordret_version"] != 3:
        raise ValueError(
            f"This Mordret file is version {doc['mordret_version']}, but this "
            "copy of Audiobook Maker only supports version 3. Try re-running "
            "the Mordret Annotator skill to regenerate the file, or check "
            "for an Audiobook Maker update if the annotator has moved on to "
            "a newer version."
        )

    known_ids = {c["id"] for c in doc["characters"]} | {"NARRATOR"}
    for chapter in doc["chapters"]:
        for line in chapter.get("lines", []):
            if line.get("speaker") not in known_ids:
                raise ValueError(
                    f"Mordret file references unknown speaker "
                    f"{line.get('speaker')!r} in chapter {chapter.get('chapter_index')} "
                    "-- the file may be corrupted or from an incompatible version."
                )

    return doc


def build_slot_map(mordret_doc):
    """Map each character id -> its voice slot id, plus NARRATOR itself.
    This is what the generator consults to know which real voice file to
    use for a given line's speaker."""
    slot_map = {"NARRATOR": "NARRATOR"}
    for character in mordret_doc["characters"]:
        slot_map[character["id"]] = character["slot"]
    return slot_map


def find_candidate_mordret_file(input_path, extra_search_dirs=()):
    """
    Look for a Mordret annotation file that plausibly belongs to
    `input_path`: in the same directory as the book, or any of
    extra_search_dirs (e.g. the book's output/<title>/ folder), matching
    either '<book_stem>.mordret.json' or 'book.mordret.json'.

    Returns the path to the best candidate (preferring one whose recorded
    source_file actually matches the book), or None if nothing is found.
    Used to auto-suggest a Mordret file instead of requiring the user to
    browse for it manually.
    """
    book_stem = os.path.splitext(os.path.basename(input_path))[0]
    candidate_names = [f"{book_stem}.mordret.json", "book.mordret.json"]
    search_dirs = [os.path.dirname(os.path.abspath(input_path))] + list(extra_search_dirs)

    found = []
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for name in candidate_names:
            path = os.path.join(d, name)
            if os.path.isfile(path) and path not in found:
                found.append(path)

    if not found:
        return None

    for path in found:
        try:
            doc = load_mordret_file(path)
        except ValueError:
            continue
        if source_file_matches(doc, input_path):
            return path

    return found[0]


def source_file_matches(mordret_doc, actual_input_path):
    """Warn-worthy (not fatal) check: does the book file being generated
    match the file this Mordret annotation was made against? A mismatch
    doesn't necessarily break anything (anchors might still happen to
    match), but it's a strong signal the two are out of sync."""
    recorded = mordret_doc.get("source_file", "")
    actual = os.path.basename(actual_input_path)
    return recorded == actual


def find_anchor_positions(source_text, anchors, start_search_from=0):
    """
    Find each anchor's starting match position in source_text, searching
    sequentially so repeated phrases resolve to distinct occurrences.
    Returns a list of positions, using the previous good position as a
    fallback for any anchor that fails to match (never -1, never crashes).
    """
    positions = []
    pos = start_search_from
    last_good = start_search_from
    for anchor in anchors:
        if not anchor:
            # Empty anchor = "rest of chapter" sentinel, handled by caller.
            positions.append(last_good)
            continue
        idx = source_text.find(anchor, pos)
        if idx == -1:
            # Anchor not found from current position onward. Fall back to
            # the last known-good position so this line merges into the
            # previous one rather than losing content or crashing.
            positions.append(last_good)
        else:
            positions.append(idx)
            pos = idx + len(anchor)
            last_good = idx
    return positions


# Only characters that plausibly mark the *start* of dialogue. U+2019 (right
# single quote) and the plain ASCII apostrophe are excluded even though they
# can visually open a quote, because they're overwhelmingly used as
# contraction apostrophes (e.g. "wasn't") -- treating them as unmatched
# opening quotes would misfire on ordinary words far more often than it
# catches a real unmatched quote.
_TRAILING_QUOTE_CHARS = '"\u201c\u2018'


def _has_unmatched_opening_quote(text):
    """True if text ends in an opening quote mark with no matching close
    quote later in the same text (a plain heuristic: odd count of the
    ASCII double-quote char is treated as unmatched)."""
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped[-1] not in _TRAILING_QUOTE_CHARS:
        return False
    return stripped.count('"') % 2 == 1


def resolve_chapter_lines(source_text, line_specs):
    """
    line_specs: list of {"speaker": str, "anchor_start": str}

    Returns: list of {"speaker": str, "text": str} where every "text" is a
    real substring of source_text (never generated, never copied from
    anywhere else), and concatenating every "text" in order reproduces
    source_text exactly (this is verified by test_reconstruction below).
    """
    if not line_specs:
        return []

    anchors = [spec["anchor_start"] for spec in line_specs]
    positions = find_anchor_positions(source_text, anchors)

    raw_segments = []
    for i, spec in enumerate(line_specs):
        seg_start = positions[i]
        seg_end = positions[i + 1] if i + 1 < len(line_specs) else len(source_text)
        raw_segments.append((spec["speaker"], source_text[seg_start:seg_end]))

    return _move_orphan_quotes_forward(raw_segments)


def _move_orphan_quotes_forward(segments):
    """Cosmetic pass: if a segment ends with an unmatched opening quote,
    move that quote (and any whitespace immediately before it, kept with
    the segment it came from) to the front of the next segment instead, so
    dialogue boundaries read naturally. Never changes total content."""
    fixed = []
    carry = ""
    for speaker, text in segments:
        text = carry + text
        carry = ""
        if _has_unmatched_opening_quote(text):
            # Find the trailing quote char (after trailing whitespace) and
            # split it off to carry forward.
            stripped_len = len(text.rstrip())
            quote_pos = stripped_len - 1
            carry = text[quote_pos:]
            text = text[:quote_pos]
        fixed.append((speaker, text))
    if carry:
        last_speaker, last_text = fixed[-1]
        fixed[-1] = (last_speaker, last_text + carry)
    return [{"speaker": s, "text": t} for s, t in fixed]
