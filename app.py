"""VideoAnalyzer — Drop a video, get a transcript + key frames for Claude Code."""

import os
import sys
import uuid
import json
import signal
import socket
import subprocess
import threading
import queue
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file, Response

app = Flask(__name__)

DATA_DIR = Path.home() / ".video-transcriber"
UPLOADS = DATA_DIR / "uploads"
OUTPUTS = DATA_DIR / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

jobs: dict[str, queue.Queue] = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    model = request.form.get("model", "base")
    job_id = uuid.uuid4().hex[:8]

    ext = Path(f.filename).suffix
    video_path = UPLOADS / f"{job_id}{ext}"
    f.save(video_path)

    jobs[job_id] = queue.Queue()

    t = threading.Thread(
        target=process_video, args=(job_id, video_path, f.filename, model), daemon=True
    )
    t.start()

    return jsonify({"job_id": job_id, "filename": f.filename})


import re


import urllib.request
import urllib.error


def _make_summary(transcript: str) -> str:
    """Generate a 5-7 word article-style title. Uses local LLM, falls back to heuristic."""
    if not transcript or transcript == "No audio detected.":
        return "No audio"

    title = _llm_title(transcript)
    return title if title else _heuristic_title(transcript)


def _llm_title(transcript: str) -> str | None:
    """Ask local ollama for a short headline."""
    snippet = " ".join(transcript.split()[:300])

    payload = json.dumps({
        "model": "gemma3:4b",
        "messages": [{"role": "user", "content": (
            "Write a 5-7 word article headline summarizing this video transcript. "
            "Be specific — include product names, techniques, or key concepts. "
            "Return ONLY the headline, no quotes, no period.\n\n"
            f"Transcript: {snippet}"
        )}],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 30},
    }).encode()

    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            title = data.get("message", {}).get("content", "").strip()
            title = title.strip('"\'').split("\n")[0].strip().rstrip(".")
            if 3 <= len(title.split()) <= 10 and len(title) < 80:
                return title
    except Exception:
        pass

    return None


def _heuristic_title(transcript: str) -> str:
    """Fallback: compressed first sentence."""
    text = re.sub(r"\s+", " ", transcript).strip()
    first = re.split(r"[.!?\n]", text)[0].strip()
    drop = {"a", "an", "the", "of", "from", "just", "actually", "really",
            "very", "basically", "exactly", "literally", "simply",
            "own", "single", "entire", "that", "will", "would", "can",
            "could", "are", "is", "was", "their", "your", "its", "his",
            "her", "my", "our", "lot", "massive", "amount", "ton",
            "you", "you're", "if"}
    words = [w for w in first.split() if w.lower().rstrip(",.") not in drop]
    cap = min(len(words), 7)
    weak = {"in", "on", "at", "to", "for", "with", "by", "and", "but", "or"}
    while cap > 4 and words[cap - 1].lower().rstrip(",.") in weak:
        cap -= 1
    s = " ".join(words[:cap]).rstrip(",.;:!?")
    return (s[0].upper() + s[1:]) if s else "Untitled"


def process_video(job_id, video_path, original_name, model):
    q = jobs[job_id]
    out_dir = OUTPUTS / job_id
    out_dir.mkdir(exist_ok=True)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    try:
        # Analyze
        q.put({"status": "analyzing", "message": "Analyzing video..."})
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True,
        )
        duration = float(result.stdout.strip()) if result.stdout.strip() else 0

        # Transcribe (with heartbeat so SSE stream doesn't timeout on long videos)
        q.put({"status": "transcribing", "message": f"Transcribing with whisper-{model}..."})
        whisper_proc = subprocess.Popen(
            ["whisper", str(video_path), "--model", model, "--output_dir", str(out_dir),
             "--output_format", "txt", "--language", "en"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        while True:
            try:
                whisper_proc.wait(timeout=30)
                break  # process finished
            except subprocess.TimeoutExpired:
                q.put({"status": "transcribing", "message": f"Transcribing with whisper-{model}... (still working)"})
        if whisper_proc.returncode != 0:
            stderr = whisper_proc.stderr.read()
            raise RuntimeError(f"Whisper failed: {stderr[:500]}")

        transcript_file = out_dir / f"{video_path.stem}.txt"
        transcript = transcript_file.read_text().strip() if transcript_file.exists() else ""

        # Also save as clean transcript.txt
        (out_dir / "transcript.txt").write_text(transcript or "No audio detected.")

        # Extract frames
        q.put({"status": "frames", "message": "Extracting key frames..."})

        total_secs = max(int(duration), 1)
        if total_secs < 60:
            interval = 3
        elif total_secs < 180:
            interval = 10
        else:
            interval = 20

        ffmpeg_proc = subprocess.Popen(
            ["ffmpeg", "-i", str(video_path),
             "-vf", f"fps=1/{interval},scale=640:-2",
             "-q:v", "3", str(frames_dir / "frame_%03d.jpg"), "-y"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        while True:
            try:
                ffmpeg_proc.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                q.put({"status": "frames", "message": "Extracting key frames... (still working)"})
        if ffmpeg_proc.returncode != 0:
            stderr = ffmpeg_proc.stderr.read()
            raise RuntimeError(f"FFmpeg failed: {stderr[:500]}")

        # Cap at 24 frames
        frame_files = sorted(frames_dir.glob("*.jpg"))
        for f in frame_files[24:]:
            f.unlink()
        frame_files = frame_files[:24]

        # Clean up the whisper .txt duplicate if needed
        if transcript_file.exists() and transcript_file.name != "transcript.txt":
            transcript_file.unlink()

        # Remove uploaded video to save disk
        video_path.unlink(missing_ok=True)

        duration_fmt = f"{int(duration // 60)}:{int(duration % 60):02d}"
        transcript_text = transcript or "No audio detected."
        summary = _make_summary(transcript_text)
        frame_names = [f.name for f in frame_files]
        frame_paths = [str(frames_dir / f.name) for f in frame_files]

        # Pre-build the Claude clipboard text server-side (verified paths)
        claude_lines = [
            f"I have a video I want you to understand. First read the transcript and all the frames below, then explain what the video is about — the key concepts, techniques, or ideas being shown. After your summary, ask me what I'd like to do with this information.",
            "",
            f"Video: {original_name} ({duration_fmt})",
            "",
            "## Transcript",
            transcript_text,
            "",
            f"## Key Frames ({len(frame_files)} — read each one)",
        ] + [f"read {p}" for p in frame_paths]

        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        meta = {
            "summary": summary,
            "original_name": original_name,
            "duration": round(duration, 1),
            "duration_fmt": duration_fmt,
            "model": model,
            "transcript": transcript_text,
            "frame_count": len(frame_files),
            "frames": frame_names,
            "output_dir": str(out_dir),
            "claude_prompt": "\n".join(claude_lines),
            "item_status": "new",
            "status_date": now,
            "created_at": now,
        }
        (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

        q.put({"status": "complete", "message": "Done!", "data": meta})

    except Exception as e:
        q.put({"status": "error", "message": str(e)})


@app.route("/api/stream/<job_id>")
def stream(job_id):
    def generate():
        q = jobs.get(job_id)
        if not q:
            yield f"data: {json.dumps({'status': 'error', 'message': 'Job not found'})}\n\n"
            return
        while True:
            try:
                event = q.get(timeout=120)
                yield f"data: {json.dumps(event)}\n\n"
                if event["status"] in ("complete", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Processing timeout'})}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/frames/<job_id>/<filename>")
def serve_frame(job_id, filename):
    path = OUTPUTS / job_id / "frames" / filename
    if not path.exists():
        return "Not found", 404
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/history")
def history():
    results = []
    if OUTPUTS.exists():
        for d in sorted(OUTPUTS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            meta_file = d / "metadata.json"
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                meta["job_id"] = d.name
                results.append(meta)
    return jsonify(results[:50])


@app.route("/api/status/<job_id>", methods=["PUT"])
def update_status(job_id):
    import shutil
    target = OUTPUTS / job_id
    meta_file = target / "metadata.json"

    data = request.get_json()
    new_status = data.get("status", "")
    if new_status not in ("new", "doing", "skipped", "revisit", "delete"):
        return jsonify({"error": "Invalid status"}), 400

    if new_status == "delete":
        if target.exists():
            shutil.rmtree(target)
        return jsonify({"ok": True, "deleted": True})

    if not meta_file.exists():
        return jsonify({"error": "Not found"}), 404

    meta = json.loads(meta_file.read_text())
    meta["item_status"] = new_status
    meta["status_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta_file.write_text(json.dumps(meta, indent=2))

    return jsonify({"ok": True, "item_status": new_status, "status_date": meta["status_date"]})


PORT_FILE = DATA_DIR / "port"


def find_port(preferred=7745):
    """Find an available port, starting with the preferred one."""
    for port in [preferred] + list(range(7746, 7800)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    # Fallback: let OS pick
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def cleanup(signum=None, frame=None):
    PORT_FILE.unlink(missing_ok=True)
    sys.exit(0)


if __name__ == "__main__":
    port = find_port()
    PORT_FILE.write_text(str(port))

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    print(f"\n  VideoAnalyzer running at http://localhost:{port}\n")

    try:
        app.run(host="127.0.0.1", port=port, debug=False)
    finally:
        PORT_FILE.unlink(missing_ok=True)
