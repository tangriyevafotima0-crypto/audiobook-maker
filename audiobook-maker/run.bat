@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "SKIP_UPDATE=0"
set "ARGS="
for %%A in (%*) do (
    if "%%~A"=="--no-update" (
        set "SKIP_UPDATE=1"
    ) else (
        set "ARGS=!ARGS! %%A"
    )
)

echo ============================================
echo   Audiobook Maker - setup and launch
echo ============================================
echo.

REM ---------------------------------------------------------------------
REM 0. Auto-update via git, if this copy is a git checkout.
REM ---------------------------------------------------------------------
if "%SKIP_UPDATE%"=="1" (
    echo [skip] Update check skipped --no-update
    goto :after_update
)
if not exist ".git" (
    echo [info] Not connected to the update channel yet ^(no .git folder^).
    echo [info] See "Staying up to date" in README.md to switch to it.
    goto :after_update
)
where git >nul 2>nul
if errorlevel 1 (
    echo [info] git not found, skipping update check.
    goto :after_update
)

echo [info] Checking for updates...
set "CURRENT_VERSION=unknown"
if exist "VERSION" set /p CURRENT_VERSION=<VERSION

git fetch --quiet origin 2>nul
if errorlevel 1 (
    echo [warn] Could not reach the update server -- continuing with the current version.
    goto :after_update
)

for /f "delims=" %%H in ('git rev-parse HEAD') do set "LOCAL_HASH=%%H"
for /f "delims=" %%H in ('git rev-parse origin/main 2^>nul') do set "REMOTE_HASH=%%H"

if "!LOCAL_HASH!"=="!REMOTE_HASH!" (
    echo [info] Already on the latest version ^(!CURRENT_VERSION!^).
    goto :after_update
)

git diff --quiet HEAD -- . ":(exclude)voices" ":(exclude)output"
if errorlevel 1 (
    echo [warn] A new version is available, but you have local changes to tracked files.
    echo [warn] Update skipped to avoid losing them. Run "git stash" first if you want to update.
    goto :after_update
)

echo [info] Updating to the latest version...
git merge --ff-only origin/main --quiet
if errorlevel 1 (
    echo [warn] Could not fast-forward automatically. Continuing with the current version.
    goto :after_update
)

set "NEW_VERSION=unknown"
if exist "VERSION" set /p NEW_VERSION=<VERSION
echo [info] Updated: !CURRENT_VERSION! -^> !NEW_VERSION!
if exist ".venv" (
    echo [info] Removing old virtual environment so it rebuilds cleanly...
    rmdir /s /q ".venv"
)

:after_update
echo.

REM ---------------------------------------------------------------------
REM 1. Check Python 3
REM ---------------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [error] Python not found.
    echo [error] Install it from https://www.python.org/downloads/
    echo [error] IMPORTANT: on the installer's first screen, check
    echo [error] "Add python.exe to PATH" before clicking Install,
    echo [error] then close this window and run run.bat again.
    pause
    exit /b 1
)
for /f "tokens=*" %%V in ('python --version 2^>^&1') do echo [info] Found Python: %%V

REM ---------------------------------------------------------------------
REM 2. Check ffmpeg
REM ---------------------------------------------------------------------
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [warn] ffmpeg not found ^(needed to join audio into MP3/M4B^).
    where winget >nul 2>nul
    if errorlevel 1 (
        echo [error] winget not available. Install ffmpeg manually:
        echo [error]   https://ffmpeg.org/download.html
        echo [error] ^(get the "essentials" build, extract it, and add its
        echo [error]  bin folder to your PATH environment variable^)
        pause
        exit /b 1
    )
    echo [info] Installing ffmpeg via winget ^(this can take a minute^)...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    echo [info] ffmpeg installed. Close this window, open a NEW terminal
    echo [info] ^(so the PATH update takes effect^), and run run.bat again.
    pause
    exit /b 0
)
echo [info] Found ffmpeg.

REM ---------------------------------------------------------------------
REM 3. Detect a stale/broken virtual environment and rebuild if needed.
REM ---------------------------------------------------------------------
set "VENV_STALE=0"
if exist ".venv" (
    if not exist ".venv\Scripts\activate.bat" set "VENV_STALE=1"
    if not exist ".venv\Scripts\python.exe" set "VENV_STALE=1"
    if "!VENV_STALE!"=="0" (
        ".venv\Scripts\python.exe" -c "exit(0)" >nul 2>nul
        if errorlevel 1 set "VENV_STALE=1"
    )
    if "!VENV_STALE!"=="1" (
        echo [info] Existing .venv looks broken or incomplete -- rebuilding.
        rmdir /s /q ".venv"
    )
)
if not exist ".venv" (
    echo [info] Creating virtual environment ^(.venv^)...
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"

REM ---------------------------------------------------------------------
REM 4. Install/update Python dependencies
REM ---------------------------------------------------------------------
echo [info] Installing Python dependencies ^(this may take a minute on first run^)...
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

REM ---------------------------------------------------------------------
REM 5. Clean up stray files from interrupted runs
REM ---------------------------------------------------------------------
for /f "delims=" %%D in ('dir /s /b /ad __pycache__ 2^>nul') do rmdir /s /q "%%D" 2>nul
del /s /q *.tmp >nul 2>nul
if exist "output" (
    for /r "output" %%F in (*.wav) do (
        if %%~zF==0 del "%%F" 2>nul
    )
)

REM ---------------------------------------------------------------------
REM 6. Voices: auto-download the default 7-voice set on first run.
REM    Each voice is a plain, explicit block -- no nested variable
REM    indirection, so this can't silently break on odd expansion rules.
REM    NARRATOR=lessac  M1=ryan  M2=danny  M3=norman  F1=amy  F2=kathleen  F3=kristin
REM ---------------------------------------------------------------------
if not exist "voices" mkdir voices
set "VOICE_BASE_URL=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US"

set "MISSING_COUNT=0"
if not exist "voices\en_US-lessac-medium.onnx" set /a MISSING_COUNT+=1
if not exist "voices\en_US-lessac-medium.onnx.json" set /a MISSING_COUNT+=1
if not exist "voices\en_US-ryan-medium.onnx" set /a MISSING_COUNT+=1
if not exist "voices\en_US-ryan-medium.onnx.json" set /a MISSING_COUNT+=1
if not exist "voices\en_US-danny-low.onnx" set /a MISSING_COUNT+=1
if not exist "voices\en_US-danny-low.onnx.json" set /a MISSING_COUNT+=1
if not exist "voices\en_US-norman-medium.onnx" set /a MISSING_COUNT+=1
if not exist "voices\en_US-norman-medium.onnx.json" set /a MISSING_COUNT+=1
if not exist "voices\en_US-amy-medium.onnx" set /a MISSING_COUNT+=1
if not exist "voices\en_US-amy-medium.onnx.json" set /a MISSING_COUNT+=1
if not exist "voices\en_US-kathleen-low.onnx" set /a MISSING_COUNT+=1
if not exist "voices\en_US-kathleen-low.onnx.json" set /a MISSING_COUNT+=1
if not exist "voices\en_US-kristin-medium.onnx" set /a MISSING_COUNT+=1
if not exist "voices\en_US-kristin-medium.onnx.json" set /a MISSING_COUNT+=1

set "INCOMPLETE_FOUND=0"
for %%F in (voices\*.onnx) do (
    if not exist "%%F.json" set "INCOMPLETE_FOUND=1"
)

if "!MISSING_COUNT!"=="0" (
    echo [info] All default voices already present.
    goto :after_voices
)
if "!INCOMPLETE_FOUND!"=="1" (
    echo [warn] Some .onnx files are missing their .onnx.json config.
    echo [warn] Fix or remove the incomplete files, then run this script again.
    goto :after_voices
)

echo [info] Downloading missing default voices ^(up to ~440 MB total, one-time^)...
echo [info] This gives every Mordret slot ^(NARRATOR + 3 male + 3 female^) a real voice.

call :download_voice "en_US-lessac-medium" "lessac/medium/en_US-lessac-medium"
call :download_voice "en_US-ryan-medium" "ryan/medium/en_US-ryan-medium"
call :download_voice "en_US-danny-low" "danny/low/en_US-danny-low"
call :download_voice "en_US-norman-medium" "norman/medium/en_US-norman-medium"
call :download_voice "en_US-amy-medium" "amy/medium/en_US-amy-medium"
call :download_voice "en_US-kathleen-low" "kathleen/low/en_US-kathleen-low"
call :download_voice "en_US-kristin-medium" "kristin/medium/en_US-kristin-medium"

echo [info] Voice download pass complete. If any failed, run this script again to retry just those.
goto :after_voices

:download_voice
set "fn=%~1"
set "fp=%~2"
if exist "voices\%fn%.onnx" if exist "voices\%fn%.onnx.json" exit /b 0
echo [info]   Downloading %fn%...
curl -fSL -o "voices\%fn%.onnx.part" "%VOICE_BASE_URL%/%fp%.onnx"
if errorlevel 1 (
    echo [warn]   Failed to download %fn%.onnx.
    del "voices\%fn%.onnx.part" 2>nul
    exit /b 0
)
curl -fSL -o "voices\%fn%.onnx.json.part" "%VOICE_BASE_URL%/%fp%.onnx.json"
if errorlevel 1 (
    echo [warn]   Failed to download %fn%.onnx.json.
    del "voices\%fn%.onnx.part" 2>nul
    del "voices\%fn%.onnx.json.part" 2>nul
    exit /b 0
)
move /y "voices\%fn%.onnx.part" "voices\%fn%.onnx" >nul
move /y "voices\%fn%.onnx.json.part" "voices\%fn%.onnx.json" >nul
exit /b 0

:after_voices
echo.

REM ---------------------------------------------------------------------
REM 7. Launch
REM ---------------------------------------------------------------------
echo [info] Starting Audiobook Maker...
echo.
python app.py %ARGS%
pause
