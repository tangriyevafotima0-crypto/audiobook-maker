"""
mordret_roster.py — deterministic helpers for the Mordret annotation
workflow. Nothing in this file calls an LLM; it only manages the batch
splitting, the running character roster, and the final file assembly.
The actual "who said this line" judgment is made by Claude directly while
following SKILL.md, one batch at a time — this module just keeps that
process consistent and resumable.
"""

import json
import os


DEFAULT_CHAPTERS_PER_BATCH = 20
SLOT_POOL = ["M1", "M2", "M3", "F1", "F2", "F3"]  # NARRATOR is implicit, not assigned


# ---------------------------------------------------------------------------
# Roster state (persisted to disk between batches)
# ---------------------------------------------------------------------------

def load_roster(roster_path):
    """Load roster state from disk, or return a fresh empty roster if the
    file doesn't exist yet (first batch of a new book)."""
    if os.path.isfile(roster_path):
        with open(roster_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "characters_so_far": [],
        "slots_used": {},          # slot_id -> character_id
        "slot_usage_count": {},    # slot_id -> how many characters share it (for least-frequent fallback)
        "last_chapter_annotated": 0,
    }


def save_roster(roster_path, roster):
    tmp_path = roster_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(roster, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, roster_path)


def find_existing_character(roster, name_or_alias):
    """Look up a character already in the roster by exact name or alias
    match. Returns the character dict, or None if not found. Matching is
    case-insensitive since a name might be capitalized differently in
    different chapters (e.g. mid-sentence vs. start-of-sentence)."""
    needle = name_or_alias.strip().lower()
    for char in roster["characters_so_far"]:
        if char["name"].strip().lower() == needle:
            return char
        for alias in char.get("aliases", []):
            if alias.strip().lower() == needle:
                return char
    return None


def assign_slot(roster, gender):
    """
    Pick a slot for a newly-discovered character of the given gender.
    Prefers an unused slot of the matching gender family (M* for male,
    F* for female). If all matching slots are already used, falls back to
    the least-frequently-shared slot of that gender family, so a book with
    hundreds of minor characters never runs out of slots -- it just means
    more characters end up sharing a voice, which is an acceptable
    degradation, not a failure.

    Unknown gender defaults to sharing NARRATOR rather than guessing a
    gendered slot -- an honest "no distinct voice" beats a coin-flip
    assignment.
    """
    if gender not in ("male", "female"):
        return "NARRATOR"

    prefix = "M" if gender == "male" else "F"
    family = [s for s in SLOT_POOL if s.startswith(prefix)]

    for slot in family:
        if slot not in roster["slots_used"]:
            return slot

    # All slots in this gender family are taken -- reuse the least-loaded one.
    usage = roster.get("slot_usage_count", {})
    least_loaded = min(family, key=lambda s: usage.get(s, 0))
    return least_loaded


def register_character(roster, name, aliases, gender):
    """
    Add a new character to the roster, or return the existing one if a
    name/alias match is found. Always returns the character dict that
    should be used going forward. This is the single entry point the
    per-batch annotation process calls for every speaking character it
    encounters, so roster consistency lives in one place.
    """
    existing = find_existing_character(roster, name)
    if existing:
        # Merge in any new aliases discovered this batch.
        for alias in aliases:
            if alias not in existing.get("aliases", []):
                existing.setdefault("aliases", []).append(alias)
        return existing

    slot = assign_slot(roster, gender)
    char_id = _make_character_id(name, roster)
    character = {
        "id": char_id,
        "name": name,
        "aliases": list(aliases),
        "gender": gender,
        "slot": slot,
    }
    roster["characters_so_far"].append(character)
    roster["slots_used"].setdefault(slot, char_id)
    roster.setdefault("slot_usage_count", {})
    roster["slot_usage_count"][slot] = roster["slot_usage_count"].get(slot, 0) + 1
    return character


def _make_character_id(name, roster):
    """snake_case id from a display name, de-duplicated against the
    existing roster in the rare case two different characters share a
    normalized name (e.g. two characters both nicknamed 'Junior')."""
    base = "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
    while "__" in base:
        base = base.replace("__", "_")
    if not base:
        base = "character"

    existing_ids = {c["id"] for c in roster["characters_so_far"]}
    candidate = base
    n = 2
    while candidate in existing_ids:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# Batch splitting
# ---------------------------------------------------------------------------

def make_batches(total_chapters, chapters_per_batch=DEFAULT_CHAPTERS_PER_BATCH, resume_from=1):
    """
    Split [resume_from, total_chapters] into consecutive batches of
    chapters_per_batch. resume_from lets annotation pick up where a
    previous run's roster left off (roster["last_chapter_annotated"] + 1),
    mirroring the audio generator's own filesystem-checkpoint resume.

    Returns a list of (start_chapter, end_chapter) inclusive tuples.
    """
    if resume_from > total_chapters:
        return []

    batches = []
    start = resume_from
    while start <= total_chapters:
        end = min(start + chapters_per_batch - 1, total_chapters)
        batches.append((start, end))
        start = end + 1
    return batches


# ---------------------------------------------------------------------------
# Final assembly
# ---------------------------------------------------------------------------

def assemble_mordret_file(book_title, source_file, roster, chapters, output_path):
    """
    Combine the final roster and all annotated chapters into the complete
    book.mordret.json file. `chapters` is the list of per-chapter dicts
    (each already in the {"chapter_index", "chapter_title", "lines": [...]}
    shape) accumulated across all batches.
    """
    # Final guard before writing to disk: refuse to assemble a file that
    # contains a "text" field anywhere in its lines. This is a last line of
    # defense in case a batch's chapters list reached this function without
    # going through validate_chapter_annotation -- writing such a file out
    # would both break the generator's loader (which only recognizes
    # anchor_start) and violate the no-book-text constraint this format
    # exists for.
    for chapter in chapters:
        for i, line in enumerate(chapter.get("lines", [])):
            if "text" in line:
                raise ValueError(
                    f"Refusing to assemble Mordret file: chapter "
                    f"{chapter.get('chapter_index')!r}, line {i} has a "
                    "'text' field. Only 'anchor_start' is valid in the v3 "
                    "format -- remove 'text' and replace it with a short "
                    "anchor phrase before assembling."
                )

    slots = {"NARRATOR": "neutral"}
    for slot in SLOT_POOL:
        slots[slot] = "male" if slot.startswith("M") else "female"

    mordret_doc = {
        "mordret_version": 3,
        "book_title": book_title,
        "source_file": source_file,
        "characters": roster["characters_so_far"],
        "slots": slots,
        "chapters": chapters,
    }

    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(mordret_doc, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, output_path)
    return mordret_doc


# ---------------------------------------------------------------------------
# Validation (run against every batch's output before accepting it)
# ---------------------------------------------------------------------------

def validate_chapter_annotation(chapter_dict, known_character_ids):
    """
    Sanity-check one annotated chapter's structure before it's accepted
    into the accumulated chapters list. Returns a list of problem strings
    (empty list = valid). This is deliberately strict: a malformed batch
    result should be caught and retried/fallback-handled immediately,
    never silently included in the final file.
    """
    problems = []

    if "chapter_index" not in chapter_dict:
        problems.append("missing chapter_index")
    if "lines" not in chapter_dict or not isinstance(chapter_dict["lines"], list):
        problems.append("missing or invalid lines array")
        return problems  # can't check further without lines

    if chapter_dict.get("annotation_status") == "fallback_narrator_only":
        # Fallback chapters have a relaxed shape -- just need the sentinel line.
        if len(chapter_dict["lines"]) != 1 or chapter_dict["lines"][0].get("speaker") != "NARRATOR":
            problems.append("fallback chapter must have exactly one NARRATOR line with empty anchor_start")
        return problems

    valid_speakers = known_character_ids | {"NARRATOR"}
    for i, line in enumerate(chapter_dict["lines"]):
        if "speaker" not in line:
            problems.append(f"line {i}: missing speaker")
            continue
        if line["speaker"] not in valid_speakers:
            problems.append(f"line {i}: unknown speaker id {line['speaker']!r}")
        if "anchor_start" not in line:
            problems.append(f"line {i}: missing anchor_start")
        elif not isinstance(line["anchor_start"], str):
            problems.append(f"line {i}: anchor_start must be a string")
        # "text" is a leftover v2-format field and must never appear here --
        # the generator's loader only recognizes anchor_start, and a stray
        # "text" field usually means a full sentence/paragraph was copied in
        # by mistake (a copyright problem, since Mordret files must never
        # store book text). Catch it immediately rather than letting it
        # reach the assembled file, where it silently breaks the generator.
        if "text" in line:
            problems.append(
                f"line {i}: has a 'text' field, which is not part of the v3 "
                "format and must not be written -- use 'anchor_start' "
                "(a short 3-8 word locating phrase) instead. This usually "
                "means a full line of book text was written by mistake."
            )

    return problems
