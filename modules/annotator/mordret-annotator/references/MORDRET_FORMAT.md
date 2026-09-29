# Mordret Format Specification (v3 — copyright-safe, position-based)

Mordret is a small JSON format that tells the audiobook generator which
voice to use for dialogue lines in a book. It connects two independent
systems:

- **The Annotator** (a Claude Skill) — reads a book, finds dialogue lines,
  and writes a Mordret file.
- **The Generator** (audiobook-maker) — reads the Mordret file and applies
  it during TTS generation, re-reading the actual text from the user's own
  book file. Pure lookup logic, no LLM involved.

## Critical design constraint: the Mordret file never stores book text

**The Mordret file contains zero copied sentences from the book.** It only
contains character names, voice slot ids, and short *anchor phrases* (a
handful of words) used to locate each line back in the user's own source
file at generation time. This matters for two independent reasons:

1. **Copyright.** A file containing the full text of a copyrighted novel,
   restructured into JSON, is still a copy of that novel. Reproducing
   large portions of copyrighted text — even for an internal technical
   format, even split across many small fields — isn't something the
   annotation process does. The annotator's output is *metadata about*
   the text (who said which part), never the text itself.
2. **It also naturally keeps the file small** — a 2000-chapter book's
   Mordret file stays under a megabyte instead of duplicating the entire
   book's content a second time.

The actual words the listener hears always come from the user's own PDF/
EPUB/TXT file, read fresh by the generator at generation time — the same
extraction step the generator already does today, whether or not a
Mordret file is present.

## Core idea: annotate dialogue only, not every sentence

Most of a novel is narration. Only quoted dialogue needs a speaker
assigned. Everything else uses one default narrator voice automatically.

## Core idea: flat character → voice slot, no indirection

Each character maps directly to a **speaker slot** (`M1`, `F1`,
`NARRATOR`, etc.) which the user assigns to one real installed Piper voice
in the GUI. One mapping step, not two.

---

## File: `book.mordret.json`

```json
{
  "mordret_version": 3,
  "book_title": "Reverend Insanity",
  "source_file": "reverend_insanity.epub",
  "characters": [
    { "id": "fang_yuan",      "name": "Fang Yuan",      "aliases": ["Old Ancestor"], "gender": "male",   "slot": "M1" },
    { "id": "bai_ning_bing",  "name": "Bai Ning Bing",  "aliases": [],               "gender": "female", "slot": "F1" }
  ],
  "slots": {
    "NARRATOR": "neutral",
    "M1": "male", "M2": "male", "M3": "male",
    "F1": "female", "F2": "female", "F3": "female"
  },
  "chapters": [
    {
      "chapter_index": 1,
      "chapter_title": "Chapter 1: Fatty Zhao",
      "lines": [
        { "speaker": "NARRATOR",     "anchor_start": "Fang Yuan opened his eyes" },
        { "speaker": "fang_yuan",    "anchor_start": "Where... am I" },
        { "speaker": "NARRATOR",     "anchor_start": "A voice answered" },
        { "speaker": "bai_ning_bing","anchor_start": "You are finally awake" }
      ]
    }
  ]
}
```

No `text` field anywhere. Only `anchor_start` — a short phrase (3–8 words)
marking where each line *begins* in the source text. A line's actual
content is everything from its own `anchor_start` match up to the next
line's `anchor_start` match (see resolution algorithm below) — so the
anchor only needs to be locatable, not perfectly bounded; imprecision in
exactly where a phrase "ends" doesn't matter and doesn't lose any text.

---

## Field reference

### Top level

| Field | Type | Notes |
|---|---|---|
| `mordret_version` | int | Currently `3`. |
| `book_title` | string | Display/logging only. |
| `source_file` | string | Filename of the original book this annotation was made against. The generator warns if asked to apply a Mordret file to a different source file, since anchors are only meaningful against the exact text they were found in. |
| `characters` | array | Every named speaker found, each pre-assigned a `slot`. |
| `slots` | object | Fixed pool of slot ids → gender hint. Always includes `NARRATOR`. |
| `chapters` | array | Per-chapter line list — attribution only, no text. |

### `characters[]`

Same as v2: `id`, `name`, `aliases` (annotator's own use, ignored by
generator), `gender` (`male`/`female`/`unknown`), `slot`.

### `slots`

Same fixed 7-slot pool as v2: `NARRATOR`, `M1`–`M3`, `F1`–`F3`.

### `chapters[].lines[]`

| Field | Notes |
|---|---|
| `speaker` | `"NARRATOR"` or a `characters[].id`. |
| `anchor_start` | First few words of this line, verbatim from the source text (3–8 words is typical). Marks where the line begins; the line's end is implicitly the start of the next line. |

---

## How the generator resolves anchors back to real text

For each chapter, the generator:

1. Extracts and cleans the chapter's real text from the source file,
   exactly as it already does today (same `text.py` extraction/cleaning
   pipeline, Mordret or not).
2. Finds each line's `anchor_start` match position, searching sequentially
   (each search starts where the previous one's match ended), so repeated
   phrases resolve to their correct, distinct occurrences.
3. Each line's actual text is the source text from its own matched
   position up to the *next* line's matched position (or end of chapter,
   for the last line). This guarantees, by construction, that concatenating
   every line's text reproduces the chapter exactly — no character is ever
   gained or lost, regardless of how imprecisely an anchor phrase was
   chosen.
4. A cosmetic cleanup pass moves a trailing orphaned opening-quote mark
   (e.g. a line ending in `…consciousness.\n\n"` right before the next
   line's quoted dialogue) to the start of the following line instead, so
   dialogue boundaries read naturally. This step never changes content,
   only where a shared boundary character is attached.
5. Each resulting line's text — pulled fresh from the user's own book
   file, never from the Mordret file — is sent to Piper in the voice
   mapped to that line's `speaker` slot.

### Fallback when an anchor can't be found

If `anchor_start` isn't found (LLM slightly misquoted, or the chapter text
was re-cleaned differently since annotation), the generator reuses the
previous successfully-matched position instead — effectively merging that
line's content into the preceding line (still spoken, just in the
preceding line's voice instead of its intended one). No text is ever
skipped or duplicated; a bad anchor only costs a voice assignment, never
content.

---

## Design principles

1. **No copyrighted text stored in the format, ever.** This is the
   non-negotiable constraint that shaped this version of the spec.
2. **Fixed slot pool, not open-ended character voices.** Trivial mapping
   step for the generator: 7 fixed slots → 7 real voice files.
3. **Default to NARRATOR on any uncertainty** — both at the
   character-attribution level and at the anchor-matching level.
4. **No text is ever silently dropped**, even on anchor mismatch.
5. **Plain, inspectable JSON.**
6. **Optional, additive.** No Mordret file = today's single-voice
   behavior, unchanged.

---

## Handling very long books: batch annotation with a running roster

A 2000+ chapter book cannot be annotated in a single LLM pass. The
Annotator Skill processes chapters in batches (e.g. 15–25 chapters per
batch) and carries a **roster state** forward between batches:

```json
{
  "characters_so_far": [
    { "id": "fang_yuan", "name": "Fang Yuan", "aliases": ["Old Ancestor"], "gender": "male", "slot": "M1" }
  ],
  "slots_used": { "M1": "fang_yuan" },
  "last_chapter_annotated": 25
}
```

Each batch prompt includes this roster. The model reuses an existing
`id`/`slot` for a recognized character (by name or alias) and only mints a
new `id` (+ next free slot of the right gender) for a genuinely new
speaking character. If all slots of a gender are taken, new minor
characters fall back to the least-frequent existing slot of that gender.

The roster file persists on disk between batches, so annotation can be
resumed by chapter number — the same filesystem-checkpoint approach used
throughout this project.

Output: one `book.mordret.json` per book, assembled from all batches.

---

## Handling refused or failed chapter annotation

Some books contain graphic content that could occasionally cause a
batch's annotation to fail or be declined. **This must never stop the
overall pipeline.** If a chapter's annotation fails for any reason, that
chapter is written to the Mordret file anyway, with a single line covering
the whole chapter, attributed to `NARRATOR`, marked
`annotation_status: "fallback_narrator_only"`:

```json
{
  "chapter_index": 47,
  "chapter_title": "Chapter 47: The Confrontation",
  "annotation_status": "fallback_narrator_only",
  "lines": [
    { "speaker": "NARRATOR", "anchor_start": "" }
  ]
}
```

An empty `anchor_start` is a special case meaning "the entire chapter,
unattributed" — the generator treats it as: use the whole cleaned chapter
text as one NARRATOR line, no anchor search needed. This keeps the schema
uniform while still requiring no reproduced text even in the fallback
case. The audiobook still generates completely for that chapter, just
without distinct character voices.
`annotation_status` is absent (or `"ok"`) on normally annotated chapters,
so a pass over the finished file shows exactly which chapters fell back.
