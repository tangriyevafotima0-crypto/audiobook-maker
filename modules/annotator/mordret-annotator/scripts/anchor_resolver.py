"""
Reference implementation + tests for Mordret v3 anchor resolution.

This never stores or transmits copyrighted book text -- it only locates
short anchor phrases (already known to the caller) back in text the
caller already has a legal copy of, and returns substrings of that same
text. This module is what Stage 3 will fold into audiobook-maker/app.py.
"""


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


_TRAILING_QUOTE_CHARS = '"\u201c\u2018\u2019\''


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _reconstruct(resolved_lines):
    return "".join(line["text"] for line in resolved_lines)


def test_basic_reconstruction_is_lossless():
    source = (
        "Fang Yuan opened his eyes in an unfamiliar body.\n\n"
        '"Where... am I?" he muttered.\n\n'
        "A figure stood in the doorway.\n\n"
        '"You are finally awake," she said.\n'
    )
    line_specs = [
        {"speaker": "NARRATOR", "anchor_start": "Fang Yuan opened his eyes"},
        {"speaker": "fang_yuan", "anchor_start": "Where... am I"},
        {"speaker": "NARRATOR", "anchor_start": "A figure stood"},
        {"speaker": "bai_ning_bing", "anchor_start": "You are finally awake"},
    ]
    resolved = resolve_chapter_lines(source, line_specs)
    assert "".join(source.split()) == "".join(_reconstruct(resolved).split()), \
        "Reconstructed text must match source exactly (whitespace-insensitive)"
    assert resolved[0]["speaker"] == "NARRATOR"
    assert resolved[1]["speaker"] == "fang_yuan"
    assert resolved[1]["text"].startswith('"Where... am I?"'), \
        f"Orphan quote should move to next segment, got: {resolved[1]['text']!r}"
    print("PASS: test_basic_reconstruction_is_lossless")


def test_repeated_phrases_resolve_sequentially():
    source = 'He said nothing. She said nothing either.\nHe said, "Enough."\nShe said, "Agreed."\n'
    line_specs = [
        {"speaker": "NARRATOR", "anchor_start": "He said nothing"},
        {"speaker": "char_a", "anchor_start": "He said"},
        {"speaker": "char_b", "anchor_start": "She said"},
    ]
    resolved = resolve_chapter_lines(source, line_specs)
    assert "".join(source.split()) == "".join(_reconstruct(resolved).split())
    assert resolved[1]["text"].strip().startswith('He said, "Enough')
    assert resolved[2]["text"].strip().startswith('She said, "Agreed')
    print("PASS: test_repeated_phrases_resolve_sequentially")


def test_missing_anchor_falls_back_without_losing_content():
    source = "Some narration here. He said something important.\n"
    line_specs = [
        {"speaker": "NARRATOR", "anchor_start": "Some narration here"},
        {"speaker": "char_a", "anchor_start": "He said something totally wrong quote"},  # won't match
    ]
    resolved = resolve_chapter_lines(source, line_specs)
    assert "".join(source.split()) == "".join(_reconstruct(resolved).split()), \
        "Even with a bad anchor, zero content may be lost"
    print("PASS: test_missing_anchor_falls_back_without_losing_content")


def test_empty_anchor_means_whole_chapter():
    source = "This entire chapter is unattributed narration text.\n"
    line_specs = [{"speaker": "NARRATOR", "anchor_start": ""}]
    resolved = resolve_chapter_lines(source, line_specs)
    assert len(resolved) == 1
    assert resolved[0]["text"] == source
    print("PASS: test_empty_anchor_means_whole_chapter")


def test_no_lines_returns_empty():
    assert resolve_chapter_lines("some text", []) == []
    print("PASS: test_no_lines_returns_empty")


def test_single_word_anchors():
    source = 'He said, "Yes." She replied, "No."\n'
    line_specs = [
        {"speaker": "NARRATOR", "anchor_start": "He said"},
        {"speaker": "char_b", "anchor_start": "She replied"},
    ]
    resolved = resolve_chapter_lines(source, line_specs)
    assert "".join(source.split()) == "".join(_reconstruct(resolved).split())
    print("PASS: test_single_word_anchors")


def test_real_scale_chapter():
    """A longer, more realistic multi-character chapter."""
    source = """Fang Yuan opened his eyes in an unfamiliar body, the weight of memories not his own pressing down on his consciousness.

"Where... am I?" he muttered, his voice hoarse and unfamiliar.

A figure stood in the doorway, arms crossed, watching him with an unreadable expression.

"You are finally awake," she said. "I was beginning to think you would sleep forever."

Fang Yuan tried to sit up, but his body felt foreign, sluggish.

"Who are you?" he asked, his voice steadier now.

"Bai Ning Bing," she replied simply. "And you are in my debt."

He said nothing for a long moment, processing everything.

"I see," he finally said. "Then let us discuss the terms of this debt."
"""
    line_specs = [
        {"speaker": "NARRATOR", "anchor_start": "Fang Yuan opened his eyes"},
        {"speaker": "fang_yuan", "anchor_start": "Where... am I"},
        {"speaker": "NARRATOR", "anchor_start": "A figure stood"},
        {"speaker": "bai_ning_bing", "anchor_start": "You are finally awake"},
        {"speaker": "NARRATOR", "anchor_start": "Fang Yuan tried to sit up"},
        {"speaker": "fang_yuan", "anchor_start": "Who are you"},
        {"speaker": "bai_ning_bing", "anchor_start": "Bai Ning Bing"},
        {"speaker": "NARRATOR", "anchor_start": "He said nothing"},
        {"speaker": "fang_yuan", "anchor_start": "I see"},
    ]
    resolved = resolve_chapter_lines(source, line_specs)
    assert "".join(source.split()) == "".join(_reconstruct(resolved).split())
    assert len(resolved) == 9
    speakers_seen = [line["speaker"] for line in resolved]
    assert speakers_seen == [
        "NARRATOR", "fang_yuan", "NARRATOR", "bai_ning_bing",
        "NARRATOR", "fang_yuan", "bai_ning_bing", "NARRATOR", "fang_yuan",
    ]
    print("PASS: test_real_scale_chapter")


if __name__ == "__main__":
    test_basic_reconstruction_is_lossless()
    test_repeated_phrases_resolve_sequentially()
    test_missing_anchor_falls_back_without_losing_content()
    test_empty_anchor_means_whole_chapter()
    test_no_lines_returns_empty()
    test_single_word_anchors()
    test_real_scale_chapter()
    print()
    print("ALL TESTS PASSED")
