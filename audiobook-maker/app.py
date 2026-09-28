"""
Audiobook Maker — a small, reliable PDF/EPUB/TXT -> audiobook tool
using local Piper TTS.

Run:
    python app.py

Pipeline:
    file -> extract text -> clean -> detect chapters -> chunk
          -> Piper TTS (per chunk, resumable) -> join chunks -> chapter MP3
          -> (optional) combine into Complete Audiobook.m4b
"""

import argparse
import os
import sys
import threading
import traceback

import text as textmod
import tts as ttsmod
import audio as audiomod
import mordret as mordretmod


# When frozen by PyInstaller, __file__ resolves inside the temp/bundle
# extraction dir, not next to the actual app on disk -- voices/ and
# output/ need to live next to the executable so they persist across
# launches instead of vanishing with the extraction dir. `python app.py`
# (unfrozen) keeps the original __file__-relative behavior.
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

VOICES_DIR = os.path.join(_APP_DIR, "voices")
OUTPUT_DIR = os.path.join(_APP_DIR, "output")


# ---------------------------------------------------------------------------
# Core pipeline (used by both CLI and UI)
# ---------------------------------------------------------------------------

class JobCancelled(Exception):
    """Raised inside a job when its cancel_event is set, so a long run can
    be stopped cleanly between chunks. Already-finished chunk WAVs stay on
    disk, so resuming later picks up exactly where the stop happened."""


class AudiobookJob:
    def __init__(self, input_path, voice_model_path=None, speed=1.0, book_title=None,
                 make_m4b=True, progress_callback=None, mordret_path=None, slot_voice_map=None,
                 speaker_id=None, start_chapter=None, end_chapter=None,
                 start_page=None, end_page=None, cancel_event=None):
        self.input_path = input_path
        self.voice_model_path = voice_model_path
        self.speed = speed
        # Optional threading.Event: when set, the job stops cleanly at the
        # next chunk boundary (raising JobCancelled) instead of running to
        # completion. Finished chunks remain on disk for a later resume.
        self.cancel_event = cancel_event

        # Range selection: chapter range (1-indexed, inclusive, against the
        # detected chapter list) works for PDF/EPUB/TXT and both single-voice
        # and Mordret. Page range (PDF only) restricts extraction itself, so
        # it can't be combined with a chapter range or with Mordret (which
        # needs a full extraction to resolve its chapter-index anchors).
        if (start_chapter is not None or end_chapter is not None) and \
                (start_page is not None or end_page is not None):
            raise ValueError("Chapter range and page range can't be used together -- pick one.")
        if mordret_path and (start_page is not None or end_page is not None):
            raise ValueError(
                "Page range isn't supported in multi-voice (Mordret) mode -- use a chapter range instead."
            )
        # Reject nonsense bounds up front rather than letting a 0 quietly
        # behave like 1 further down.
        for label, value in (("start_chapter", start_chapter), ("end_chapter", end_chapter),
                             ("start_page", start_page), ("end_page", end_page)):
            if value is not None and value < 1:
                raise ValueError(f"{label.replace('_', ' ')} must be 1 or greater (got {value}).")
        self.start_chapter = start_chapter
        self.end_chapter = end_chapter
        self.start_page = start_page
        self.end_page = end_page
        # speaker_id selects a speaker within a multi-speaker Piper model
        # (e.g. libritts, vctk) for voice_model_path. Only applies to that
        # one model -- per-slot Mordret voices always use their default
        # speaker, since slot_voice_map doesn't carry a speaker index.
        self.speaker_id = speaker_id
        self.book_title = book_title or os.path.splitext(os.path.basename(input_path))[0]
        self.make_m4b = make_m4b
        # progress_callback(message, progress_info) where progress_info is a
        # dict like {"chapter": 3, "total_chapters": 17, "chunk": 2,
        # "total_chunks": 5, "percent": 12.4} or None for non-progress messages.
        self.progress_callback = progress_callback or (lambda msg, info=None: None)

        # Mordret (multi-voice) mode: mordret_path points to a book.mordret.json
        # file, and slot_voice_map maps each voice slot id (e.g. "NARRATOR",
        # "M1", "F1") to a real Piper .onnx model path. When mordret_path is
        # None, the job runs in today's single-voice mode using voice_model_path
        # for everything -- Mordret is strictly opt-in and additive.
        self.mordret_path = mordret_path
        self.slot_voice_map = slot_voice_map or {}

        if self.mordret_path and not self.voice_model_path:
            # In Mordret mode, voice_model_path isn't required (each line
            # picks its own voice from slot_voice_map), but a fallback voice
            # is still useful for any slot the user didn't map.
            self.voice_model_path = next(iter(self.slot_voice_map.values()), None)

        self.book_output_dir = os.path.join(OUTPUT_DIR, textmod.safe_filename(self.book_title))

        self._total_chunks_all = 0
        self._done_chunks_all = 0

    def _report(self, msg, info=None):
        self.progress_callback(msg, info)

    def _check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelled("Generation stopped.")

    def _progress_info(self, chapter_index, total_chapters, chunk_index, total_chunks_here):
        percent = 0.0
        if self._total_chunks_all:
            percent = 100.0 * self._done_chunks_all / self._total_chunks_all
        return {
            "chapter": chapter_index,
            "total_chapters": total_chapters,
            "chunk": chunk_index,
            "total_chunks_here": total_chunks_here,
            "percent": percent,
        }

    def run(self):
        os.makedirs(self.book_output_dir, exist_ok=True)
        if self.mordret_path:
            return self._run_mordret()
        return self._run_single_voice()

    def _run_single_voice(self):
        self._report("Extracting text...")
        chapters = textmod.extract_chapters(
            self.input_path, start_page=self.start_page, end_page=self.end_page
        )  # list of (title, raw_text)
        total_detected_chapters = len(chapters)

        self._report(f"Found {len(chapters)} chapter(s). Cleaning and chunking...")
        chapter_chunks = []  # list of (real_chapter_number, title, [chunks], [boundary_is_real])
        for i, (title, raw_text) in enumerate(chapters, start=1):
            cleaned = textmod.clean_text(raw_text)
            normalized = textmod.normalize_for_speech(cleaned)
            chunks, boundaries = textmod.chunk_text(normalized, return_boundaries=True)
            if chunks:
                chapter_chunks.append((i, title, chunks, boundaries))

        if not chapter_chunks:
            raise ValueError("No usable text found after cleaning.")

        if self.start_chapter is not None or self.end_chapter is not None:
            start_c = self.start_chapter if self.start_chapter is not None else 1
            end_c = self.end_chapter if self.end_chapter is not None else total_detected_chapters
            if start_c < 1 or end_c > total_detected_chapters or start_c > end_c:
                raise ValueError(
                    f"Book has {total_detected_chapters} chapter(s); requested range "
                    f"{start_c}-{end_c} is out of bounds."
                )
            chapter_chunks = [c for c in chapter_chunks if start_c <= c[0] <= end_c]
            if not chapter_chunks:
                raise ValueError(f"No usable chapters found in requested range {start_c}-{end_c}.")
            self._report(f"Generating chapters {start_c}-{end_c} of {total_detected_chapters}.")

        total_chapters = len(chapter_chunks)
        self._total_chunks_all = sum(len(chunks) for _, _, chunks, _ in chapter_chunks)
        self._done_chunks_all = 0

        chapter_mp3_paths = []
        chapter_titles = []
        last_chapter_number = total_detected_chapters

        for chapter_number, title, chunks, boundaries in chapter_chunks:
            chapter_dir_name = f"chapter_{chapter_number:02d}"
            chapter_dir = os.path.join(self.book_output_dir, chapter_dir_name)
            os.makedirs(chapter_dir, exist_ok=True)

            total_chunks_here = len(chunks)
            last_chapter_number = chapter_number

            self._report(
                f"Chapter {chapter_number}/{total_detected_chapters}: {title}",
                self._progress_info(chapter_number, total_detected_chapters, 0, total_chunks_here),
            )

            chunk_wav_paths = []
            for chunk_index, chunk in enumerate(chunks, start=1):
                self._check_cancelled()
                wav_path = os.path.join(chapter_dir, f"{chunk_index:03d}.wav")
                chunk_wav_paths.append(wav_path)

                if audiomod.chunk_done(wav_path):
                    self._done_chunks_all += 1
                    self._report(
                        f"  Chunk {chunk_index}/{total_chunks_here} already done, skipping.",
                        self._progress_info(chapter_number, total_detected_chapters, chunk_index, total_chunks_here),
                    )
                    continue

                self._report(
                    f"  Generating chunk {chunk_index}/{total_chunks_here}...",
                    self._progress_info(chapter_number, total_detected_chapters, chunk_index, total_chunks_here),
                )
                tmp_path = wav_path + ".tmp"
                try:
                    ttsmod.text_to_speech(chunk, tmp_path, self.voice_model_path,
                                           speed=self.speed, speaker_id=self.speaker_id)
                    os.replace(tmp_path, wav_path)
                    audiomod.trim_silence_wav(wav_path)
                    self._done_chunks_all += 1
                except Exception:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise

            safe_title = textmod.safe_filename(title)
            chapter_mp3_name = f"{chapter_number:02d} - {safe_title}.mp3"
            chapter_mp3_path = os.path.join(self.book_output_dir, chapter_mp3_name)

            self._report(
                f"  Joining {len(chunk_wav_paths)} chunk(s) into chapter MP3...",
                self._progress_info(chapter_number, total_detected_chapters, total_chunks_here, total_chunks_here),
            )
            audiomod.join_wavs_to_mp3(chunk_wav_paths, chapter_mp3_path, boundary_is_real=boundaries)

            chapter_mp3_paths.append(chapter_mp3_path)
            chapter_titles.append(title)

        if self.make_m4b and audiomod.ffmpeg_available():
            self._report("Combining chapters into Complete Audiobook.m4b...",
                          self._progress_info(last_chapter_number, total_detected_chapters, 0, 0))
            m4b_path = os.path.join(self.book_output_dir, "Complete Audiobook.m4b")
            try:
                audiomod.combine_mp3s_to_m4b(
                    chapter_mp3_paths, chapter_titles, m4b_path, book_title=self.book_title
                )
            except Exception as e:
                self._report(f"  M4B creation skipped ({e}). MP3 chapters are still available.")

        self._report(f"Done. Output in: {self.book_output_dir}")
        return self.book_output_dir

    def _run_mordret(self):
        """
        Multi-voice generation path: reads a book.mordret.json file and
        generates each line in the voice mapped to its speaker slot,
        instead of chunking the whole book into one voice. Reuses the same
        chunk-file/resume/join/M4B machinery as single-voice mode -- the
        only real difference is where each chunk's text and voice come
        from.
        """
        self._report("Loading Mordret annotation file...")
        mordret_doc = mordretmod.load_mordret_file(self.mordret_path)

        if not mordretmod.source_file_matches(mordret_doc, self.input_path):
            self._report(
                f"  Warning: this Mordret file was annotated against "
                f"'{mordret_doc.get('source_file')}', but you're generating "
                f"'{os.path.basename(self.input_path)}'. Continuing anyway, "
                "but character voices may not align correctly if the text differs."
            )

        slot_map = mordretmod.build_slot_map(mordret_doc)
        missing_slots = self._missing_slot_voices(mordret_doc)
        if missing_slots:
            self._report(
                f"  Note: no voice assigned for slot(s) {', '.join(missing_slots)}; "
                "those characters will use the default voice instead."
            )

        self._report(f"Extracting text for {len(mordret_doc['chapters'])} annotated chapter(s)...")
        real_chapters = textmod.extract_chapters(self.input_path)  # [(title, raw_text), ...]
        total_book_chapters = len(real_chapters)

        start_c, end_c = None, None
        if self.start_chapter is not None or self.end_chapter is not None:
            start_c = self.start_chapter if self.start_chapter is not None else 1
            end_c = self.end_chapter if self.end_chapter is not None else total_book_chapters
            if start_c < 1 or end_c > total_book_chapters or start_c > end_c:
                raise ValueError(
                    f"Book has {total_book_chapters} chapter(s); requested range "
                    f"{start_c}-{end_c} is out of bounds."
                )
            self._report(f"Generating chapters {start_c}-{end_c} of {total_book_chapters}.")

        # Build each Mordret chapter's list of (speaker_slot, text) lines by
        # resolving anchors against the freshly-extracted, freshly-cleaned
        # real text -- never text stored in the Mordret file itself.
        chapter_lines = []  # list of (real_chapter_number, title, [(slot, text), ...], [boundary_is_real])
        for mchapter in mordret_doc["chapters"]:
            idx = mchapter["chapter_index"]
            if idx < 1 or idx > total_book_chapters:
                self._report(f"  Skipping chapter {idx}: out of range for the source file.")
                continue
            if start_c is not None and not (start_c <= idx <= end_c):
                continue
            title, raw_text = real_chapters[idx - 1]
            cleaned = textmod.clean_text(raw_text)
            resolved = mordretmod.resolve_chapter_lines(cleaned, mchapter["lines"])
            lines = [(slot_map.get(r["speaker"], "NARRATOR"), r["text"]) for r in resolved]
            # Merge out any zero-length lines (can happen if two anchors
            # resolve to the same position) so they don't produce empty
            # TTS chunks.
            lines = [(slot, text) for slot, text in lines if text.strip()]
            # A single Mordret line can be unusually long (e.g. a long
            # narration stretch) -- pre-split any such line into normal-sized
            # sub-pieces up front, so every entry in `lines` maps to exactly
            # one TTS call/one .wav file, keeping resume and joining logic
            # identical to the single-voice path. The gap before each new
            # line is always a real (speaker-turn) boundary; gaps between
            # pieces *within* one over-long line are whatever chunk_text
            # determined for that piece split.
            flat_lines = []
            boundaries = []
            for slot, text in lines:
                normalized = textmod.normalize_for_speech(text)
                pieces, piece_boundaries = textmod.chunk_text(normalized, return_boundaries=True)
                if not pieces:
                    pieces, piece_boundaries = [normalized], []
                for i, piece in enumerate(pieces):
                    if flat_lines:
                        boundaries.append(True if i == 0 else piece_boundaries[i - 1])
                    flat_lines.append((slot, piece))
            lines = flat_lines
            if lines:
                chapter_lines.append((idx, mchapter.get("chapter_title", title), lines, boundaries))

        if not chapter_lines:
            if start_c is not None:
                raise ValueError(f"No usable annotated lines found in requested range {start_c}-{end_c}.")
            raise ValueError("No usable annotated lines found after resolving the Mordret file.")

        total_chapters = len(chapter_lines)
        self._total_chunks_all = sum(len(lines) for _, _, lines, _ in chapter_lines)
        self._done_chunks_all = 0

        chapter_mp3_paths = []
        chapter_titles = []
        last_chapter_number = total_book_chapters

        for chapter_number, title, lines, boundaries in chapter_lines:
            chapter_dir = os.path.join(self.book_output_dir, f"chapter_{chapter_number:02d}")
            os.makedirs(chapter_dir, exist_ok=True)
            total_lines_here = len(lines)
            last_chapter_number = chapter_number

            self._report(
                f"Chapter {chapter_number}/{total_book_chapters}: {title}",
                self._progress_info(chapter_number, total_book_chapters, 0, total_lines_here),
            )

            chunk_wav_paths = []
            for line_index, (slot, text) in enumerate(lines, start=1):
                self._check_cancelled()
                wav_path = os.path.join(chapter_dir, f"{line_index:03d}.wav")
                chunk_wav_paths.append(wav_path)

                if audiomod.chunk_done(wav_path):
                    self._done_chunks_all += 1
                    self._report(
                        f"  Line {line_index}/{total_lines_here} ({slot}) already done, skipping.",
                        self._progress_info(chapter_number, total_book_chapters, line_index, total_lines_here),
                    )
                    continue

                voice_path = self.slot_voice_map.get(slot) or self.voice_model_path
                if not voice_path:
                    raise ValueError(
                        f"No voice available for slot '{slot}' and no default voice set."
                    )

                self._report(
                    f"  Generating line {line_index}/{total_lines_here} (voice: {slot})...",
                    self._progress_info(chapter_number, total_book_chapters, line_index, total_lines_here),
                )
                tmp_path = wav_path + ".tmp"
                # A per-line speaker id isn't tracked in slot_voice_map, so
                # only apply speaker_id when this line fell back to the
                # single default voice_model_path -- slot-specific voices
                # always use their model's default speaker.
                line_speaker_id = self.speaker_id if voice_path == self.voice_model_path else None
                try:
                    ttsmod.text_to_speech(text, tmp_path, voice_path,
                                           speed=self.speed, speaker_id=line_speaker_id)
                    os.replace(tmp_path, wav_path)
                    audiomod.trim_silence_wav(wav_path)
                    self._done_chunks_all += 1
                except Exception:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise

            safe_title = textmod.safe_filename(title)
            chapter_mp3_path = os.path.join(self.book_output_dir, f"{chapter_number:02d} - {safe_title}.mp3")

            self._report(
                f"  Joining {len(chunk_wav_paths)} line(s) into chapter MP3...",
                self._progress_info(chapter_number, total_book_chapters, total_lines_here, total_lines_here),
            )
            audiomod.join_wavs_to_mp3(chunk_wav_paths, chapter_mp3_path, boundary_is_real=boundaries)

            chapter_mp3_paths.append(chapter_mp3_path)
            chapter_titles.append(title)

        if self.make_m4b and audiomod.ffmpeg_available():
            self._report("Combining chapters into Complete Audiobook.m4b...",
                          self._progress_info(last_chapter_number, total_book_chapters, 0, 0))
            m4b_path = os.path.join(self.book_output_dir, "Complete Audiobook.m4b")
            try:
                audiomod.combine_mp3s_to_m4b(
                    chapter_mp3_paths, chapter_titles, m4b_path, book_title=self.book_title
                )
            except Exception as e:
                self._report(f"  M4B creation skipped ({e}). MP3 chapters are still available.")

        self._report(f"Done. Output in: {self.book_output_dir}")
        return self.book_output_dir

    def _missing_slot_voices(self, mordret_doc):
        """Which slots actually used by characters in this book have no
        real voice assigned in slot_voice_map."""
        used_slots = {c["slot"] for c in mordret_doc["characters"]}
        used_slots.add("NARRATOR")
        return sorted(s for s in used_slots if s not in self.slot_voice_map)


def preview_voice(voice_model_path, speed, sample_text=None, output_path=None, speaker_id=None):
    sample_text = sample_text or (
        "This is a preview of the selected voice, so you can check "
        "how it sounds before generating the full audiobook."
    )
    output_path = output_path or os.path.join(OUTPUT_DIR, "_preview.wav")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    ttsmod.text_to_speech(sample_text, output_path, voice_model_path, speed=speed, speaker_id=speaker_id)
    return output_path


# ---------------------------------------------------------------------------
# Simple Tkinter UI
# ---------------------------------------------------------------------------

def launch_ui():
    import tkinter as tk
    from tkinter import filedialog, ttk, messagebox
    import subprocess
    import platform
    import shutil
    import time

    # Placeholder rows shown in the voice dropdown when there's nothing real
    # to pick. They must never be treated as filenames -- see get_voice_path.
    NO_VOICES = "(no voices found in ./voices)"
    NO_MATCHES = "(no voices match your search)"
    PLACEHOLDERS = (NO_VOICES, NO_MATCHES)

    # -----------------------------------------------------------------
    # Theme
    # -----------------------------------------------------------------
    BG = "#F5F4F1"        # window background (warm off-white)
    CARD = "#FFFFFF"      # card surface
    BORDER = "#E2DFD8"
    INK = "#1E1C1A"       # primary text
    MUTED = "#6E6862"     # secondary text
    ACCENT = "#B4531F"    # warm terracotta -- primary action
    ACCENT_DK = "#8F4117"  # pressed/hover state for the primary action
    DANGER = "#B3261E"
    OK_GREEN = "#2E7D52"

    root = tk.Tk()
    root.title("Audiobook Maker")
    # Resizable with a sane minimum: the window used to be a fixed size
    # smaller than its own content, which clipped the primary action off
    # the bottom. Content now scrolls and the action bar is pinned.
    root.geometry("620x760")
    root.minsize(520, 480)
    root.configure(bg=BG)

    style = ttk.Style(root)
    # 'clam' honours custom colors on every platform; the native macOS
    # theme ignores most of them, which is what made the old UI look
    # unstyled and inconsistent across sections.
    if "clam" in style.theme_names():
        style.theme_use("clam")

    base_font = ("Helvetica", 12)
    style.configure(".", background=BG, foreground=INK, font=base_font)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD, relief="flat")
    style.configure("TLabel", background=BG, foreground=INK, font=base_font)
    style.configure("Card.TLabel", background=CARD, foreground=INK, font=base_font)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=("Helvetica", 11))
    style.configure("CardValue.TLabel", background=CARD, foreground=INK, font=("Helvetica", 12, "bold"))
    style.configure("Title.TLabel", background=BG, foreground=INK, font=("Helvetica", 22, "bold"))
    style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Helvetica", 12))
    style.configure("StepNum.TLabel", background=CARD, foreground=ACCENT, font=("Helvetica", 11, "bold"))
    style.configure("CardTitle.TLabel", background=CARD, foreground=INK, font=("Helvetica", 13, "bold"))
    style.configure("Status.TLabel", background=BG, foreground=MUTED, font=("Helvetica", 11))
    style.configure("Percent.TLabel", background=BG, foreground=INK, font=("Helvetica", 12, "bold"))

    style.configure("TButton", font=base_font, padding=(12, 7),
                    background="#EDEAE4", foreground=INK, borderwidth=0, focuscolor=BG)
    style.map("TButton",
              background=[("active", "#E2DED6"), ("disabled", "#F0EEE9")],
              foreground=[("disabled", "#AEA9A2")])
    style.configure("Card.TButton", font=base_font, padding=(12, 7),
                    background="#EFECE6", foreground=INK, borderwidth=0)
    style.map("Card.TButton",
              background=[("active", "#E4E0D8"), ("disabled", "#F4F2EE")],
              foreground=[("disabled", "#AEA9A2")])
    style.configure("Primary.TButton", font=("Helvetica", 13, "bold"), padding=(18, 11),
                    background=ACCENT, foreground="#FFFFFF", borderwidth=0)
    style.map("Primary.TButton",
              background=[("active", ACCENT_DK), ("disabled", "#D8CFC7")],
              foreground=[("disabled", "#FFFFFF")])
    style.configure("Danger.TButton", font=base_font, padding=(14, 10),
                    background="#F3E3E1", foreground=DANGER, borderwidth=0)
    style.map("Danger.TButton", background=[("active", "#EBD4D1")])

    style.configure("TRadiobutton", background=CARD, foreground=INK, font=base_font)
    style.map("TRadiobutton", background=[("active", CARD)],
              foreground=[("disabled", "#B3AEA7")])
    style.configure("TEntry", fieldbackground="#FFFFFF", bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, borderwidth=1, padding=6)
    style.configure("TCombobox", fieldbackground="#FFFFFF", background="#FFFFFF",
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    borderwidth=1, padding=6)
    style.configure("TSpinbox", fieldbackground="#FFFFFF", bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, borderwidth=1, padding=4)
    style.configure("Horizontal.TProgressbar", troughcolor="#E6E2DB", background=ACCENT,
                    borderwidth=0, thickness=8)
    style.configure("Horizontal.TScale", background=CARD, troughcolor="#E6E2DB")

    state = {"input_path": None, "chapters": None, "running": False,
             "cancel_event": None, "last_output_dir": None, "started_at": None}

    # -----------------------------------------------------------------
    # Header
    # -----------------------------------------------------------------
    header = ttk.Frame(root, padding=(24, 20, 24, 12))
    header.pack(fill="x")
    ttk.Label(header, text="Audiobook Maker", style="Title.TLabel").pack(anchor="w")
    ttk.Label(header, text="Turn a PDF, EPUB, or TXT into a narrated audiobook — fully offline.",
              style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))

    # -----------------------------------------------------------------
    # Scrollable body. The action bar below is pinned outside this area,
    # so the primary button can never be pushed off-screen.
    # -----------------------------------------------------------------
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True)

    canvas = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
    vscroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vscroll.set)
    canvas.pack(side="left", fill="both", expand=True)

    content = ttk.Frame(canvas, padding=(24, 4, 24, 16))
    content_id = canvas.create_window((0, 0), window=content, anchor="nw")

    def _sync_scroll(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(content_id, width=canvas.winfo_width())
        # Only show the scrollbar when the content actually overflows.
        needs_scroll = content.winfo_reqheight() > canvas.winfo_height()
        if needs_scroll and not vscroll.winfo_ismapped():
            vscroll.pack(side="right", fill="y")
        elif not needs_scroll and vscroll.winfo_ismapped():
            vscroll.pack_forget()

    content.bind("<Configure>", _sync_scroll)
    canvas.bind("<Configure>", _sync_scroll)

    def _on_mousewheel(event):
        if content.winfo_reqheight() <= canvas.winfo_height():
            return
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        else:
            # macOS reports small deltas; Windows reports multiples of 120.
            delta = -1 * (event.delta if abs(event.delta) < 20 else int(event.delta / 120))
        canvas.yview_scroll(delta, "units")

    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(seq, _on_mousewheel)

    def make_card(step, title, subtitle=None):
        """One titled section. Returns the frame to put controls in."""
        outer = tk.Frame(content, bg=BORDER, bd=0)
        outer.pack(fill="x", pady=(0, 14))
        card = ttk.Frame(outer, style="Card.TFrame", padding=(16, 14))
        card.pack(fill="both", expand=True, padx=1, pady=1)

        head = ttk.Frame(card, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=f"{step}", style="StepNum.TLabel").pack(side="left")
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left", padx=(8, 0))
        if subtitle:
            ttk.Label(card, text=subtitle, style="CardMuted.TLabel",
                      wraplength=520, justify="left").pack(anchor="w", pady=(2, 0))
        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill="x", pady=(10, 0))
        return inner

    # -----------------------------------------------------------------
    # 1. Book
    # -----------------------------------------------------------------
    book_card = make_card("1", "Book")

    file_row = ttk.Frame(book_card, style="Card.TFrame")
    file_row.pack(fill="x")

    def select_file():
        path = filedialog.askopenfilename(
            title="Choose a book",
            filetypes=[("Books", "*.pdf *.epub *.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        state["input_path"] = path
        file_name_var.set(os.path.basename(path))

        details = []
        try:
            chapters = textmod.extract_chapters(path)
            state["chapters"] = chapters
            details.append(f"{len(chapters)} chapter(s)")
            chapter_hint_var.set(f"of {len(chapters)}")
            chapter_end_var.set(str(len(chapters)))
            chapter_start_var.set("1")
        except Exception as e:
            # Not just ValueError: a corrupt PDF raises pymupdf's own
            # FileDataError, which used to escape this callback entirely
            # and leave the UI showing a file it couldn't actually read.
            state["chapters"] = None
            chapter_hint_var.set("")
            file_detail_var.set("Couldn't read this file.")
            messagebox.showerror(
                "Couldn't read that file",
                f"{e}\n\nTry a different file, or re-export it if it's damaged."
            )
            _refresh_ready_state()
            return

        is_pdf = path.lower().endswith(".pdf")
        if is_pdf:
            try:
                pages = textmod.pdf_page_count(path)
                details.append(f"{pages} page(s)")
                page_hint_var.set(f"of {pages}")
                page_start_var.set("1")
                page_end_var.set(str(pages))
            except ValueError:
                page_hint_var.set("")
        else:
            page_hint_var.set("")
            if range_mode_var.get() == "page":
                range_mode_var.set("whole")
        page_radio.config(state="normal" if is_pdf else "disabled")

        file_detail_var.set(" · ".join(details) if details else "")
        _on_range_mode_change()
        _refresh_ready_state()
        _maybe_offer_detected_mordret_file(path)

    ttk.Button(file_row, text="Choose file…", style="Card.TButton",
               command=select_file).pack(side="left")

    file_text = ttk.Frame(file_row, style="Card.TFrame")
    file_text.pack(side="left", fill="x", expand=True, padx=(12, 0))
    file_name_var = tk.StringVar(value="No file selected")
    ttk.Label(file_text, textvariable=file_name_var, style="CardValue.TLabel",
              wraplength=340, justify="left").pack(anchor="w")
    file_detail_var = tk.StringVar(value="PDF, EPUB, or TXT")
    ttk.Label(file_text, textvariable=file_detail_var, style="CardMuted.TLabel").pack(anchor="w")

    # -----------------------------------------------------------------
    # 2. Voice
    # -----------------------------------------------------------------
    voice_card = make_card("2", "Voice")

    voice_files = ttsmod.list_available_voices(VOICES_DIR)

    def _build_display_names(files):
        """Speaker name first (that's what people search by), followed by
        its gender/locale/quality tags. Sorted into gender groups so a
        large library still reads in a predictable order."""
        if not files:
            return [NO_VOICES], {}, {}
        cats = ttsmod.categorize_voices(files, voices_dir=VOICES_DIR)
        group_order = {"Female": 0, "Male": 1, "Unknown": 2}
        quality_order = {"high": 0, "medium": 1, "low": 2, "x_low": 3}

        def sort_key(f):
            c = cats[f]
            return (group_order.get(c["gender"], 3),
                    quality_order.get(c["quality"], 4), c["locale"], f)

        display_names, mapping = [], {}
        for f in sorted(files, key=sort_key):
            c = cats[f]
            speaker = ttsmod.parse_voice_filename(f)["speaker"]
            label = f"{speaker}  ·  {c['gender']} · {c['locale']} · {c['quality']}"
            if c["multi_speaker"]:
                label += f" · {c['num_speakers']} speakers"
            display_names.append(label)
            mapping[label] = f
        return display_names, mapping, cats

    voice_display_names, voice_display_to_file, voice_categories = _build_display_names(voice_files)

    search_row = ttk.Frame(voice_card, style="Card.TFrame")
    search_row.pack(fill="x")
    voice_filter_var = tk.StringVar(value="")
    search_entry = ttk.Entry(search_row, textvariable=voice_filter_var)
    search_entry.pack(side="left", fill="x", expand=True)
    voice_count_var = tk.StringVar(value="")
    ttk.Label(search_row, textvariable=voice_count_var, style="CardMuted.TLabel").pack(side="left", padx=(10, 0))
    ttk.Button(search_row, text="Refresh", style="Card.TButton",
               command=lambda: refresh_voices(auto=False)).pack(side="left", padx=(8, 0))

    # Placeholder text inside the search box, cleared on first focus.
    _search_ph = "Search voices by name, locale, or quality…"

    def _ph_clear(_e=None):
        if search_entry.get() == _search_ph:
            search_entry.delete(0, "end")
            search_entry.configure(foreground=INK)

    def _ph_restore(_e=None):
        if not search_entry.get():
            search_entry.insert(0, _search_ph)
            search_entry.configure(foreground=MUTED)

    search_entry.insert(0, _search_ph)
    search_entry.configure(foreground=MUTED)
    search_entry.bind("<FocusIn>", _ph_clear)
    search_entry.bind("<FocusOut>", _ph_restore)

    voice_var = tk.StringVar(value=voice_display_names[0])
    voice_menu = ttk.Combobox(voice_card, textvariable=voice_var,
                              values=voice_display_names, state="readonly")
    voice_menu.pack(fill="x", pady=(8, 0))

    # Speaker index: only meaningful for multi-speaker models (libritts,
    # vctk, …) which pack many voices into one file.
    speaker_row = ttk.Frame(voice_card, style="Card.TFrame")
    ttk.Label(speaker_row, text="Speaker index", style="Card.TLabel").pack(side="left")
    speaker_id_var = tk.IntVar(value=0)
    speaker_spin = ttk.Spinbox(speaker_row, from_=0, to=0, textvariable=speaker_id_var, width=6)
    speaker_spin.pack(side="left", padx=(10, 0))
    ttk.Label(speaker_row, text="this model contains multiple speakers",
              style="CardMuted.TLabel").pack(side="left", padx=(10, 0))

    def _current_voice_filename():
        return voice_display_to_file.get(voice_var.get())

    def _update_speaker_controls(*_args):
        f = _current_voice_filename()
        cats = voice_categories.get(f) if f else None
        if cats and cats["multi_speaker"]:
            speaker_spin.config(to=max(cats["num_speakers"] - 1, 0))
            if speaker_id_var.get() >= cats["num_speakers"]:
                speaker_id_var.set(0)
            speaker_row.pack(fill="x", pady=(8, 0))
        else:
            speaker_row.pack_forget()
            speaker_id_var.set(0)

    def _apply_voice_filter(*_args):
        raw = voice_filter_var.get()
        query = "" if raw == _search_ph else raw.strip().lower()
        if not voice_display_to_file:
            filtered = [NO_VOICES]
        else:
            filtered = [d for d in voice_display_names if query in d.lower()] if query \
                else list(voice_display_names)
            if not filtered:
                filtered = [NO_MATCHES]
        voice_menu["values"] = filtered
        if voice_var.get() not in filtered:
            voice_var.set(filtered[0])
        total = len(voice_display_to_file)
        shown = len([f for f in filtered if f not in PLACEHOLDERS])
        voice_count_var.set(f"{shown} of {total}" if query else f"{total} voice(s)")
        _update_speaker_controls()
        _refresh_ready_state()

    voice_filter_var.trace_add("write", _apply_voice_filter)
    voice_menu.bind("<<ComboboxSelected>>", lambda e: (_update_speaker_controls(),
                                                        _refresh_ready_state()))

    def refresh_voices(auto=False):
        nonlocal voice_files, voice_display_names, voice_display_to_file, voice_categories
        voice_files = ttsmod.list_available_voices(VOICES_DIR)
        incomplete = ttsmod.list_incomplete_voices(VOICES_DIR)
        voice_display_names, voice_display_to_file, voice_categories = _build_display_names(voice_files)
        _apply_voice_filter()
        if not auto:
            msg = f"Found {len(voice_files)} usable voice(s)." if voice_files \
                else f"No voices found in:\n{VOICES_DIR}"
            if incomplete:
                names = ", ".join(incomplete)
                msg += (f"\n\nNote: {names} is missing its matching .onnx.json "
                        "config file, so it can't be used yet. Make sure you "
                        "downloaded both files for that voice.")
            messagebox.showinfo("Voices refreshed", msg)

    speed_row = ttk.Frame(voice_card, style="Card.TFrame")
    speed_row.pack(fill="x", pady=(14, 0))
    ttk.Label(speed_row, text="Speed", style="Card.TLabel").pack(side="left")
    speed_value_var = tk.StringVar(value="1.0×")
    ttk.Label(speed_row, textvariable=speed_value_var, style="CardValue.TLabel").pack(side="right")
    speed_var = tk.DoubleVar(value=1.0)
    speed_scale = ttk.Scale(voice_card, from_=0.5, to=2.0, variable=speed_var, orient="horizontal")
    speed_scale.pack(fill="x", pady=(4, 0))
    ttk.Label(voice_card, text="0.9–1.0× sounds the most natural for long-form narration.",
              style="CardMuted.TLabel").pack(anchor="w", pady=(2, 0))

    def on_speed_change(_):
        speed_value_var.set(f"{speed_var.get():.1f}×")

    speed_scale.config(command=on_speed_change)

    def get_voice_path():
        display_name = voice_var.get()
        # The dropdown can legitimately be showing a placeholder row (no
        # voices installed, or a search that matched nothing). Those are
        # not filenames -- joining them produced a bogus path that only
        # failed later, mid-generation, inside Piper.
        if not voice_files or display_name in PLACEHOLDERS or display_name not in voice_display_to_file:
            if not voice_files:
                messagebox.showerror(
                    "No voices installed",
                    f"No Piper voice models found in:\n{VOICES_DIR}\n\n"
                    "Download a .onnx voice and its matching .onnx.json config from the "
                    "Piper voices repository and put both files in the 'voices' folder, "
                    "then click Refresh."
                )
            else:
                messagebox.showerror(
                    "No voice selected",
                    "Your search doesn't match any installed voice, so no voice is "
                    "selected.\n\nClear the search box and pick a voice from the list."
                )
            return None
        return os.path.join(VOICES_DIR, voice_display_to_file[display_name])

    def get_speaker_id():
        f = _current_voice_filename()
        cats = voice_categories.get(f) if f else None
        if cats and cats["multi_speaker"]:
            return speaker_id_var.get()
        return None

    def do_preview():
        if state["running"]:
            return
        voice_path = get_voice_path()
        if not voice_path:
            return
        speaker_id = get_speaker_id()
        preview_btn.config(state="disabled")
        set_status("Generating preview…")

        def work():
            try:
                out_path = preview_voice(voice_path, speed_var.get(), speaker_id=speaker_id)
                root.after(0, lambda: set_status("Playing preview…"))
                system = platform.system()
                if system == "Darwin":
                    subprocess.run(["afplay", out_path])
                elif system == "Windows":
                    import winsound
                    winsound.PlaySound(out_path, winsound.SND_FILENAME)
                elif system == "Linux":
                    for player in ("paplay", "aplay", "ffplay"):
                        if shutil.which(player):
                            extra = ["-nodisp", "-autoexit"] if player == "ffplay" else []
                            subprocess.run([player, *extra, out_path])
                            break
                root.after(0, lambda: set_status("Preview finished."))
            except Exception as e:
                err = str(e)
                root.after(0, lambda: set_status(f"Preview failed: {err}"))
            finally:
                root.after(0, lambda: preview_btn.config(
                    state="disabled" if state["running"] else "normal"))

        threading.Thread(target=work, daemon=True).start()

    preview_btn = ttk.Button(voice_card, text="▶  Preview this voice",
                             style="Card.TButton", command=do_preview)
    preview_btn.pack(anchor="w", pady=(12, 0))

    # -----------------------------------------------------------------
    # 3. Range
    # -----------------------------------------------------------------
    range_card = make_card("3", "Range", "Generate the whole book, or just part of it.")

    range_mode_var = tk.StringVar(value="whole")

    mode_row = ttk.Frame(range_card, style="Card.TFrame")
    mode_row.pack(fill="x")
    ttk.Radiobutton(mode_row, text="Whole book", variable=range_mode_var, value="whole",
                    command=lambda: _on_range_mode_change()).pack(side="left")
    ttk.Radiobutton(mode_row, text="Chapters", variable=range_mode_var, value="chapter",
                    command=lambda: _on_range_mode_change()).pack(side="left", padx=(16, 0))
    page_radio = ttk.Radiobutton(mode_row, text="Pages", variable=range_mode_var, value="page",
                                 command=lambda: _on_range_mode_change(), state="disabled")
    page_radio.pack(side="left", padx=(16, 0))

    chapter_row = ttk.Frame(range_card, style="Card.TFrame")
    ttk.Label(chapter_row, text="Chapter", style="Card.TLabel").pack(side="left")
    chapter_start_var = tk.StringVar(value="")
    chapter_end_var = tk.StringVar(value="")
    ttk.Entry(chapter_row, textvariable=chapter_start_var, width=6).pack(side="left", padx=(10, 4))
    ttk.Label(chapter_row, text="to", style="CardMuted.TLabel").pack(side="left")
    ttk.Entry(chapter_row, textvariable=chapter_end_var, width=6).pack(side="left", padx=(4, 8))
    chapter_hint_var = tk.StringVar(value="")
    ttk.Label(chapter_row, textvariable=chapter_hint_var, style="CardMuted.TLabel").pack(side="left")

    page_row = ttk.Frame(range_card, style="Card.TFrame")
    ttk.Label(page_row, text="Page", style="Card.TLabel").pack(side="left")
    page_start_var = tk.StringVar(value="")
    page_end_var = tk.StringVar(value="")
    ttk.Entry(page_row, textvariable=page_start_var, width=6).pack(side="left", padx=(10, 4))
    ttk.Label(page_row, text="to", style="CardMuted.TLabel").pack(side="left")
    ttk.Entry(page_row, textvariable=page_end_var, width=6).pack(side="left", padx=(4, 8))
    page_hint_var = tk.StringVar(value="")
    ttk.Label(page_row, textvariable=page_hint_var, style="CardMuted.TLabel").pack(side="left")

    pdf_only_note = ttk.Label(range_card, text="Page ranges are available for PDF files only.",
                              style="CardMuted.TLabel")

    def _on_range_mode_change():
        mode = range_mode_var.get()
        chapter_row.pack_forget()
        page_row.pack_forget()
        pdf_only_note.pack_forget()
        if mode == "chapter":
            chapter_row.pack(fill="x", pady=(10, 0))
        elif mode == "page":
            page_row.pack(fill="x", pady=(10, 0))
        elif str(page_radio.cget("state")) == "disabled" and state["input_path"]:
            pdf_only_note.pack(anchor="w", pady=(8, 0))
        _sync_scroll()

    def get_range_kwargs():
        """Validate the Range card and return AudiobookJob kwargs, or None
        (after showing an error) when the entered range doesn't make sense."""
        mode = range_mode_var.get()
        if mode == "whole":
            return {}

        if mode == "chapter":
            first_var, last_var, noun = chapter_start_var, chapter_end_var, "Chapter"
            limit = len(state["chapters"]) if state["chapters"] else None
        else:
            first_var, last_var, noun = page_start_var, page_end_var, "Page"
            limit = None
            if state["input_path"] and state["input_path"].lower().endswith(".pdf"):
                try:
                    limit = textmod.pdf_page_count(state["input_path"])
                except ValueError:
                    limit = None

        def read(var, label):
            raw = var.get().strip()
            if not raw:
                return None
            try:
                value = int(raw)
            except ValueError:
                messagebox.showerror("Invalid range", f"{label} must be a whole number.")
                return "invalid"
            if value < 1:
                messagebox.showerror("Invalid range", f"{label} must be 1 or greater.")
                return "invalid"
            return value

        first = read(first_var, f"{noun} start")
        if first == "invalid":
            return None
        last = read(last_var, f"{noun} end")
        if last == "invalid":
            return None
        if first is not None and last is not None and first > last:
            messagebox.showerror(
                "Invalid range",
                f"{noun} start ({first}) is after {noun.lower()} end ({last})."
            )
            return None
        if limit and last is not None and last > limit:
            messagebox.showerror(
                "Invalid range",
                f"This book only has {limit} {noun.lower()}(s), but you asked for {last}."
            )
            return None

        if mode == "chapter":
            return {"start_chapter": first, "end_chapter": last}
        return {"start_page": first, "end_page": last}

    # -----------------------------------------------------------------
    # 4. Multi-voice (Mordret)
    # -----------------------------------------------------------------
    mordret_card = make_card(
        "4", "Multi-voice",
        "Optional: give each character their own voice using a Mordret annotation file.")

    mordret_state = {"path": None, "doc": None, "slot_voice_map": {}}

    mordret_status_var = tk.StringVar(value="No Mordret file — the whole book uses one voice.")
    ttk.Label(mordret_card, textvariable=mordret_status_var, style="CardMuted.TLabel",
              wraplength=520, justify="left").pack(anchor="w")

    def _suggest_slot_voices(doc):
        """Fill in a sensible default voice for each slot that doesn't have
        one yet: distinct female voices for F-slots, distinct male voices
        for M-slots, preferring higher quality tiers. Never overrides a
        slot the user already set -- this only removes the "(none)" dead
        end so a freshly loaded file is immediately playable."""
        if not voice_files:
            return {}

        def quality_rank(f):
            return {"high": 0, "medium": 1, "low": 2, "x_low": 3}.get(
                voice_categories[f]["quality"], 4)

        ranked = sorted(voice_files, key=quality_rank)
        by_gender = {"Female": [], "Male": [], "Unknown": []}
        for f in ranked:
            by_gender[voice_categories[f]["gender"]].append(f)

        used_slots = sorted({c["slot"] for c in doc["characters"]} | {"NARRATOR"})
        suggestion, female_i, male_i = {}, 0, 0
        for slot in used_slots:
            if slot in mordret_state["slot_voice_map"]:
                continue
            if slot.startswith("F") and by_gender["Female"]:
                choice = by_gender["Female"][female_i % len(by_gender["Female"])]
                female_i += 1
            elif slot.startswith("M") and by_gender["Male"]:
                choice = by_gender["Male"][male_i % len(by_gender["Male"])]
                male_i += 1
            elif by_gender["Unknown"]:
                choice = by_gender["Unknown"][0]
            elif ranked:
                choice = ranked[0]
            else:
                continue
            suggestion[slot] = os.path.join(VOICES_DIR, choice)
        return suggestion

    def _apply_mordret_doc(path, doc):
        mordret_state["path"] = path
        mordret_state["doc"] = doc
        suggestion = _suggest_slot_voices(doc)
        mordret_state["slot_voice_map"].update(suggestion)
        note = "  Default voices were assigned automatically — use Assign voices to change them." \
            if suggestion else ""
        mordret_status_var.set(
            f"{os.path.basename(path)} — {len(doc['characters'])} character(s), "
            f"{len(doc['chapters'])} chapter(s) annotated.{note}"
        )
        configure_voices_btn.config(state="normal")
        _refresh_ready_state()

    def select_mordret_file():
        path = filedialog.askopenfilename(
            title="Choose a Mordret file",
            filetypes=[("Mordret files", "*.mordret.json *.json")])
        if not path:
            return
        try:
            doc = mordretmod.load_mordret_file(path)
        except ValueError as e:
            messagebox.showerror("Invalid Mordret file", str(e))
            return
        _apply_mordret_doc(path, doc)

    def _maybe_offer_detected_mordret_file(input_path):
        """Auto-check next to the book (and in its output folder) for a
        matching annotation file, so the common case needs no browsing."""
        if mordret_state["path"]:
            return
        book_title = os.path.splitext(os.path.basename(input_path))[0]
        book_output_dir = os.path.join(OUTPUT_DIR, textmod.safe_filename(book_title))
        candidate = mordretmod.find_candidate_mordret_file(
            input_path, extra_search_dirs=[book_output_dir])
        if not candidate:
            return
        try:
            doc = mordretmod.load_mordret_file(candidate)
        except ValueError:
            return
        if messagebox.askyesno(
            "Multi-voice file detected",
            f"Found a matching annotation file next to this book:\n"
            f"{os.path.basename(candidate)}\n\nUse it so each character gets their own voice?"
        ):
            _apply_mordret_doc(candidate, doc)

    def clear_mordret_file():
        mordret_state.update({"path": None, "doc": None, "slot_voice_map": {}})
        mordret_status_var.set("No Mordret file — the whole book uses one voice.")
        configure_voices_btn.config(state="disabled")
        _refresh_ready_state()

    def configure_voices():
        doc = mordret_state["doc"]
        if not doc:
            return
        used_slots = sorted({c["slot"] for c in doc["characters"]} | {"NARRATOR"})

        win = tk.Toplevel(root)
        win.title("Assign voices")
        win.configure(bg=BG)
        win.geometry("560x460")
        win.minsize(460, 320)
        win.transient(root)

        head = ttk.Frame(win, padding=(20, 18, 20, 8))
        head.pack(fill="x")
        ttk.Label(head, text="Assign voices", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text="Pick a voice for each slot used in this book.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))

        rows_wrap = tk.Frame(win, bg=BG)
        rows_wrap.pack(fill="both", expand=True, padx=20)
        rows_canvas = tk.Canvas(rows_wrap, bg=BG, highlightthickness=0, bd=0)
        rows_scroll = ttk.Scrollbar(rows_wrap, orient="vertical", command=rows_canvas.yview)
        rows_canvas.configure(yscrollcommand=rows_scroll.set)
        rows_canvas.pack(side="left", fill="both", expand=True)
        rows_scroll.pack(side="right", fill="y")
        rows = ttk.Frame(rows_canvas)
        rows_win = rows_canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda e: (
            rows_canvas.configure(scrollregion=rows_canvas.bbox("all")),
            rows_canvas.itemconfigure(rows_win, width=rows_canvas.winfo_width())))
        rows_canvas.bind("<Configure>",
                         lambda e: rows_canvas.itemconfigure(rows_win, width=e.width))

        # Show the same friendly labels as the main picker rather than raw
        # .onnx filenames, so the two lists are recognisably the same thing.
        slot_options = ["(none)"] + voice_display_names if voice_display_to_file else ["(none)"]
        file_to_display = {v: k for k, v in voice_display_to_file.items()}

        slot_vars = {}
        for slot in used_slots:
            row = tk.Frame(rows, bg=BORDER)
            row.pack(fill="x", pady=(0, 8))
            inner = ttk.Frame(row, style="Card.TFrame", padding=(12, 10))
            inner.pack(fill="x", padx=1, pady=1)

            chars_here = [c["name"] for c in doc["characters"] if c["slot"] == slot]
            title = "NARRATOR" if slot == "NARRATOR" else slot
            ttk.Label(inner, text=title, style="CardValue.TLabel").pack(anchor="w")
            ttk.Label(inner,
                      text=", ".join(chars_here) if chars_here else "narration",
                      style="CardMuted.TLabel", wraplength=460, justify="left").pack(anchor="w")

            current_file = mordret_state["slot_voice_map"].get(slot, "")
            current_display = file_to_display.get(os.path.basename(current_file), "(none)") \
                if current_file else "(none)"
            var = tk.StringVar(value=current_display)
            ttk.Combobox(inner, textvariable=var, values=slot_options,
                         state="readonly").pack(fill="x", pady=(8, 0))
            slot_vars[slot] = var

        def save_and_close():
            for slot, var in slot_vars.items():
                choice = var.get()
                filename = voice_display_to_file.get(choice)
                if choice and choice != "(none)" and filename:
                    mordret_state["slot_voice_map"][slot] = os.path.join(VOICES_DIR, filename)
                else:
                    mordret_state["slot_voice_map"].pop(slot, None)
            win.destroy()
            _refresh_ready_state()

        footer = ttk.Frame(win, padding=(20, 12, 20, 18))
        footer.pack(fill="x")
        ttk.Button(footer, text="Save", style="Primary.TButton",
                   command=save_and_close).pack(side="right")
        ttk.Button(footer, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))

    mordret_btn_row = ttk.Frame(mordret_card, style="Card.TFrame")
    mordret_btn_row.pack(fill="x", pady=(12, 0))
    ttk.Button(mordret_btn_row, text="Select Mordret file…", style="Card.TButton",
               command=select_mordret_file).pack(side="left")
    configure_voices_btn = ttk.Button(mordret_btn_row, text="Assign voices…", style="Card.TButton",
                                      command=configure_voices, state="disabled")
    configure_voices_btn.pack(side="left", padx=(8, 0))
    ttk.Button(mordret_btn_row, text="Clear", style="Card.TButton",
               command=clear_mordret_file).pack(side="left", padx=(8, 0))

    # -----------------------------------------------------------------
    # Pinned action bar
    # -----------------------------------------------------------------
    action_wrap = tk.Frame(root, bg=BORDER, height=1)
    action_wrap.pack(fill="x")
    action_bar = ttk.Frame(root, padding=(24, 14, 24, 18))
    action_bar.pack(fill="x")

    progress_bar = ttk.Progressbar(action_bar, mode="determinate", maximum=100,
                                   style="Horizontal.TProgressbar")

    status_row = ttk.Frame(action_bar)
    status_row.pack(fill="x")
    status_var = tk.StringVar(value="Choose a book to get started.")
    ttk.Label(status_row, textvariable=status_var, style="Status.TLabel",
              wraplength=380, justify="left").pack(side="left", fill="x", expand=True)
    percent_var = tk.StringVar(value="")
    ttk.Label(status_row, textvariable=percent_var, style="Percent.TLabel").pack(side="right")

    btn_row = ttk.Frame(action_bar)
    btn_row.pack(fill="x", pady=(10, 0))

    open_folder_btn = ttk.Button(btn_row, text="Open output folder",
                                 command=lambda: _open_folder(state["last_output_dir"]))
    stop_btn = ttk.Button(btn_row, text="Stop", style="Danger.TButton",
                          command=lambda: _request_stop())
    generate_btn = ttk.Button(btn_row, text="Generate Audiobook", style="Primary.TButton",
                              command=lambda: do_generate())
    generate_btn.pack(side="right")

    def set_status(msg):
        status_var.set(msg)

    def _open_folder(path):
        if not path or not os.path.isdir(path):
            return
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.run(["open", path])
            elif system == "Windows":
                os.startfile(path)  # noqa: S606
            else:
                subprocess.run(["xdg-open", path])
        except Exception:
            messagebox.showinfo("Output folder", path)

    def _request_stop():
        if state["cancel_event"] is not None:
            state["cancel_event"].set()
            set_status("Stopping after the current chunk…")
            stop_btn.config(state="disabled")

    def _refresh_ready_state():
        """Keep the primary button's enabled state and the idle status line
        honest about what's still missing."""
        if state["running"]:
            return
        generate_btn.config(state="normal")
        if not state["input_path"]:
            set_status("Choose a book to get started.")
        elif not voice_files:
            set_status("No voices installed — add voices to the voices folder, then Refresh.")
        elif voice_var.get() in PLACEHOLDERS:
            set_status("No voice selected — clear the search box and pick one.")
        else:
            speaker = voice_var.get().split("  ·  ")[0]
            if mordret_state["path"]:
                set_status(f"Ready — multi-voice, {len(mordret_state['slot_voice_map'])} slot(s) assigned.")
            else:
                set_status(f"Ready to generate with “{speaker}”.")

    def _tick_elapsed():
        if not state["running"] or not state["started_at"]:
            return
        secs = int(time.time() - state["started_at"])
        mins, secs = divmod(secs, 60)
        hrs, mins = divmod(mins, 60)
        elapsed = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d}"
        state["elapsed_text"] = elapsed
        root.after(1000, _tick_elapsed)

    def do_generate():
        if state["running"]:
            return
        if not state["input_path"]:
            messagebox.showerror("No book selected", "Choose a book file first.")
            return

        using_mordret = mordret_state["path"] is not None
        voice_path = None
        if not using_mordret:
            voice_path = get_voice_path()
            if not voice_path:
                return
        elif not mordret_state["slot_voice_map"]:
            if not messagebox.askyesno(
                "No voices assigned",
                "You loaded a Mordret file but haven't assigned any voices to slots yet. "
                "Every line will use the single selected voice instead.\n\nContinue anyway?"
            ):
                return
            voice_path = get_voice_path()
            if not voice_path:
                return

        speaker_id = get_speaker_id() if voice_path else None
        range_kwargs = get_range_kwargs()
        if range_kwargs is None:
            return

        # Read every Tk variable the worker needs *here*, on the main
        # thread. Tkinter isn't thread-safe, so touching a StringVar/
        # DoubleVar from the worker is undefined behaviour rather than a
        # merely slow path.
        selected_speed = speed_var.get()

        cancel_event = threading.Event()
        state.update({"running": True, "cancel_event": cancel_event,
                      "started_at": time.time()})
        generate_btn.config(state="disabled")
        preview_btn.config(state="disabled")
        open_folder_btn.pack_forget()
        stop_btn.pack(side="right", padx=(0, 8))
        progress_bar.pack(fill="x", pady=(0, 10), before=status_row)
        progress_bar["value"] = 0
        percent_var.set("0%")
        set_status("Starting…")
        _tick_elapsed()

        def on_progress(msg, info=None):
            def update():
                if info:
                    progress_bar["value"] = info["percent"]
                    elapsed = state.get("elapsed_text", "")
                    percent_var.set(f"{info['percent']:.0f}%")
                    where = f"Chapter {info['chapter']}/{info['total_chapters']}"
                    if info["total_chunks_here"]:
                        where += f" · chunk {info['chunk']}/{info['total_chunks_here']}"
                    if elapsed:
                        where += f" · {elapsed} elapsed"
                    set_status(f"{msg.strip()}   —   {where}")
                else:
                    set_status(msg.strip())
            root.after(0, update)

        def finish(reset_status=None, show_open=False):
            state.update({"running": False, "cancel_event": None, "started_at": None})
            generate_btn.config(state="normal")
            preview_btn.config(state="normal")
            stop_btn.pack_forget()
            stop_btn.config(state="normal")
            if show_open:
                open_folder_btn.pack(side="right", padx=(0, 8))
            if reset_status:
                set_status(reset_status)

        def work():
            try:
                job = AudiobookJob(
                    input_path=state["input_path"],
                    voice_model_path=voice_path,
                    speed=selected_speed,
                    progress_callback=on_progress,
                    mordret_path=mordret_state["path"],
                    slot_voice_map=dict(mordret_state["slot_voice_map"]),
                    speaker_id=speaker_id,
                    cancel_event=cancel_event,
                    **range_kwargs,
                )
                job.run()
                out_dir = job.book_output_dir
                state["last_output_dir"] = out_dir

                def done():
                    progress_bar["value"] = 100
                    percent_var.set("100%")
                    finish(reset_status=f"Done — saved to {out_dir}", show_open=True)
                    messagebox.showinfo("Audiobook ready", f"Audiobook created in:\n{out_dir}")
                root.after(0, done)
            except JobCancelled:
                out_dir = os.path.join(OUTPUT_DIR, textmod.safe_filename(
                    os.path.splitext(os.path.basename(state["input_path"]))[0]))
                state["last_output_dir"] = out_dir

                def stopped():
                    percent_var.set("")
                    finish(reset_status="Stopped. Finished chunks were kept — "
                                        "generating again resumes where you left off.",
                           show_open=os.path.isdir(out_dir))
                root.after(0, stopped)
            except Exception as e:
                traceback.print_exc()
                err = str(e)

                def failed():
                    percent_var.set("")
                    finish(reset_status="Generation failed.")
                    messagebox.showerror("Generation failed", err)
                root.after(0, failed)

        threading.Thread(target=work, daemon=True).start()

    _apply_voice_filter()
    _on_range_mode_change()
    _refresh_ready_state()
    root.after(50, _sync_scroll)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli_progress(msg, info=None):
    if info:
        print(f"[{info['percent']:5.1f}%] {msg}")
    else:
        print(msg)


def _parse_range_arg(value, flag_name):
    """Parse a 'START:END' range string (either side optional, e.g. '3:6',
    ':6', '3:') into a (start, end) tuple of ints/None."""
    if ":" not in value:
        raise argparse.ArgumentTypeError(f"{flag_name} must be START:END (e.g. 3:6), got {value!r}")
    start_s, end_s = value.split(":", 1)
    try:
        start = int(start_s) if start_s else None
        end = int(end_s) if end_s else None
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{flag_name} must be START:END with integers (e.g. 3:6), got {value!r}"
        )
    return start, end


def main():
    if len(sys.argv) == 1:
        launch_ui()
        return

    # CLI mode: python app.py <file> <voice.onnx> [speed] [--chapters A:B] [--pages A:B]
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Audiobook Maker CLI -- turn a PDF/EPUB/TXT into an audiobook.",
    )
    parser.add_argument("file", help="Path to the book file (.pdf, .epub, .txt)")
    parser.add_argument("voice", nargs="?", default=None,
                         help="Path to a Piper .onnx voice model (default: first voice found in ./voices)")
    parser.add_argument("speed", nargs="?", type=float, default=1.0,
                         help="Speech speed, 1.0 = normal (default: 1.0)")
    parser.add_argument("--chapters", metavar="START:END",
                         type=lambda v: _parse_range_arg(v, "--chapters"),
                         help="Only generate chapters START-END (1-indexed, inclusive)")
    parser.add_argument("--pages", metavar="START:END",
                         type=lambda v: _parse_range_arg(v, "--pages"),
                         help="Only generate from PDF pages START-END (1-indexed, inclusive; PDF only)")
    args = parser.parse_args()

    input_path = args.file
    voice_path = args.voice
    speed = args.speed

    start_chapter, end_chapter = args.chapters if args.chapters else (None, None)
    start_page, end_page = args.pages if args.pages else (None, None)

    if not os.path.isfile(input_path):
        print(f"File not found: {input_path}")
        print()
        print("Tip: if the filename has spaces or special characters, either:")
        print('  - wrap it in quotes: python app.py "my book.epub"')
        print("  - or drag the file from Finder into the terminal window "
              "after typing 'python app.py '")
        print()
        candidates = [f for f in os.listdir(".") if f.lower().endswith((".pdf", ".epub", ".txt"))]
        if candidates:
            print("Books found in the current folder:")
            for c in candidates:
                print(f"  {c}")
        sys.exit(1)

    if not voice_path:
        voices = ttsmod.list_available_voices(VOICES_DIR)
        if not voices:
            print(f"No voice models found in {VOICES_DIR}")
            sys.exit(1)
        voice_path = os.path.join(VOICES_DIR, voices[0])
    elif not os.path.isfile(voice_path):
        print(f"Voice model not found: {voice_path}")
        sys.exit(1)

    try:
        job = AudiobookJob(
            input_path=input_path,
            voice_model_path=voice_path,
            speed=speed,
            progress_callback=_cli_progress,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            start_page=start_page,
            end_page=end_page,
        )
        job.run()
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
