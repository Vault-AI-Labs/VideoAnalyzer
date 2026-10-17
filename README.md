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

## Saved TikToks → research lessons

VideoAnalyzer can also take **links** instead of files and turn each video into a rated lesson:

- **Paste TikTok links** (one per line) or **Import TikTok data file** (TikTok → Settings → Account → Download your data →
  JSON; every Favorite/saved video is queued, 50 per import, already-saved ones skipped). Needs `brew install yt-dlp`.
  For videos that need sign-in, set `VA_YTDLP_COOKIES_BROWSER=safari` (or `chrome`).
- **Three thumbnails** per video (15 %, 50 %, 85 %) so lessons are easy to recognise.
- **Lesson analysis by a local model** (OpenAI-compatible endpoint: Ollama, vllm-mlx, LM Studio — never a hosted API):
  lesson name, summary, key points, tools, category, and a recommendation (adopt / trial / watch / skip, value 0–10,
  where it fits your production flow, effort, steps). Thumbnails are sent as images when the model supports vision.
  Describe your production flow in a file and point `VA_PRODUCTION_CONTEXT` at it.
- **Send to BadMonkey**: exports the lesson to the BadMonkey workspace's Research module (automatic when configured).

Settings go in the environment or `~/.video-transcriber/config.json` (Finder launches have no shell environment):

| Setting | Default |
|---|---|
| `VA_LLM_URL` / `VA_LLM_MODEL` | `http://127.0.0.1:11434/v1` / `qwen3.8:27b` |
| `VA_LLM_VISION` | `1` |
| `VA_PRODUCTION_CONTEXT` | built-in description |
| `VA_BADMONKEY_URL` / `VA_BADMONKEY_KEY` | `http://127.0.0.1:4180` / — (the workspace's `BM_RESEARCH_KEY`) |
| `VA_AUTO_EXPORT` | `1` |
| `VA_DATA_DIR` | `~/.video-transcriber` |

If the model is down the video is still saved; press **Re-analyse** later.

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

> **Note:** The app auto-discovers `ffprobe`, `ffmpeg`, and `whisper` binaries at startup, including from `/opt/homebrew/bin` and `/usr/local/bin`, and exports that PATH for child tools. This matters when launching from Finder because Whisper internally calls `ffmpeg` by name.

### macOS Desktop App

Double-click `VideoAnalyzer.app` or `VideoTranscriber.app` on your Desktop if you've built the launcher. The launcher starts the local Flask server and opens the browser.

## How It Works

1. **Upload** — Drop any video file (MP4, MOV, WebM, MKV, AVI)
2. **Analyze** — ffprobe extracts video duration
3. **Extract Audio** — ffmpeg converts audio to 16kHz mono WAV (handles all codecs reliably)
4. **Transcribe** — Whisper processes the extracted audio locally (model selectable)
5. **Extract Frames** — ffmpeg pulls key frames at smart intervals:
   - Under 60s: every 3 seconds
   - 60-180s: every 10 seconds
   - Over 180s: every 20 seconds
   - Capped at 24 frames
6. **Generate Title** — Local LLM creates a 5-7 word headline
7. **Build Prompt** — Server-side construction with verified absolute frame paths
8. **Ready** — Copy for Claude, review the transcript, or browse frames

Long-running transcriptions send heartbeat events every 30s to keep the SSE connection alive — no timeouts on big videos.

## Troubleshooting

### Transcript says "Audio was present but no speech was detected"

If the video clearly has spoken audio, first make sure both tools are installed:

```bash
brew install openai-whisper ffmpeg
```

Finder-launched macOS apps do not inherit your full shell PATH. VideoAnalyzer now exports Homebrew and system binary paths before running Whisper so Whisper can find its own `ffmpeg` subprocess. If you still see this message after updating, quit the running local server and reopen the desktop app so it reloads the latest `app.py`.

### App opens but uses an old result

Processed videos are stored in `~/.video-transcriber/outputs/<job_id>/`. Results generated before a fix are not rewritten automatically; drop the source video into the app again to regenerate the transcript and frames.

## Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python Flask (single file `app.py`) |
| Frontend | Vanilla HTML/JS (`templates/index.html`), no build step |
| Transcription | OpenAI Whisper (local) |
| Frame extraction | ffmpeg |
| Title generation | Ollama gemma3:4b (optional, heuristic fallback) |
| Link downloads | yt-dlp (optional) |
| Lesson analysis | Any local OpenAI-compatible model (optional; default Ollama `qwen3.8:27b`) |
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
| POST | `/api/ingest-url` | Queue links: `{"url": "one or more links", "model": "base"}` |
| POST | `/api/import-tiktok` | Queue saved videos from TikTok's data-download JSON (multipart `file`) |
| GET | `/api/queue` | Jobs waiting for download |
| GET | `/api/thumbs/<job_id>/<file>` | Serve a thumbnail |
| POST | `/api/analyze/<job_id>` | Re-run the local model on a saved transcript |
| POST | `/api/export/<job_id>` | Send the lesson to BadMonkey Research |
| GET | `/api/health` | Tools present, queue, disk, model and export settings (used by health agents) |

## Project Structure

```
~/.video-transcriber/           # Runtime data
  ├── port                      # Current port
  ├── uploads/                  # Temporary (deleted after processing)
  ├── config.json               # Optional settings (see above)
  └── outputs/<job_id>/         # Persisted results
      ├── metadata.json         # All job data + Claude prompt, source, analysis, export state
      ├── thumbs/thumb_*.jpg    # Three lesson thumbnails
      ├── transcript.txt        # Clean transcript
      └── frames/frame_*.jpg    # Extracted key frames
```

## Contributing

PRs welcome. Keep it simple — `app.py` is the Flask app; `research.py` holds the optional links/analysis/export add-on.
Tests: `uv run --with flask python -m unittest discover -s tests`.

## License

MIT

---

Built by [VaultAI](https://vaultai.us)
