# Audiobook Maker (Mordret Engine)

A high-performance, local, privacy-focused media processing application designed to synthesize structured e-books (EPUB, PDF, TXT) into multi-chapter audiobooks using local Piper neural text-to-speech (TTS) and multi-voice character assignment.

## System Architecture

The core pipeline processes documents through a multi-stage engineering workflow:
1. **Document Ingestion & Text Processing (`text.py`):** Structured extraction via PyMuPDF/EbookLib, regex-based chapter splitting, punctuation normalization, and speech chunking.
2. **Multi-Voice Dialogue Alignment (`mordret.py`):** Character-level voice assignment using sequential anchor mapping and fuzzy token matching to prevent voice dropouts.
3. **Neural Speech Synthesis (`tts.py`):** Local ONNX-runtime synthesis powered by Piper TTS with dynamic memory caching.
4. **Audio Engineering Pipeline (`audio.py`):** FFmpeg concatenation, silence trimming, EBU R128 loudness normalization, and final packaging into M4B containers with chapter metadata.
5. **Job Orchestration (`app.py`):** Task management, checkpointing, and thread-safe cancellation.

## Key Technical Highlights
- **Hardware-Optimized Parallelism:** Multi-process worker pool utilizing available CPU cores without memory leaks.
- **Robust Alignment:** Resilient anchor resolution using Levenshtein distance fallbacks.
- **Checkpointing Architecture:** Chunk-level progress persistence ensuring zero loss on unexpected interruptions.

## Tech Stack
- **Language:** Python 3.10+
- **Inference Runtime:** ONNX Runtime (Piper TTS)
- **Audio Processing:** FFmpeg (loudnorm, silenceremove)
- **Document Parsers:** PyMuPDF, BeautifulSoup4, EbookLib

## License
MIT License
