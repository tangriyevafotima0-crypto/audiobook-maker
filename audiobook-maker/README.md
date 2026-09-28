# Audiobook Maker

A small, reliable tool that turns a PDF, EPUB, or TXT file into an audiobook
using **Piper TTS** — fully local, free, no API keys, no cloud, no network
calls after setup.

```
PDF / EPUB / TXT
   → extract text
   → clean text
   → detect chapters
   → chunk text (~1000–2000 chars)
   → Piper TTS (per chunk, resumable)
   → join chunks into chapter MP3s
   → combine into Complete Audiobook.m4b
```

## Packaged app (no install)

If someone already built a packaged app for you (`AudiobookMaker.app` on
macOS, or an `AudiobookMaker` folder on Windows), you don't need Python,
pip, or ffmpeg installed at all -- just copy it to your Desktop (or
anywhere) and double-click it. `voices/` and `output/` folders appear next
to the app the first time you run it, and stay there across launches.

- **macOS:** double-click `AudiobookMaker.app`. The first time, macOS will
  likely block it ("cannot be opened because it is from an unidentified
  developer") since it isn't code-signed -- **right-click → Open → Open**
  once (same one-time step as `Start Audiobook Maker.command` below), and
  plain double-clicking works after that.
- **Windows:** double-click `AudiobookMaker.exe` inside the app's folder.
  SmartScreen may warn about an unrecognized app the first time -- click
  **More info → Run anyway**.

This is a separate, additive distribution path -- it doesn't replace or
change anything about the `run.sh`/`run.bat`/manual-setup instructions
below, which still work exactly as before.

**Building the packaged app yourself** (only needed if nobody built one
for you already): run `./build_mac.sh` (macOS) or `build_win.bat`
(Windows) from this folder. Each creates its own throwaway build
environment, installs [PyInstaller](https://pyinstaller.org/) and a
bundled static ffmpeg, and produces the app in `dist/`. This is a one-time
developer step -- end users never run these scripts themselves, they just
receive the already-built app.

## Staying up to date

If you installed this as a `git clone` (see below), `run.sh` checks for a
newer version every time you launch it and updates itself automatically —
you never need to manually download a new zip again. Your `voices/` and
`output/` folders, and any `.mordret.json` files, are never touched by an
update.

**If you haven't switched to this yet** (e.g. you unzipped a downloaded
copy instead of cloning it), do this once:

1. Move anything you want to keep out of the old folder first — your
   `voices/*.onnx`/`.onnx.json` files, and anything in `output/` you
   haven't saved elsewhere.
2. In Terminal:
   ```bash
   cd ~/Desktop
   git clone <REPO_URL> audiobook-maker-new
   ```
   (ask whoever gave you this project for `<REPO_URL>` if you don't have
   it — it's the GitHub link for this project)
3. Copy your saved voice files into `audiobook-maker-new/voices/`.
4. From now on, use `audiobook-maker-new` instead of your old folder —
   you can delete the old one. Every future `./run.sh` will pull the
   latest version automatically before launching.

To skip the update check for a single run (e.g. you're offline):
`./run.sh --no-update`

## Setup (macOS / Apple Silicon)

### Easiest: no terminal at all

Double-click **`Start Audiobook Maker.command`** in Finder.

The very first time, macOS will likely block it with a security warning
("cannot be opened because it is from an unidentified developer"). To allow
it (only needed once):

1. **Right-click** (or Control-click) `Start Audiobook Maker.command`
2. Choose **Open**
3. Click **Open** again in the dialog that appears

After that, plain double-clicking works every time. A Terminal window will
briefly appear (this is normal — it's needed to run Python) and the app
window will open on top of it.

### Almost as easy: one command

```bash
./run.sh
```

This script is self-healing — run it any time, including after problems:

- Installs ffmpeg (via Homebrew) if missing
- **Detects and installs `python-tk`** if your Python was built without GUI
  support (the most common cause of a `ModuleNotFoundError: No module
  named '_tkinter'` crash on Homebrew Python)
- **Detects a broken or outdated `.venv`** (e.g. left over from before a
  Python upgrade) and rebuilds it automatically instead of failing
- Installs/updates the Python dependencies
- Cleans up stray cache files and leftover zero-byte chunks from any
  previously interrupted run
- Warns you clearly if no voice model is present yet
- Launches the app

Run it with arguments to use CLI mode instead of the GUI:

```bash
./run.sh book.epub voices/en_US-lessac-medium.onnx 1.0
```

If a file isn't found, the CLI now lists the book files it *can* see in the
current folder, and reminds you to quote filenames with spaces.

### Manual setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install FFmpeg (used to join audio and export MP3/M4B):

```bash
brew install ffmpeg
```

### Download a voice

Piper needs a voice model (`.onnx`) and its matching config
(`.onnx.json`) — **both files, same name, same folder**, or the voice is
silently ignored. Download from the official Piper voices collection:

https://huggingface.co/rhasspy/piper-voices

Pick a voice for your language (e.g. `en_US-lessac-medium`), download both:

- `en_US-lessac-medium.onnx`
- `en_US-lessac-medium.onnx.json`

and place them in a `voices/` folder next to `app.py`:

```
audiobook-maker/
├── voices/
│   ├── en_US-lessac-medium.onnx
│   └── en_US-lessac-medium.onnx.json
```

If a voice you added doesn't show up in the GUI's Voice dropdown, click
**Refresh** next to it — it will also tell you if a `.onnx` file is present
but missing its `.onnx.json` (the most common reason a voice doesn't appear).

#### Male / Female grouping

The Voice dropdown labels each voice `[Female]`, `[Male]`, or `[Unknown]`
and groups them in that order, based on the known speaker each Piper voice
is trained on (e.g. `amy` and `kathleen` are female speakers, `ryan` and
`danny` are male). Piper doesn't store gender info anywhere itself, so
voices with unrecognized or ambiguous names are labeled `[Unknown]` rather
than guessed — an honest "don't know" beats a confident wrong label.
Preview a voice before committing to a full audiobook if you want to
confirm it sounds the way you expect.

Some known voices by group:
- **Female:** `amy`, `kathleen`, `kristin`, `hfc_female`, `ljspeech`, `libritts`
- **Male:** `ryan`, `danny`, `joe`, `john`, `norman`, `bryce`, `hfc_male`

#### For more natural-sounding audio

Voice quality depends heavily on which model you pick, not just settings:

- **Use a `high` quality voice instead of `medium` or `low`** — e.g.
  `en_US-lessac-high` instead of `en_US-lessac-medium`. Same download
  process, just swap `medium` for `high` in the URL. Larger file, clearly
  more natural.
- **Try a few different voices, not just one.** Each Piper voice is trained
  on a different speaker/dataset, so naturalness varies a lot between them
  even at the same quality tier. `en_US-ljspeech-high` (trained on
  audiobook narration) is often a good fit for long-form listening.
- **Speed around 0.9–1.0x** tends to sound more natural than faster
  settings, since speeding up compresses the natural pacing between words.
- Chapter audio now has a short pause automatically inserted between text
  chunks, so chunk boundaries don't sound abruptly cut.

## Multi-voice audiobooks (Mordret)

By default, the whole book is read in one voice. If you want different
characters to speak in different voices (e.g. male/female voices for
dialogue), you can optionally annotate the book first using the separate
**Mordret Annotator** Claude Skill, which produces a `book.mordret.json`
file — then load that file in the GUI's **Multi-voice (optional)** box:

1. Click **Select Mordret file...** and pick the `.mordret.json` file.
2. Click **Assign voices...** — you'll see up to 7 voice slots (NARRATOR,
   plus up to 3 male and 3 female character slots), each showing which
   character(s) use it. Pick a real installed voice for each slot you
   want distinct, and save.
3. Generate normally — each line is spoken in its assigned character's
   voice, narration in the NARRATOR voice.

The Mordret file itself never contains any of the book's actual text —
only short phrases used to relocate lines in your own book file at
generation time, plus character names and voice assignments. This keeps
annotation copyright-safe: the audio is generated from your own legally
obtained book file, same as always.

If a slot has no voice assigned, lines for that slot fall back to your
regular single-voice selection instead of blocking generation. Resume
works the same way as single-voice mode — interrupted multi-voice
generation picks back up exactly where it left off.

## Usage

### GUI

```bash
./run.sh
```

(or `python app.py` if you already activated `.venv` manually)

1. Click **Select PDF / EPUB / TXT** and choose your book.
2. Pick a **Voice** and **Speed**.
3. Click **Preview** to hear a short sample first.
4. *(Optional)* Set up multi-voice narration — see "Multi-voice
   audiobooks" above.
5. Click **Generate Audiobook**.

Output appears in `output/<Book Title>/`:

```
output/My Book/
├── 01 - Introduction.mp3
├── 02 - Chapter 1.mp3
├── 03 - Chapter 2.mp3
└── Complete Audiobook.m4b
```

### Command line

```bash
python app.py path/to/book.epub voices/en_US-lessac-medium.onnx 1.0
```

(`speed` is optional, defaults to `1.0`. CLI mode currently supports
single-voice generation only — use the GUI for multi-voice/Mordret books.)

## Resuming an interrupted run

If generation stops partway through (crash, closed terminal, etc.), just
run it again with the same file. Already-generated chunk `.wav` files on
disk are detected and skipped automatically — no database, no special
resume command needed. Only missing or incomplete chunks are regenerated.
`run.sh` also cleans up any zero-byte leftover files from the crash on
your next run, so the output folder stays tidy. This applies to
multi-voice (Mordret) generation too.

## Progress while generating

The GUI shows a real progress bar with the current chapter, current chunk,
and overall percent complete — not just a spinner. In CLI mode, each line
is prefixed with the running percentage, e.g. `[ 42.3%] Generating chunk 3/5...`.

## Notes

- If a PDF has no selectable text (i.e. it's a scan), the tool will tell
  you directly rather than attempting OCR.
- Text cleaning only fixes obvious extraction artifacts (page numbers,
  repeated headers/footers, line-wrap hyphenation). It does not rewrite
  or paraphrase your book.
- If chapters can't be reliably detected, the whole book is treated as a
  single chapter.
# Test change for v3.1.0
