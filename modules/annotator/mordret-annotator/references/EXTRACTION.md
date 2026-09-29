# Extraction: getting chapter text for annotation

This document explains how to get each chapter's text before annotating
it, and why this step must produce *exactly* the same text the audiobook
generator will later extract from the same file.

## Why this has to match exactly

Anchors (`anchor_start`) are exact substrings searched for in the
generator's own extracted-and-cleaned chapter text at generation time. If
the text you annotated against differs from what the generator extracts
later — different whitespace collapsing, different page-number stripping,
different chapter boundaries — an anchor that matched during annotation
may not be found during generation, and every mismatched line falls back
to NARRATOR (safe, but defeats the purpose of annotating).

## How to extract chapter text for annotation

The audiobook-maker project (a sibling tool this skill produces input
for) already has this exact extraction and cleaning logic in
`text.py: extract_chapters()` and `text.py: clean_text()`. Reuse it
directly rather than re-implementing extraction:

```python
import sys
sys.path.insert(0, "<path_to_audiobook_maker>")
import text as textmod

chapters = textmod.extract_chapters(book_file_path)  # [(title, raw_text), ...]
cleaned_chapters = [
    (title, textmod.clean_text(raw_text))
    for title, raw_text in chapters
]
```

If the audiobook-maker project isn't available in the environment (e.g.
this skill is being run standalone), ask the user for its path, or fall
back to a minimal equivalent extraction — but flag clearly in the output
summary that anchors were produced against independently-extracted text
and may need re-validation against the generator's own extraction before
first use.

## Chapter indexing must match too

`extract_chapters()` returns chapters in the same order and with the same
splitting logic the generator itself uses (including its "Introduction"
handling for text before the first detected heading — see the
generator's own chapter-detection behavior). Use the same 1-based
`chapter_index` numbering the generator would assign when iterating this
list, so `chapter_index` in the Mordret file lines up with the generator's
own chapter loop.

## What NOT to do

- Don't apply any additional cleaning, paraphrasing, or normalization
  beyond what `clean_text()` already does — extra changes make anchors
  less likely to match the generator's version of the same text.
- Don't annotate against the raw, uncleaned extraction — always clean
  first, matching the generator's own order of operations (extract, then
  clean, then — in the generator's normal path — chunk; annotation
  replaces the chunking step with line-based splitting instead).
