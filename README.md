# VideoAnalyzer

**Drop a video. Get a transcript and an action prompt for Claude.**

A free, open-source local tool that turns any video into an AI-ready knowledge packet — transcript, key frames, and a one-click prompt you paste into [Claude](https://claude.ai) to immediately start building, learning, or creating from what you just watched.

---

## Why This Exists

You watch a TikTok about a new coding technique, an AI workflow, a design pattern. You save it. Then it sits in your camera roll and you never do anything with it.

**VideoAnalyzer closes that gap.** AirDrop your saved TikToks, screen recordings, or tutorial clips to your Mac, drop them into VideoAnalyzer, and in seconds you have:

- A full transcript of everything said in the video
- Key frames extracted at smart intervals
- A **"Copy for Claude"** button that packages the entire video into a structured prompt

Paste that prompt into Claude Code or claude.ai, and you're instantly working — building the thing shown in the video, deploying a new skill, recreating a workflow, or extracting the key ideas into notes.

### Real-World Workflow

1. Watch a TikTok showing how to build an AI agent
2. Save it to your phone, AirDrop to your Mac
3. Drop the video into VideoAnalyzer
4. Hit **"Copy for Claude"** and paste into Claude Code
5. Claude reads the transcript + every frame, understands the full context
6. Ask Claude to build it, explain it, or adapt it to your project
7. Ship it

This turns passive video consumption into active creation. Every tutorial, every demo, every "I should try that" moment becomes something you can act on in minutes.

## Features

- **100% Local** — Whisper runs on your machine. Nothing leaves your computer.
- **Thumbnails** — Visual previews in the history list so you can find videos at a glance
- **Status Tracking** — Mark videos as New, Doing It, Skipped, or Revisit Later
- **LLM-Generated Titles** — Smart headlines via local Ollama (e.g., "Build an AI Agent in 5 Minutes")
- **Whisper Model Selection** — Choose accuracy vs. speed: tiny, base, small, medium, or large
- **Copy for Claude** — One click produces a complete prompt with transcript + `read` commands for every frame
- **Zero Setup** — Single `uv run` command, no virtual environments, no build step

## Quick Start

### Prerequisites

Install via Homebrew:

```bash
brew install openai-whisper ffmpeg
```

Optional (for smart title generation):

```bash
brew install ollama
ollama pull gemma3:4b
```

### Run

```bash
git clone https://github.com/Vault-AI-Labs/VideoAnalyzer.git
cd VideoAnalyzer
./start.sh
```

Opens at `http://localhost:7745`. Drop a video and go.

> **Note:** The app auto-discovers `ffprobe`, `ffmpeg`, and `whisper` binaries at startup, including from `/opt/homebrew/bin` and `/usr/local/bin`. This means it works correctly under `uv run` even when the shell PATH isn't fully inherited.

### macOS Desktop App

Double-click `VideoAnalyzer.app` on your Desktop (if you've built the launcher).

## How It Works

1. **Upload** — Drop any video file (MP4, MOV, WebM, MKV, AVI)
2. **Analyze** — ffprobe extracts video duration
3. **Transcribe** — Whisper processes audio locally (model selectable)
4. **Extract Frames** — ffmpeg pulls key frames at smart intervals:
   - Under 60s: every 3 seconds
   - 60-180s: every 10 seconds
   - Over 180s: every 20 seconds
   - Capped at 24 frames
5. **Generate Title** — Local LLM creates a 5-7 word headline
6. **Build Prompt** — Server-side construction with verified absolute frame paths
7. **Ready** — Copy for Claude, review the transcript, or browse frames

Long-running transcriptions use heartbeat events every 30s to keep the connection alive — no timeouts on big videos.

## Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python Flask (single file `app.py`) |
| Frontend | Vanilla HTML/JS (`templates/index.html`), no build step |
| Transcription | OpenAI Whisper (local) |
| Frame extraction | ffmpeg |
| Title generation | Ollama gemma3:4b (optional, heuristic fallback) |
| Dependencies | `uv run --with flask` (zero venv) |

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web UI |
| POST | `/api/upload` | Upload video, returns `job_id` |
| GET | `/api/stream/<job_id>` | SSE processing progress |
| GET | `/api/frames/<job_id>/<file>` | Serve extracted frame |
| GET | `/api/history` | List past transcriptions |
| PUT | `/api/status/<job_id>` | Update item status |

## Project Structure

```
~/.video-transcriber/           # Runtime data
  ├── port                      # Current port
  ├── uploads/                  # Temporary (deleted after processing)
  └── outputs/<job_id>/         # Persisted results
      ├── metadata.json         # All job data + Claude prompt
      ├── transcript.txt        # Clean transcript
      └── frames/frame_*.jpg    # Extracted key frames
```

## Contributing

PRs welcome. Keep it simple — this is a single-file Flask app by design.

## License

MIT

---

Built by [VaultAI](https://vaultai.us)
