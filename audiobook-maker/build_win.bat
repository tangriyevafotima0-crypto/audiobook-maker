@echo off
REM build_win.bat -- one-time developer build of the packaged desktop app
REM (Windows). Produces dist\AudiobookMaker\AudiobookMaker.exe, which end
REM users can copy anywhere and double-click -- no Python, pip, or ffmpeg
REM setup required on their machine.
REM
REM This is a dev-only build script, not something end users run. It does
REM not touch run.sh/run.bat/the venv those use -- this is an additional,
REM separate distribution path.

setlocal
cd /d "%~dp0"

set BUILD_VENV=.build_venv

echo ==^> Creating a clean build virtual environment (%BUILD_VENV%)...
if exist "%BUILD_VENV%" rmdir /s /q "%BUILD_VENV%"
python -m venv "%BUILD_VENV%"
call "%BUILD_VENV%\Scripts\activate.bat"

echo ==^> Installing dependencies (app requirements + PyInstaller + bundled ffmpeg)...
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet pyinstaller imageio-ffmpeg

echo ==^> Running PyInstaller (onedir)...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
pyinstaller --noconfirm audiobook_maker_win.spec

call "%BUILD_VENV%\Scripts\deactivate.bat"

echo.
echo ==^> Build complete.
echo     App folder: %cd%\dist\AudiobookMaker\
echo     Copy the whole AudiobookMaker folder anywhere and double-click
echo     AudiobookMaker.exe inside it.
pause
