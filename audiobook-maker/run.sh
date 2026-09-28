#!/usr/bin/env bash
#
# run.sh — Setup, self-heal, auto-update, and launch Audiobook Maker on macOS.
#
# What it does:
#   0. Checks for a newer version via git and updates automatically,
#      preserving your voices/, output/, and any Mordret files untouched.
#   1. Checks for Python 3 and tkinter support (installs python-tk via
#      Homebrew if missing — this is the #1 cause of GUI crashes on macOS).
#   2. Checks for ffmpeg (installs via Homebrew if missing).
#   3. Detects a broken/stale virtual environment (e.g. left over from a
#      different Python version) and rebuilds it automatically.
#   4. Installs/updates the required Python packages.
#   5. Cleans up stray __pycache__ and leftover .tmp chunk files from
#      interrupted runs so they don't cause confusing "already done" states.
#   6. Warns if no Piper voice model is present in ./voices.
#   7. Launches the app (GUI by default, or CLI mode if you pass arguments).
#
# Usage:
#   ./run.sh                              # launch the GUI
#   ./run.sh book.epub voices/x.onnx 1.0  # run from the command line
#   ./run.sh --no-update                  # skip the update check this run
#
set -euo pipefail

cd "$(dirname "$0")"

GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

info()  { echo -e "${GREEN}==>${RESET} $1"; }
warn()  { echo -e "${YELLOW}==>${RESET} $1"; }
error() { echo -e "${RED}==>${RESET} $1"; }

SKIP_UPDATE=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--no-update" ]; then
        SKIP_UPDATE=true
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

# ---------------------------------------------------------------------------
# 0. Auto-update via git, if this copy is a git checkout.
#
#    A copy that was unzipped/downloaded manually (not `git clone`d) has no
#    .git folder — in that case this step is skipped entirely and a message
#    explains how to switch to the auto-updating channel, without blocking
#    the current run.
# ---------------------------------------------------------------------------
if [ "$SKIP_UPDATE" = true ]; then
    info "Skipping update check (--no-update)."
elif [ ! -d ".git" ]; then
    warn "This copy isn't connected to the update channel yet (no .git folder found)."
    warn "See the 'Staying up to date' section in README.md to switch to it —"
    warn "after that, this script updates itself automatically on every run."
elif ! command -v git >/dev/null 2>&1; then
    warn "git not found, so the update check was skipped this run."
else
    info "Checking for updates..."
    CURRENT_VERSION="$(cat VERSION 2>/dev/null || echo unknown)"

    if git fetch --quiet origin 2>/dev/null; then
        LOCAL_HASH="$(git rev-parse HEAD 2>/dev/null || echo local)"
        REMOTE_HASH="$(git rev-parse origin/main 2>/dev/null || echo remote)"

        if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
            # Detect local edits to tracked files (not voices/output, which
            # are gitignored and therefore untouched by any of this) before
            # updating, so a user's own tweaks are never silently discarded.
            if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
                warn "A new version is available, but you have local changes to"
                warn "tracked files, so the update was skipped to avoid losing them."
                warn "Run 'git stash' to set them aside, then run this script again."
            else
                info "Updating to the latest version..."
                if git merge --ff-only origin/main --quiet 2>/dev/null; then
                    NEW_VERSION="$(cat VERSION 2>/dev/null || echo unknown)"
                    info "Updated: ${CURRENT_VERSION} -> ${NEW_VERSION}"
                    # A code update can change dependencies or invalidate an
                    # old venv's cached packages, so force a clean reinstall
                    # below by removing the venv here rather than trusting
                    # it's still compatible.
                    rm -rf .venv
                else
                    warn "Could not fast-forward to the latest version automatically"
                    warn "(your local history has diverged). Continuing with the"
                    warn "current version — run 'git pull' manually to investigate."
                fi
            fi
        else
            info "Already on the latest version (${CURRENT_VERSION})."
        fi
    else
        warn "Could not reach the update server this time — continuing with the current version."
    fi
fi

# ---------------------------------------------------------------------------
# 1. Check Python 3
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    error "python3 not found. Install it from https://www.python.org/downloads/ or via 'brew install python'."
    exit 1
fi
info "Found Python: $(python3 --version)"

SYSTEM_PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

# ---------------------------------------------------------------------------
# 2. Check tkinter support on the system Python BEFORE creating a venv.
#    (venvs inherit tkinter support from the Python they're created from,
#    so this must be fixed first or every venv rebuild will still be broken.)
# ---------------------------------------------------------------------------
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    warn "tkinter (GUI support) is missing from this Python install."
    if command -v brew >/dev/null 2>&1; then
        info "Installing python-tk via Homebrew..."
        if brew install "python-tk@${SYSTEM_PY_VERSION}" 2>/dev/null; then
            :
        else
            warn "python-tk@${SYSTEM_PY_VERSION} not found, trying generic python-tk..."
            brew install python-tk || {
                error "Could not install python-tk automatically."
                error "Try manually: brew install python-tk"
                error "You can still use CLI mode: ./run.sh book.epub voices/voice.onnx"
            }
        fi
        # A stale venv built before python-tk existed will NOT pick it up
        # retroactively, so force a clean rebuild below.
        if [ -d ".venv" ]; then
            warn "Rebuilding .venv so it picks up the new tkinter support..."
            rm -rf .venv
        fi
    else
        error "Homebrew not found, so python-tk can't be installed automatically."
        error "Install Homebrew first: https://brew.sh"
        error "Then run: brew install python-tk"
        error "You can still use CLI mode meanwhile: ./run.sh book.epub voices/voice.onnx"
    fi
fi

# ---------------------------------------------------------------------------
# 3. Check / install ffmpeg
# ---------------------------------------------------------------------------
if ! command -v ffmpeg >/dev/null 2>&1; then
    warn "ffmpeg not found (needed to join audio into MP3/M4B)."
    if command -v brew >/dev/null 2>&1; then
        info "Installing ffmpeg via Homebrew..."
        brew install ffmpeg
    else
        error "Homebrew not found. Install ffmpeg manually: https://ffmpeg.org/download.html"
        error "Or install Homebrew first: https://brew.sh"
        exit 1
    fi
else
    info "Found ffmpeg: $(ffmpeg -version | head -n1)"
fi

# ---------------------------------------------------------------------------
# 4. Detect a stale/broken virtual environment and rebuild if needed.
#    A venv is considered stale if:
#      - its recorded Python version doesn't match the current system Python
#      - its python binary is broken (symlink to a deleted Python)
#      - it exists but has no pip / activate script (partial install)
# ---------------------------------------------------------------------------
VENV_STALE=false

if [ -d ".venv" ]; then
    if [ ! -f ".venv/bin/activate" ] || [ ! -f ".venv/bin/python3" ]; then
        VENV_STALE=true
        warn "Existing .venv looks incomplete."
    elif ! .venv/bin/python3 -c "exit(0)" >/dev/null 2>&1; then
        VENV_STALE=true
        warn "Existing .venv's Python binary is broken (likely an old Homebrew upgrade)."
    else
        VENV_PY_VERSION="$(.venv/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "unknown")"
        if [ "$VENV_PY_VERSION" != "$SYSTEM_PY_VERSION" ]; then
            VENV_STALE=true
            warn "Existing .venv was built with Python $VENV_PY_VERSION, but system Python is now $SYSTEM_PY_VERSION."
        fi
    fi
fi

if [ "$VENV_STALE" = true ]; then
    info "Removing outdated virtual environment..."
    rm -rf .venv
fi

if [ ! -d ".venv" ]; then
    info "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# ---------------------------------------------------------------------------
# 5. Install/update Python dependencies
# ---------------------------------------------------------------------------
info "Installing Python dependencies (this may take a minute on first run)..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ---------------------------------------------------------------------------
# 6. Clean up stray files from interrupted runs / old code versions
# ---------------------------------------------------------------------------
find . -name "__pycache__" -type d -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
find . -name "*.tmp" -not -path "./.venv/*" -delete 2>/dev/null || true
# Remove zero-byte / truncated leftover WAV chunks from a crashed run so
# they don't confuse anyone poking around the output folder by hand.
# (The app itself already detects and regenerates these automatically —
# this is just housekeeping so `output/` stays tidy between runs.)
if [ -d "output" ]; then
    find output -name "*.wav" -size 0 -delete 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 7. Voices: auto-download a broad default voice library on first run, if
#    the voices folder is genuinely empty. This is what makes both
#    single-voice and multi-voice (Mordret) generation work out of the box
#    with zero manual setup, with real choice of voice/locale/quality.
#
#    The list below is a static snapshot of every en_US / en_GB voice of
#    "medium" or "high" quality from the Piper voices repo (a fetched
#    manifest would need JSON parsing tooling we can't assume is installed,
#    and a stale/unreachable manifest shouldn't be able to break first run --
#    a checked-in list is more reliable/offline-resilient for that reason).
#    To regenerate: download
#    https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json
#    and filter to language.code in (en_US, en_GB) and quality in
#    (medium, high); each entry below is the file path with no extension.
# ---------------------------------------------------------------------------
mkdir -p voices

VOICE_BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICE_SPECS=(
    "en/en_GB/alan/medium/en_GB-alan-medium"
    "en/en_GB/alba/medium/en_GB-alba-medium"
    "en/en_GB/aru/medium/en_GB-aru-medium"
    "en/en_GB/cori/high/en_GB-cori-high"
    "en/en_GB/cori/medium/en_GB-cori-medium"
    "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium"
    "en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium"
    "en/en_GB/semaine/medium/en_GB-semaine-medium"
    "en/en_GB/vctk/medium/en_GB-vctk-medium"
    "en/en_US/amy/medium/en_US-amy-medium"
    "en/en_US/arctic/medium/en_US-arctic-medium"
    "en/en_US/bryce/medium/en_US-bryce-medium"
    "en/en_US/hfc_female/medium/en_US-hfc_female-medium"
    "en/en_US/hfc_male/medium/en_US-hfc_male-medium"
    "en/en_US/joe/medium/en_US-joe-medium"
    "en/en_US/john/medium/en_US-john-medium"
    "en/en_US/kristin/medium/en_US-kristin-medium"
    "en/en_US/kusal/medium/en_US-kusal-medium"
    "en/en_US/l2arctic/medium/en_US-l2arctic-medium"
    "en/en_US/lessac/high/en_US-lessac-high"
    "en/en_US/lessac/medium/en_US-lessac-medium"
    "en/en_US/libritts/high/en_US-libritts-high"
    "en/en_US/libritts_r/medium/en_US-libritts_r-medium"
    "en/en_US/ljspeech/high/en_US-ljspeech-high"
    "en/en_US/ljspeech/medium/en_US-ljspeech-medium"
    "en/en_US/mike/medium/en_US-mike-medium"
    "en/en_US/norman/medium/en_US-norman-medium"
    "en/en_US/reza_ibrahim/medium/en_US-reza_ibrahim-medium"
    "en/en_US/ryan/high/en_US-ryan-high"
    "en/en_US/ryan/medium/en_US-ryan-medium"
    "en/en_US/sam/medium/en_US-sam-medium"
)
# ~2.4 GB total for the ${#VOICE_SPECS[@]} voices above -- update this
# estimate if VOICE_SPECS changes.
VOICE_LIBRARY_SIZE_ESTIMATE="~2.4 GB"

# Only auto-seed the full default library the first time this script finds
# a genuinely empty voices/ folder -- never force it onto a folder the user
# already populated (with defaults or their own voices). A "seed started"
# marker lets an interrupted first download resume (retrying only what's
# missing) without re-checking "was it empty" against files it just added.
SEED_COMPLETE_MARKER="voices/.default_library_complete"
SEED_STARTED_MARKER="voices/.default_library_seeding"

usable_count=0
incomplete_list=""
for f in voices/*.onnx; do
    [ -e "$f" ] || continue
    if [ -f "${f}.json" ]; then
        usable_count=$((usable_count + 1))
    else
        incomplete_list="${incomplete_list}  $(basename "$f")\n"
    fi
done

if [ ! -f "$SEED_COMPLETE_MARKER" ]; then
    should_seed=false
    if [ -f "$SEED_STARTED_MARKER" ]; then
        should_seed=true
    elif [ "$usable_count" -eq 0 ] && [ -z "$incomplete_list" ]; then
        should_seed=true
        touch "$SEED_STARTED_MARKER"
    fi

    if [ "$should_seed" = true ]; then
        # Retry only what's missing -- lets an interrupted download resume
        # without re-fetching voices that already succeeded.
        missing_default_voices=0
        for spec in "${VOICE_SPECS[@]}"; do
            filename="$(basename "$spec")"
            if [ ! -f "voices/${filename}.onnx" ] || [ ! -f "voices/${filename}.onnx.json" ]; then
                missing_default_voices=$((missing_default_voices + 1))
            fi
        done

        if [ "$missing_default_voices" -eq "${#VOICE_SPECS[@]}" ]; then
            info "No voices found -- downloading the default voice library"
            info "(${#VOICE_SPECS[@]} voices, ${VOICE_LIBRARY_SIZE_ESTIMATE}, one-time)..."
            info "This covers a broad set of en_US/en_GB medium/high quality voices,"
            info "enough to assign a distinct voice to every Mordret slot."
        else
            info "Resuming download of ${missing_default_voices} missing default voice(s)..."
        fi
        download_failed=false
        for spec in "${VOICE_SPECS[@]}"; do
            filename="$(basename "$spec")"
            onnx_path="voices/${filename}.onnx"
            json_path="voices/${filename}.onnx.json"
            if [ -f "$onnx_path" ] && [ -f "$json_path" ]; then
                continue
            fi
            info "  Downloading ${filename}..."
            if curl -fSL --progress-bar -o "${onnx_path}.part" "${VOICE_BASE_URL}/${spec}.onnx" \
               && curl -fSL -o "${json_path}.part" "${VOICE_BASE_URL}/${spec}.onnx.json"; then
                mv "${onnx_path}.part" "$onnx_path"
                mv "${json_path}.part" "$json_path"
                usable_count=$((usable_count + 1))
            else
                warn "  Failed to download ${filename} -- skipping (check your internet connection)."
                rm -f "${onnx_path}.part" "${json_path}.part"
                download_failed=true
            fi
        done
        if [ "$download_failed" = true ]; then
            warn "Some voices failed to download. Run this script again to retry just the missing ones."
        else
            info "Default voice library downloaded successfully."
            touch "$SEED_COMPLETE_MARKER"
        fi
    elif [ "$usable_count" -eq 0 ]; then
        warn "No usable Piper voice found in ./voices"
        warn "Download a voice (.onnx + .onnx.json) from:"
        warn "  https://huggingface.co/rhasspy/piper-voices"
        warn "and place BOTH files in the 'voices' folder before generating audio."
    else
        info "Found ${usable_count} usable voice(s) in ./voices"
    fi
else
    info "Found ${usable_count} usable voice(s) in ./voices"
fi

if [ -n "$incomplete_list" ]; then
    warn "These .onnx files are missing their matching .onnx.json config, so they're ignored:"
    echo -e "$incomplete_list"
    warn "Re-download the .onnx.json for each of the files above."
fi

mkdir -p output

# ---------------------------------------------------------------------------
# 8. Verify tkinter one more time before attempting GUI mode, so the error
#    is a clear one-liner instead of a Python traceback.
# ---------------------------------------------------------------------------
if [ "$#" -eq 0 ]; then
    if ! python3 -c "import tkinter" >/dev/null 2>&1; then
        error "GUI mode isn't available (tkinter still missing after setup)."
        error "Run 'brew install python-tk' manually, then try again."
        error "Or use CLI mode instead: ./run.sh yourbook.epub voices/yourvoice.onnx"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 9. Launch
# ---------------------------------------------------------------------------
info "Starting Audiobook Maker..."
echo
python app.py "$@"
