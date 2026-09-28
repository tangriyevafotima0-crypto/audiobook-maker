"""
Text extraction, cleaning, chapter detection, and chunking.

Pipeline: file -> raw text (per chapter) -> cleaned text -> chunks (~1000-2000 chars)
"""

import re
import os


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_chapters(path, start_page=None, end_page=None):
    """
    Returns a list of (title, text) tuples.
    If chapters can't be detected, returns a single ("Full Text", text) tuple.
    Raises ValueError with a human-readable message on failure.

    start_page/end_page (1-indexed, inclusive) restrict extraction to a
    page range -- PDF only, since EPUB/TXT have no native page concept.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        return _extract_pdf(path, start_page=start_page, end_page=end_page)
    elif ext == ".epub":
        if start_page is not None or end_page is not None:
            raise ValueError("Page range is only supported for PDF files.")
        return _extract_epub(path)
    elif ext == ".txt":
        if start_page is not None or end_page is not None:
            raise ValueError("Page range is only supported for PDF files.")
        return _extract_txt(path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


def _extract_pdf(path, start_page=None, end_page=None):
    import pymupdf  # PyMuPDF (module still importable as `fitz`, but that
                     # alias is deprecated in newer PyMuPDF releases)

    doc = pymupdf.open(path)
    page_count = len(doc)

    first_index = (start_page - 1) if start_page is not None else 0
    last_index = (end_page - 1) if end_page is not None else page_count - 1
    if first_index < 0 or last_index >= page_count or first_index > last_index:
        doc.close()
        raise ValueError(
            f"PDF has {page_count} page(s); requested range "
            f"{start_page or 1}-{end_page or page_count} is out of bounds."
        )

    full_text = []
    for page in doc[first_index:last_index + 1]:
        full_text.append(page.get_text())
    doc.close()

    text = "\n".join(full_text)

    if not text.strip():
        raise ValueError("This PDF appears to be scanned and contains no selectable text.")

    chapters = _split_by_chapter_headings(text)
    return chapters


def pdf_page_count(path):
    """Return the number of pages in a PDF, for UI hints. Raises ValueError
    on any problem opening the file."""
    import pymupdf
    try:
        doc = pymupdf.open(path)
        count = len(doc)
        doc.close()
        return count
    except Exception as e:
        raise ValueError(f"Could not read PDF: {e}")


def _extract_epub(path):
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(path)
    chapters = []

    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")

        # Try to find a title from a heading tag
        heading = soup.find(["h1", "h2", "h3"])
        title = heading.get_text(strip=True) if heading else None

        text = soup.get_text("\n")
        text = text.strip()

        if not text:
            continue

        if not title:
            title = f"Section {len(chapters) + 1}"

        chapters.append((title, text))

    if not chapters:
        raise ValueError("Could not extract any readable text from this EPUB.")

    return chapters


def _extract_txt(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    if not text.strip():
        raise ValueError("This TXT file appears to be empty.")

    return _split_by_chapter_headings(text)


# ---------------------------------------------------------------------------
# Chapter detection (for PDF / TXT, which have no built-in structure)
# ---------------------------------------------------------------------------

_CHAPTER_PATTERN = re.compile(
    r"^\s*("
    r"chapter\s+\d+"
    r"|chapter\s+[ivxlcdm]+"
    r"|chapter\s+(one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen"
    r"|eighteen|nineteen|twenty)"
    r"|part\s+\d+"
    r"|part\s+[ivxlcdm]+"
    r")\s*[:\-]?\s*.{0,60}$",
    re.IGNORECASE,
)


def _split_by_chapter_headings(text):
    lines = text.split("\n")
    chapter_starts = []  # (line_index, title)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 80:
            continue
        if _CHAPTER_PATTERN.match(stripped):
            chapter_starts.append((i, stripped))

    if not chapter_starts:
        return [("Full Text", text)]

    chapters = []
    # Any text before the first detected heading (e.g. a preface/intro) is
    # kept as its own leading section instead of being silently dropped.
    first_heading_line = chapter_starts[0][0]
    intro = "\n".join(lines[:first_heading_line]).strip()
    if intro:
        chapters.append(("Introduction", intro))

    for idx, (line_no, title) in enumerate(chapter_starts):
        start = line_no + 1
        end = chapter_starts[idx + 1][0] if idx + 1 < len(chapter_starts) else len(lines)
        body = "\n".join(lines[start:end])
        if body.strip():
            chapters.append((title, body))
        # Empty chapters (heading with no body, e.g. a trailing heading with
        # nothing after it) are intentionally skipped rather than emitted as
        # silent audio.

    if not chapters:
        # Every detected heading turned out to have no body text at all
        # (e.g. the whole "book" is just a lone heading line). Fall back to
        # treating the entire original text as one chapter so nothing is lost.
        return [("Full Text", text)]

    return chapters


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

_HYPHEN_LINEBREAK = re.compile(r"(\w+)-\n(\w+)")
_MULTI_BLANK = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_PAGE_NUMBER_LINE = re.compile(r"^\s*\d{1,4}\s*$")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")


def clean_text(text):
    # Fix hyphenation caused by line-wrap: "develop-\nment" -> "development"
    # Only join when both sides look like lowercase word fragments (avoids
    # breaking legitimate hyphenated words, which don't span a line break).
    text = _HYPHEN_LINEBREAK.sub(_join_hyphenated, text)

    lines = text.split("\n")
    cleaned_lines = []
    prev_line = None
    repeat_count = 0

    for line in lines:
        stripped = line.strip()

        # Drop standalone page numbers
        if _PAGE_NUMBER_LINE.match(stripped):
            continue

        # Detect repeated headers/footers (same short line appearing many
        # times in a row across the document is handled at join time below)
        cleaned_lines.append(stripped)

    text = "\n".join(cleaned_lines)

    # Collapse repeated headers/footers: a short line (<60 chars) repeated
    # 3+ times identically across the text is almost certainly a header/footer
    text = _strip_repeated_short_lines(text)

    text = _TRAILING_SPACE.sub("\n", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_BLANK.sub("\n\n", text)

    return text.strip()


def _join_hyphenated(match):
    left, right = match.group(1), match.group(2)
    # Only auto-join if left fragment is lowercase (real words at line-wrap
    # points are typically lowercase; this avoids merging "Self-" style
    # legitimate compounds that happen to end a line)
    if left[-1].islower() and right[0].islower():
        return left + right
    return left + "-" + right


def _strip_repeated_short_lines(text):
    lines = text.split("\n")
    positions = {}
    for i, line in enumerate(lines):
        if 0 < len(line) < 60:
            positions.setdefault(line, []).append(i)

    # A running header/footer repeats at roughly regular, page-sized
    # intervals throughout the document. Legitimate repeated narration
    # (a refrain, a repeated one-word paragraph) tends to cluster closely
    # together or appear at irregular intervals -- require both a minimum
    # gap and evenly-spaced repeats so those aren't mistaken for headers.
    repeated = set()
    for line, idxs in positions.items():
        if len(idxs) < 3:
            continue
        gaps = [b - a for a, b in zip(idxs, idxs[1:])]
        avg_gap = sum(gaps) / len(gaps)
        if avg_gap < 20:
            continue
        if all(abs(g - avg_gap) <= avg_gap * 0.5 for g in gaps):
            repeated.add(line)

    if not repeated:
        return text

    filtered = [line for line in lines if line not in repeated]
    return "\n".join(filtered)


# ---------------------------------------------------------------------------
# Speech normalization (applied after clean_text, right before chunk_text --
# run late so it doesn't disturb Mordret anchor matching, which is done
# against clean_text's output)
# ---------------------------------------------------------------------------

_CURLY_DOUBLE_QUOTE = re.compile(r"[“”]")
_CURLY_SINGLE_QUOTE = re.compile(r"[‘’]")
_REPEATED_BANG = re.compile(r"!{2,}")
_REPEATED_QUESTION = re.compile(r"\?{2,}")
# Ellipsis (...) is deliberately not matched here -- Piper reads it as a
# natural pause already. A lone em/en dash reads oddly, so it becomes a
# comma instead (a natural micro-pause), consuming any surrounding spaces
# so it doesn't leave "word ,  word" behind.
_EM_EN_DASH = re.compile(r"\s*[—–]\s*")


def normalize_for_speech(text):
    """
    Normalize punctuation so Piper reads it more naturally: straighten
    curly quotes/apostrophes, collapse repeated !!!/??? to a single mark,
    and turn em/en dashes into a comma. Sentence-ending abbreviations
    (Mr., Dr., etc.) are handled separately, in the sentence-splitter
    used by chunk_text (see _protect_abbreviations below).
    """
    text = _CURLY_DOUBLE_QUOTE.sub('"', text)
    text = _CURLY_SINGLE_QUOTE.sub("'", text)
    text = _REPEATED_BANG.sub("!", text)
    text = _REPEATED_QUESTION.sub("?", text)
    text = _EM_EN_DASH.sub(", ", text)
    return text


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

MIN_CHUNK = 1000
MAX_CHUNK = 2000


def chunk_text(text, min_size=MIN_CHUNK, max_size=MAX_CHUNK, return_boundaries=False):
    """
    Split text into chunks of roughly min_size-max_size characters.
    Prefers splitting at paragraph, then sentence, then word boundaries.
    Never cuts a word in half.

    If return_boundaries is True, also returns boundary_is_real: one bool
    per gap between consecutive chunks, True when that gap falls on a real
    paragraph break and False when it's a forced length-limit split mid
    paragraph -- callers use this to vary the pause length between chunks
    (see audio.join_wavs_to_mp3's boundary_is_real).
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return ([], []) if return_boundaries else []

    chunks = []
    ends_paragraph = []  # parallel to chunks: did chunks[i] end at a real paragraph boundary?
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para

        if len(candidate) <= max_size:
            current = candidate
            continue

        # Adding this paragraph would overflow.
        if current and len(current) >= min_size:
            chunks.append(current)
            ends_paragraph.append(True)
            current = ""
        elif current:
            # Leftover text is below min_size -- merge it into this
            # paragraph instead of discarding it when current is overwritten.
            para = f"{current}\n\n{para}"
            current = ""

        if len(para) <= max_size:
            current = para
        else:
            # Paragraph itself is too long; split by sentence/word.
            if current:
                chunks.append(current)
                ends_paragraph.append(True)
                current = ""
            sub_chunks = _split_long_paragraph(para, max_size)
            if sub_chunks:
                *complete, last = sub_chunks
                chunks.extend(complete)
                ends_paragraph.extend([False] * len(complete))
                current = last

    if current:
        chunks.append(current)
        ends_paragraph.append(True)

    if return_boundaries:
        boundary_is_real = ends_paragraph[:-1] if ends_paragraph else []
        return chunks, boundary_is_real
    return chunks


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Common abbreviations whose period would otherwise look like a sentence
# boundary to _SENTENCE_SPLIT (e.g. splitting "Dr. Smith" into two chunks
# right after "Dr."), causing an awkward pause/pitch reset mid-title.
_ABBREVIATIONS = ("Mr", "Mrs", "Ms", "Dr", "St", "Prof", "Sr", "Jr", "vs", "etc", "e.g", "i.e")
_ABBREV_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in _ABBREVIATIONS) + r")\.",
    re.IGNORECASE,
)
_ABBREV_PLACEHOLDER = "\x00"


def _protect_abbreviations(text):
    return _ABBREV_PATTERN.sub(lambda m: m.group(1) + _ABBREV_PLACEHOLDER, text)


def _restore_abbreviations(text):
    return text.replace(_ABBREV_PLACEHOLDER, ".")


def _split_long_paragraph(para, max_size):
    protected = _protect_abbreviations(para)
    sentences = [_restore_abbreviations(s) for s in _SENTENCE_SPLIT.split(protected)]
    chunks = []
    current = ""

    for sentence in sentences:
        candidate = f"{current} {sentence}" if current else sentence

        if len(candidate) <= max_size:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(sentence) <= max_size:
            current = sentence
        else:
            # Single sentence too long; split on word boundaries.
            words = sentence.split(" ")
            piece = ""
            for word in words:
                cand = f"{piece} {word}" if piece else word
                if len(cand) <= max_size:
                    piece = cand
                else:
                    if piece:
                        chunks.append(piece)
                    piece = word
            current = piece

    if current:
        chunks.append(current)

    return chunks


def safe_filename(name, max_len=60):
    name = re.sub(r"[^\w\s\-]", "", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:max_len] if name else "Untitled"
