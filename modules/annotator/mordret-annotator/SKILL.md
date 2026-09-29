---
name: mordret-annotator
description: Annotate a novel/book file (PDF, EPUB, or TXT) with per-character dialogue attribution, producing a book.mordret.json file for use with the audiobook-maker's multi-voice generation feature. Use this whenever the user wants "different voices for different characters," "male and female voices for dialogue," asks to "annotate" or "tag" a book for the audiobook generator, or mentions Mordret by name. Handles books of any length, including very long ones (1000+ chapters), by processing in resumable batches. Never reproduces copyrighted book text in its output — only short locating phrases and speaker attribution.
---

# Mordret Annotator

Reads a book and produces a `book.mordret.json` file that tells the
audiobook-maker which character is speaking each line of dialogue, so it
can generate the audiobook with a different voice per character instead
of one voice for the whole book.

Read `references/MORDRET_FORMAT.md` in full before starting — it defines
the exact output schema this skill produces. Don't skip this; get the
schema wrong and the audiobook generator won't be able to read the file.

## The one rule that overrides everything else in this skill

**Never write actual book text into the Mordret file.** Only short
"anchor" phrases (3–8 words) that mark where a line starts, never full
sentences or paragraphs copied out. This isn't a style preference — a
Mordret file containing substantial reproduced text from a copyrighted
novel would itself be a copy of that novel. See "Writing anchors" below
for exactly how much text is safe to include in an anchor.

## Overview of the process

1. Extract the book's chapters (reuse the same extraction the audiobook
   generator already does — see `references/EXTRACTION.md`).
2. Load or initialize the roster (`scripts/mordret_roster.py`) — this
   tracks every character discovered so far and their assigned voice slot,
   and persists to disk so annotation can resume after an interruption.
3. Process chapters in batches (default 20 chapters per batch — see
   "Batch size" below for when to adjust). For each batch:
   - Read the batch's chapter text.
   - For each chapter, identify dialogue lines and who says them,
     following "Annotating a chapter" below.
   - Register any newly-discovered characters via
     `mordret_roster.register_character()` — never invent a character id
     by hand, always go through this function so aliases and slots stay
     consistent.
   - Validate the batch's output with
     `mordret_roster.validate_chapter_annotation()` before accepting it.
   - Save the updated roster to disk.
4. After all batches, call `mordret_roster.assemble_mordret_file()` to
   produce the final `book.mordret.json`.

## Setting up

```bash
python3 -c "
import sys
sys.path.insert(0, '<skill_path>/scripts')
import mordret_roster as mr
roster = mr.load_roster('<workspace>/roster.json')
print(f'Resuming from chapter {roster[\"last_chapter_annotated\"] + 1}' if roster['last_chapter_annotated'] else 'Starting fresh')
"
```

Use a workspace directory next to the book (e.g. `<book_name>_mordret_work/`)
to hold `roster.json` (running state) and `chapters_annotated.jsonl` (one
JSON line per finished chapter, appended as you go — this is the resumable
checkpoint, mirroring how the audio generator itself checkpoints on the
filesystem rather than trusting in-memory state alone).

## Batch size

Default to 20 chapters per batch. Adjust down (10–15) for chapters that
run unusually long, or up (25–30) for short chapters — the goal is each
batch comfortably fitting in context with room to think, not hitting a
fixed chapter count. For a very long book (500+ chapters), tell the user
up front roughly how many batches this will take
(`len(mordret_roster.make_batches(total_chapters, chapters_per_batch))`)
so they know what to expect before a long run starts.

## Annotating a chapter

For each chapter's cleaned text:

1. **Everything is NARRATOR by default.** Only quoted dialogue
   (`"..."` or similar quotation conventions used in the book) needs a
   speaker other than NARRATOR. Don't try to attribute narration,
   description, or internal thought that isn't in quotes — that stays
   NARRATOR even if it's clearly "from" a character's point of view.

2. **For each dialogue line, determine the speaker from context** — the
   most common signal is an attribution tag right before or after the
   quote ("Fang Yuan said", "she asked", "he muttered"). When a
   conversation has no explicit tag for a line (common in back-and-forth
   dialogue once speakers are established), infer from the alternating
   pattern and immediate context. If you genuinely can't tell — the text
   is ambiguous even with full context — attribute it to NARRATOR rather
   than guessing. A missed distinct voice is a minor quality loss; a
   wrong character voice is a worse listening experience.

3. **Resolve the speaker's name through the roster**, not by writing a
   new id yourself:
   ```python
   character = mr.register_character(roster, name="Fang Yuan", aliases=["Old Ancestor"], gender="male")
   speaker_id = character["id"]  # use this in the line's "speaker" field
   ```
   If this is a character already in `roster["characters_so_far"]` (by
   name or alias, case-insensitive), this returns the *existing* record —
   always use its `id`, never mint a new one for a character you
   recognize under a different name/title than before.

4. **Determine gender from context** the first time a character speaks —
   pronouns used to describe them, how other characters address them,
   explicit description. If truly unclear, pass `gender="unknown"` (the
   roster will place them in NARRATOR's slot rather than guessing).

## Writing anchors

For each line (NARRATOR or character), write `anchor_start`: the exact
first 3–8 words of that line, copied verbatim from the source text —
character-for-character exact, including punctuation, since the generator
does an exact substring search against the real book file to relocate
this text later. A close paraphrase will fail to match.

- Long narration paragraph → anchor is just its first few words, not the
  whole paragraph.
- Short dialogue ("Enough.") → the anchor can be the whole short line if
  it's already under ~8 words.
- Never write an `anchor_end`, a full sentence, or a summary — the format
  only uses `anchor_start` (see the format doc for why this is sufficient
  and how the generator resolves line boundaries from it alone).

## When a chapter can't be annotated

If a chapter's content makes it impossible to complete normal annotation
(the analysis is declined, produces an error, or the content can't be
processed for any reason), do **not** stop the overall run. Record that
chapter using the fallback shape instead and continue to the next chapter:

```python
fallback_chapter = {
    "chapter_index": chapter_index,
    "chapter_title": chapter_title,
    "annotation_status": "fallback_narrator_only",
    "lines": [{"speaker": "NARRATOR", "anchor_start": ""}],
}
```

This is a normal, expected code path — not an error state to report back
to the user as a failure. Mention at the end how many chapters (if any)
fell back, so the user knows those specific chapters will use a single
voice instead of per-character voices, but the run as a whole succeeds.

## After each batch

1. Validate every chapter in the batch:
   ```python
   known_ids = {c["id"] for c in roster["characters_so_far"]}
   for chapter in batch_chapters:
       problems = mr.validate_chapter_annotation(chapter, known_ids)
       if problems:
           # Fix the issue and re-validate, or fall back this one chapter
           # rather than including malformed data.
           ...
   ```
2. Append validated chapters to the running `chapters_annotated.jsonl`
   (one JSON object per line — this makes partial progress durable and
   trivially resumable, since you can read back exactly which chapters
   are already done without re-parsing a single giant array).
3. Update `roster["last_chapter_annotated"]` and save the roster:
   ```python
   roster["last_chapter_annotated"] = batch_end_chapter
   mr.save_roster(roster_path, roster)
   ```

## Resuming an interrupted run

If `roster.json` and `chapters_annotated.jsonl` already exist for this
book, don't start over. Read `roster["last_chapter_annotated"]`, resume
batching from the next chapter (`mr.make_batches(total, resume_from=roster["last_chapter_annotated"] + 1)`),
and keep appending to the same `chapters_annotated.jsonl`. This mirrors
the audio generator's own chunk-level resume — a full re-run should never
be necessary just because a previous annotation pass was interrupted.

## Final assembly

Once every chapter is annotated (or has a valid fallback entry):

```python
chapters = [json.loads(line) for line in open(chapters_jsonl_path, encoding="utf-8")]
mr.assemble_mordret_file(
    book_title=book_title,
    source_file=source_filename,   # must match the filename the audiobook
                                    # generator will be pointed at later
    roster=roster,
    chapters=chapters,
    output_path=os.path.join(book_dir, "book.mordret.json"),
)
```

Tell the user where the file was written and give a short summary: how
many characters were identified, how many chapters (if any) fell back to
narrator-only, and remind them the next step is mapping each voice slot
(`M1`–`M3`, `F1`–`F3`) to a real installed Piper voice in the
audiobook-maker GUI before generating.
