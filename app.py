"""VideoAnalyzer — Drop a video, get a transcript + key frames for Claude Code."""

import os
import sys
import uuid
import json
import hmac
import hashlib
import secrets
import signal
import socket
import stat
import subprocess
import threading
import queue
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, abort, request, jsonify, render_template, send_file, Response

app = Flask(__name__)

# Resolve binary paths — uv run may not inherit the full shell PATH
import shutil

_SEARCH_PATHS = ":".join(
    p for p in [
        os.environ.get("PATH", ""),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    if p
)
os.environ["PATH"] = _SEARCH_PATHS
FFPROBE = shutil.which("ffprobe", path=_SEARCH_PATHS) or "ffprobe"
FFMPEG = shutil.which("ffmpeg", path=_SEARCH_PATHS) or "ffmpeg"
WHISPER = shutil.which("whisper", path=_SEARCH_PATHS) or "whisper"
YTDLP = shutil.which("yt-dlp", path=_SEARCH_PATHS) or "yt-dlp"

DATA_DIR = Path(os.environ.get("VA_DATA_DIR", Path.home() / ".video-transcriber"))

# Finder-launched apps get no shell environment: ~/.video-transcriber/config.json fills the gaps.
_CONFIG_FILE = DATA_DIR / "config.json"
if _CONFIG_FILE.is_file():
    try:
        for _key, _value in json.loads(_CONFIG_FILE.read_text()).items():
            if isinstance(_value, (str, int, float)):
                os.environ.setdefault(_key, str(_value))
    except (OSError, ValueError):
        pass

import research  # noqa: E402  (needs the settings above)
UPLOADS = DATA_DIR / "uploads"
OUTPUTS = DATA_DIR / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)


def _load_local_api_token(path):
    """Create or read the agent token without ever following a token-file symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    while True:
        try:
            descriptor = os.open(path, flags)
            break
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, create_flags, 0o600)
            except FileExistsError:
                continue
            try:
                os.write(descriptor, (token + "\n").encode())
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            continue

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"Local API token is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
        token = os.read(descriptor, 4096).decode().strip()
    finally:
        os.close(descriptor)
    if len(token) < 32:
        raise RuntimeError(f"Local API token is invalid: {path}")
    return token


LOCAL_API_TOKEN = _load_local_api_token(DATA_DIR / "token")

jobs: dict[str, queue.Queue] = {}

NO_AUDIO_TRACK_MESSAGE = "[No audio track detected in this video.]"
NO_SPEECH_DETECTED_MESSAGE = "[Audio was present but no speech was detected. The video may be music-only, too noisy, or in a language Whisper could not recognize.]"


def _same_origin(origin):
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme == request.scheme
        and parsed.netloc.lower() == request.host.lower()
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


@app.before_request
def protect_local_api():
    host = request.host.lower()
    match = re.fullmatch(r"(?:127\.0\.0\.1|localhost):(\d{1,5})", host)
    if not match or not 1 <= int(match.group(1)) <= 65535:
        return jsonify({"error": "Invalid Host header"}), 400

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin", "")
        if origin:
            allowed = _same_origin(origin)
        else:
            supplied = request.headers.get("X-VideoAnalyzer-Token", "")
            allowed = bool(supplied) and hmac.compare_digest(supplied, LOCAL_API_TOKEN)
        if not allowed:
            return jsonify({"error": "Local request authorization required"}), 403


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    model = request.form.get("model", "base")
    if model not in WHISPER_MODELS:
        return jsonify({"error": "Unknown Whisper model"}), 400
    export = request.form.get("export", "1") != "0"
    job_id = uuid.uuid4().hex[:8]

    ext = re.sub(r"[^A-Za-z0-9.]", "", Path(f.filename or "video.mp4").suffix)[:8] or ".mp4"
    video_path = UPLOADS / f"{job_id}{ext}"
    f.save(video_path)

    jobs[job_id] = queue.Queue()

    t = threading.Thread(
        target=process_video, args=(job_id, video_path, f.filename, model),
        kwargs={"source": {"platform": "upload"}, "export": export}, daemon=True
    )
    t.start()

    return jsonify({"job_id": job_id, "filename": f.filename})


WHISPER_MODELS = ("tiny", "base", "small", "medium", "large", "turbo")


# ---- TikTok (and other saved-video) links ---------------------------------------------------------
# One video at a time: Whisper and the local model are heavy, and TikTok throttles bursts.
url_queue: "queue.Queue[tuple]" = queue.Queue()
waiting: list[str] = []
waiting_lock = threading.Lock()
INBOX_HEARTBEAT_SECONDS = 120


def _inbox_heartbeat(item_id, token, job_id, stop, lease_lost):
    while not stop.wait(INBOX_HEARTBEAT_SECONDS):
        try:
            research.inbox_report(item_id, token, "processing", job=job_id)
        except research.InboxLeaseLost:
            lease_lost.set()
            return


def _enqueue_url(url, model, export=True, inbox_id=None, inbox_token=None, file=None, source=None):
    """Queue a link (or, with file=, a media file: watched folder or a dropped R&D upload, whose display
    name, content-hash source key and kind ride in source=). One job runs at a time."""
    if inbox_id and not inbox_token:
        raise ValueError("An inbox fencing token is required")
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = queue.Queue()
    with waiting_lock:
        waiting.append(job_id)
        position = len(waiting)
    jobs[job_id].put({"status": "queued", "message": f"Queued ({position} in line)..."})
    lease = None
    if inbox_id:
        stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=_inbox_heartbeat,
            args=(inbox_id, inbox_token, job_id, stop, lease_lost),
            daemon=True,
        )
        heartbeat.start()
        lease = (stop, lease_lost, heartbeat)
    url_queue.put((job_id, url, model, export, inbox_id, inbox_token, lease, file, source))
    return job_id


def _outcome(job_id):
    """(ok, module_id, error) for a finished job, read from what it saved."""
    meta_file = OUTPUTS / _safe_id(job_id) / "metadata.json"
    if not meta_file.exists():
        return False, "", "VideoAnalyzer could not process this video (see its window for the error)."
    export = json.loads(meta_file.read_text()).get("export") or {}
    if export.get("ok"):
        return True, export.get("module_id", ""), ""
    return False, "", "Processed, but not saved to BadMonkey R&D: " + export.get("error", "export is off")


def _raise_if_inbox_abandoned(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise research.InboxLeaseLost("Inbox lease lost; abandoning the local job")


def _report_inbox_outcome(job_id, inbox_id, inbox_token, failure):
    try:
        ok, module_id, error = _outcome(job_id)
        return research.inbox_report(
            inbox_id,
            inbox_token,
            "done" if ok else "failed",
            module_id=module_id,
            error=failure or error,
            job=job_id,
        )
    except research.InboxLeaseLost:
        return False
    except Exception as exception:
        app.logger.warning(
            "Inbox finalization failed for %s: %s: %s",
            inbox_id,
            type(exception).__name__,
            exception,
        )
        return False


def _url_worker():
    while True:
        job_id, url, model, export, inbox_id, inbox_token, lease, file, file_source = url_queue.get()
        with waiting_lock:
            if job_id in waiting:
                waiting.remove(job_id)
        q = jobs[job_id]
        failure = ""
        lease_lost = lease[1] if lease else None
        try:
            _raise_if_inbox_abandoned(lease_lost)
            if file:
                video_path = UPLOADS / f"{job_id}{file.suffix.lower()}"
                shutil.copy2(file, video_path)
                if file_source is None:   # watched-folder file: keep it yours, moved to Done
                    research.file_done(file)
                else:                     # R&D upload temp file: ours to remove
                    file.unlink(missing_ok=True)
                _raise_if_inbox_abandoned(lease_lost)
                source = file_source or {"platform": "upload", "title": file.stem[:200]}
                display_name = source.get("title") or file.stem[:200]
                process_video(job_id, video_path, display_name, model, source=source, export=export, cancel_event=lease_lost)
            else:
                q.put({"status": "downloading", "message": "Downloading the video..."})
                video_path, source = research.download(YTDLP, url, UPLOADS, job_id)
                _raise_if_inbox_abandoned(lease_lost)
                process_video(job_id, video_path, source.get("title") or url, model, source=source, export=export, cancel_event=lease_lost)
        except Exception as e:
            failure = str(e)[:500]
            q.put({"status": "error", "message": failure})
        finally:
            try:
                if lease:
                    stop, lease_lost, heartbeat = lease
                    stop.set()
                    heartbeat.join()
                if inbox_id and not lease_lost.is_set():
                    _report_inbox_outcome(job_id, inbox_id, inbox_token, failure)
            finally:
                url_queue.task_done()


# ---- phone inbox (links shared from the iPhone, held by the BadMonkey workspace) and the watched folder ----
phone = {"enabled": False, "last_poll": 0, "last_error": "", "watch_dir": ""}


def _phone_poller():
    """Pull links sent from the phone. Takes one at a time so the workspace shows what is still waiting."""
    phone["enabled"] = research.export_configured()
    while phone["enabled"]:
        try:
            if len(waiting) < 2:
                claim = research.inbox_next()
                phone["last_poll"], phone["last_error"] = time.time(), ""
                item = claim.get("item")
                if item:
                    token = claim.get("token")
                    if not token:
                        raise RuntimeError("Inbox claim did not include a fencing token")
                    if str(item.get("url", "")).startswith("upload:"):
                        try:   # a file dropped on the R&D page: fetch its bytes, then the normal file pipeline
                            blob = research.upload_media(item["id"])
                            name = str(item.get("name") or "file")
                            suffix = Path(name).suffix.lower() or ".bin"
                            tmp = UPLOADS / f"drop-{item['id'][:12]}{suffix}"
                            tmp.write_bytes(blob)
                            _enqueue_url("", research.PHONE_MODEL, True, inbox_id=item["id"], inbox_token=token, file=tmp,
                                         source={"platform": "upload", "title": name[:200],
                                                 "key": "upload:" + hashlib.sha256(blob).hexdigest(),
                                                 "kind": research.upload_kind(name)})
                        except Exception as e:
                            research.inbox_report(item["id"], token, "failed", error=f"could not fetch the dropped file: {e}"[:500])
                        continue   # check for more right away
                    try:
                        link = research.normalize_url(item["url"])
                    except ValueError as e:
                        research.inbox_report(item["id"], token, "failed", error=str(e))
                        continue
                    _enqueue_url(link, research.PHONE_MODEL, True, inbox_id=item["id"], inbox_token=token)
                    continue   # check for more right away
            else:
                phone["last_poll"] = time.time()
        except Exception as e:   # workspace restarting or unreachable: try again shortly
            phone["last_error"] = f"{type(e).__name__}: {e}"[:300]
        time.sleep(research.PHONE_POLL)


def _watch_folder():
    folder = research.watch_dir()
    if not folder:
        return
    phone["watch_dir"] = str(folder)
    seen = {}
    while True:
        try:
            for file, size in research.ready_files(folder, seen):
                _enqueue_url("", research.PHONE_MODEL, True, file=file)
        except OSError as e:
            phone["last_error"] = f"watch folder: {e}"[:300]
        time.sleep(15)


def _queue_ticker():
    # Keeps waiting browsers connected (the stream gives up after 120 s of silence).
    while True:
        time.sleep(30)
        with waiting_lock:
            snapshot = list(waiting)
        for i, job_id in enumerate(snapshot, 1):
            if job_id in jobs:
                jobs[job_id].put({"status": "queued", "message": f"Queued ({i} in line)..."})


threading.Thread(target=_url_worker, daemon=True).start()
threading.Thread(target=_queue_ticker, daemon=True).start()
if os.environ.get("VA_BACKGROUND", "1") == "1":   # tests switch the phone poller and folder watcher off
    threading.Thread(target=_phone_poller, daemon=True).start()
    threading.Thread(target=_watch_folder, daemon=True).start()


def _known_sources():
    known = set()
    for meta_file in OUTPUTS.glob("*/metadata.json"):
        try:
            key = json.loads(meta_file.read_text()).get("source", {}).get("key")
        except (OSError, ValueError):
            continue
        if key:
            known.add(key)
    return known


@app.route("/api/ingest-url", methods=["POST"])
def ingest_url():
    """Paste one or more TikTok links (one per line). Each is downloaded and processed in turn."""
    data = request.get_json(silent=True) or {}
    model = data.get("model", "base")
    if model not in WHISPER_MODELS:
        return jsonify({"error": "Unknown Whisper model"}), 400
    raw = data.get("urls") if isinstance(data.get("urls"), list) else str(data.get("url", "")).split()
    links, rejected = [], []
    for item in raw[:200]:
        try:
            links.append(research.normalize_url(str(item)))
        except ValueError as e:
            rejected.append({"url": str(item)[:200], "error": str(e)})
    known = _known_sources()
    queued, skipped = [], []
    for link in dict.fromkeys(links):
        key = research.source_key_for_url(link)
        if key and key in known and not data.get("force"):
            skipped.append(link)
            continue
        queued.append({"url": link, "job_id": _enqueue_url(link, model, data.get("export", True) is not False)})
    if not queued and not skipped:
        return jsonify({"error": "No supported video links found.", "rejected": rejected}), 400
    return jsonify({"queued": queued, "skipped": skipped, "rejected": rejected})


@app.route("/api/import-tiktok", methods=["POST"])
def import_tiktok():
    """Upload TikTok's 'Download your data' JSON; every Favorite (saved) video is queued. Likes with include=likes."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Choose the user_data JSON file from your TikTok data download."}), 400
    try:
        export = json.loads(f.read(60_000_000))
    except ValueError:
        return jsonify({"error": "That file is not JSON. In TikTok, request your data in JSON format."}), 400
    include = ("favorite", "like") if request.form.get("include") == "likes" else ("favorite",)
    links = research.links_from_tiktok_export(export, include)
    if not links:
        return jsonify({"error": "No saved (Favorite) videos were found in that file."}), 400
    limit = max(1, min(int(request.form.get("limit", "50") or 50), 500))
    known = _known_sources()
    fresh = [u for u in links if research.source_key_for_url(u) not in known]
    queued = [{"url": u, "job_id": _enqueue_url(u, request.form.get("model", "base") if request.form.get("model") in WHISPER_MODELS else "base")} for u in fresh[:limit]]
    return jsonify({"found": len(links), "already_saved": len(links) - len(fresh), "queued": queued, "left_for_later": max(0, len(fresh) - limit)})


@app.route("/api/queue")
def queue_status():
    with waiting_lock:
        return jsonify({"waiting": list(waiting)})


@app.route("/api/health")
def health():
    tools = {name: bool(shutil.which(path) or Path(path).is_file()) for name, path in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE), ("whisper", WHISPER), ("yt-dlp", YTDLP))}
    free = shutil.disk_usage(DATA_DIR).free
    return jsonify({
        "ok": all(tools.values()) and free > 2e9,
        "tools": tools,
        "queue": len(waiting),
        "disk_free_gb": round(free / 1e9, 1),
        "analysis_model": research.LLM_MODEL,
        "analysis_url": research.LLM_URL,
        "badmonkey_export": research.export_configured(),
        "phone_inbox": {"enabled": phone["enabled"], "last_poll": phone["last_poll"], "last_error": phone["last_error"]},
        "watch_dir": phone["watch_dir"],
    })


@app.route("/api/export/<job_id>", methods=["POST"])
def export_job(job_id):
    meta_file = OUTPUTS / _safe_id(job_id) / "metadata.json"
    if not meta_file.exists():
        return jsonify({"error": "Not found"}), 404
    meta = json.loads(meta_file.read_text())
    meta["export"] = research.export(meta, meta_file.parent)
    meta_file.write_text(json.dumps(meta, indent=2))
    status = 200 if meta["export"].get("ok") else 502
    return jsonify(meta["export"]), status


@app.route("/api/analyze/<job_id>", methods=["POST"])
def reanalyze(job_id):
    """Run the local model again on a saved transcript (e.g. after starting Ollama)."""
    out_dir = OUTPUTS / _safe_id(job_id)
    meta_file = out_dir / "metadata.json"
    if not meta_file.exists():
        return jsonify({"error": "Not found"}), 404
    meta = json.loads(meta_file.read_text())
    meta["analysis"] = research.analyze(meta.get("transcript", ""), meta.get("source", {}), [out_dir / "thumbs" / t for t in meta.get("thumbnails", [])])
    if meta["analysis"].get("lesson_name"):
        meta["summary"] = meta["analysis"]["lesson_name"]
    meta_file.write_text(json.dumps(meta, indent=2))
    return jsonify(meta)


def _safe_id(job_id):
    if not re.fullmatch(r"[a-f0-9]{8}", job_id or ""):
        abort(404)
    return job_id


import urllib.request
import urllib.error


def _make_summary(transcript: str) -> str:
    """Generate a 5-7 word article-style title. Uses local LLM, falls back to heuristic."""
    if not transcript or transcript in (NO_AUDIO_TRACK_MESSAGE, NO_SPEECH_DETECTED_MESSAGE):
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


def transcribe_timeout(duration):
    """Seconds Whisper may run: three times the audio length, never less than 10 minutes."""
    return max(600, int((duration or 0) * 3))


def _run_with_heartbeat(cmd, q, status_msg, timeout=600, cancel_event=None):
    """Run a subprocess with periodic heartbeat messages, draining pipes properly."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    done = threading.Event()

    def heartbeat():
        while not done.wait(30):
            q.put({"status": "processing", "message": f"{status_msg} (still working)"})

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            _raise_if_inbox_abandoned(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                stdout, stderr = proc.communicate()
                break
            try:
                stdout, stderr = proc.communicate(timeout=min(1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except research.InboxLeaseLost:
        proc.kill()
        proc.communicate()
        raise
    finally:
        done.set()
    return proc.returncode, stdout, stderr


def process_video(job_id, video_path, original_name, model, source=None, export=True, cancel_event=None):
    job_id = _safe_id(job_id)
    _raise_if_inbox_abandoned(cancel_event)
    q = jobs[job_id]
    out_dir = OUTPUTS / job_id
    out_dir.mkdir(exist_ok=True)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    try:
        kind = (source or {}).get("kind") or "video"
        if kind == "image":   # the picture IS the reference: no probing, no audio, no frames
            duration = 0.0
            transcript = ""
            transcript_text = ""
        else:
            # Analyze
            q.put({"status": "analyzing", "message": "Analyzing media..."})
            result = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(video_path)],
                capture_output=True, text=True,
            )
            _raise_if_inbox_abandoned(cancel_event)
            duration = float(result.stdout.strip()) if result.stdout.strip() else 0

            # Extract audio to WAV first — handles all codecs reliably
            q.put({"status": "transcribing", "message": "Extracting audio..."})
            wav_path = out_dir / "audio.wav"
            audio_result = subprocess.run(
                [FFMPEG, "-i", str(video_path), "-vn", "-acodec", "pcm_s16le",
                 "-ar", "16000", "-ac", "1", str(wav_path), "-y"],
                capture_output=True, timeout=120,
            )
            _raise_if_inbox_abandoned(cancel_event)

            transcript = ""
            transcript_text = NO_AUDIO_TRACK_MESSAGE
            has_audio_track = (
                audio_result.returncode == 0
                and wav_path.exists()
                and wav_path.stat().st_size > 1000
            )
            if has_audio_track:
                # Transcribe the extracted audio
                q.put({"status": "transcribing", "message": f"Transcribing with whisper-{model}..."})
                fast = research.fast_whisper(model)
                command = ([fast[0], "-m", str(fast[1]), "-f", str(wav_path), "-l", "auto", "-otxt", "-of", str(out_dir / "audio"), "-np"]
                           if fast else [WHISPER, str(wav_path), "--model", model, "--output_dir", str(out_dir), "--output_format", "txt"])
                rc, stdout, stderr = _run_with_heartbeat(
                    command,
                    q, f"Transcribing with whisper-{model}", timeout=transcribe_timeout(duration), cancel_event=cancel_event,
                )
                _raise_if_inbox_abandoned(cancel_event)
                if rc is not None and rc < 0:
                    minutes = transcribe_timeout(duration) // 60
                    raise RuntimeError(f"Transcription stopped after {minutes} minutes for a {duration / 60:.0f}-minute video.")
                if rc != 0:
                    raise RuntimeError(f"Whisper failed: {stderr[-500:].decode(errors='replace')}")

                transcript_file = out_dir / "audio.txt"
                if not transcript_file.exists():
                    whisper_output = (stderr + stdout)[-1000:].decode(errors="replace").strip()
                    raise RuntimeError(
                        "Whisper did not create a transcript file."
                        + (f" Output: {whisper_output}" if whisper_output else "")
                    )
                transcript = transcript_file.read_text().strip()
                if transcript_file.exists():
                    transcript_file.unlink()

                if transcript:
                    transcript_text = transcript
                else:
                    transcript_text = NO_SPEECH_DETECTED_MESSAGE

            wav_path.unlink(missing_ok=True)

        # Save clean transcript
        (out_dir / "transcript.txt").write_text(transcript_text)

        frame_files = []
        thumbs = []
        if kind == "image":
            # One JPEG thumbnail of the picture itself, sized for the lesson ingest
            q.put({"status": "frames", "message": "Preparing the image..."})
            thumbs_dir = out_dir / "thumbs"
            thumbs_dir.mkdir(exist_ok=True)
            target = thumbs_dir / "thumb_1.jpg"
            rc, _, stderr = _run_with_heartbeat(
                [FFMPEG, "-i", str(video_path), "-vf", "scale='min(1440,iw)':-2", "-q:v", "5", str(target), "-y"],
                q, "Preparing the image", cancel_event=cancel_event,
            )
            _raise_if_inbox_abandoned(cancel_event)
            if rc != 0 or not target.is_file() or target.stat().st_size >= 400_000:
                rc, _, stderr = _run_with_heartbeat(
                    [FFMPEG, "-i", str(video_path), "-vf", "scale=720:-2", "-q:v", "8", str(target), "-y"],
                    q, "Preparing the image", cancel_event=cancel_event,
                )
                _raise_if_inbox_abandoned(cancel_event)
            if rc != 0 or not target.is_file() or not 0 < target.stat().st_size < 400_000:
                raise RuntimeError(f"Could not turn the image into a storable thumbnail: {stderr[-300:].decode(errors='replace') if rc else 'too large'}")
            thumbs = [target.name]
        elif kind == "audio":
            q.put({"status": "frames", "message": "Audio-only file — no frames to capture..."})
        else:
            # Extract frames
            q.put({"status": "frames", "message": "Extracting key frames..."})

            total_secs = max(int(duration), 1)
            if total_secs < 60:
                interval = 3
            elif total_secs < 180:
                interval = 10
            else:
                interval = 20

            rc, stdout, stderr = _run_with_heartbeat(
                [FFMPEG, "-i", str(video_path),
                 "-vf", f"fps=1/{interval},scale=640:-2",
                 "-q:v", "3", str(frames_dir / "frame_%03d.jpg"), "-y"],
                q, "Extracting key frames", cancel_event=cancel_event,
            )
            _raise_if_inbox_abandoned(cancel_event)
            if rc != 0:
                raise RuntimeError(f"FFmpeg failed: {stderr[-500:].decode(errors='replace')}")

            # Cap at 24 frames
            frame_files = sorted(frames_dir.glob("*.jpg"))
            for f in frame_files[24:]:
                f.unlink()
            frame_files = frame_files[:24]

            # A few spread-out thumbnails so the lesson is easy to recognise later
            q.put({"status": "frames", "message": "Capturing thumbnails..."})
            thumbs = research.thumbnails(FFMPEG, video_path, duration, out_dir / "thumbs", frame_files)
            _raise_if_inbox_abandoned(cancel_event)

        # Remove the uploaded input to save disk
        video_path.unlink(missing_ok=True)

        duration_fmt = f"{int(duration // 60)}:{int(duration % 60):02d}"
        source = dict(source or {})
        source.setdefault("key", research.source_key(source, job_id))
        q.put({"status": "analyzing_lesson", "message": f"Local model is reading the lesson ({research.LLM_MODEL})..."})
        analysis = research.analyze(transcript_text, source, [out_dir / "thumbs" / t for t in thumbs])
        _raise_if_inbox_abandoned(cancel_event)
        summary = analysis.get("lesson_name") or _make_summary(transcript_text)
        frame_names = [f.name for f in frame_files]
        frame_paths = [str(frames_dir / f.name) for f in frame_files]

        # Pre-build the Claude clipboard text server-side (verified paths)
        claude_lines = [
            f"I have a video I want you to understand. Use the Read tool on each frame path listed below (paths are in backticks), then combine the visual evidence with the transcript to explain what the video is about — the key concepts, techniques, or ideas being shown. After your summary, ask me what I'd like to do with this information.",
            "",
            f"Video: {original_name} ({duration_fmt})",
            "",
            "## Transcript",
            transcript_text,
            "",
            f"## Key Frames ({len(frame_files)} total):",
        ] + [f"- Frame {i:02d}: `{p}`" for i, p in enumerate(frame_paths, start=1)]

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
            "source": source,
            "thumbnails": thumbs,
            "analysis": analysis,
        }
        (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

        if export and research.export_configured() and research.AUTO_EXPORT:
            _raise_if_inbox_abandoned(cancel_event)
            q.put({"status": "exporting", "message": "Saving to BadMonkey R&D..."})
            meta["export"] = research.export(meta, out_dir)
            (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

        q.put({"status": "complete", "message": "Done!", "data": meta})

    except Exception as e:
        q.put({"status": "error", "message": str(e)})


@app.route("/api/stream/<job_id>")
def stream(job_id):
    job_id = _safe_id(job_id)

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
    path = OUTPUTS / _safe_id(job_id) / "frames" / filename
    if not path.exists():
        return "Not found", 404
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/thumbs/<job_id>/<filename>")
def serve_thumb(job_id, filename):
    if not re.fullmatch(r"thumb_\d\.jpg", filename):
        return "Not found", 404
    path = OUTPUTS / _safe_id(job_id) / "thumbs" / filename
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
    target = OUTPUTS / _safe_id(job_id)
    meta_file = target / "metadata.json"

    data = request.get_json()
    new_status = data.get("status", "")
    if new_status not in ("new", "doing", "skipped", "revisit", "delete"):
        return jsonify({"error": "Invalid status"}), 400

    if new_status == "delete":
        if target.parent != OUTPUTS or target.is_symlink() or not target.is_dir():
            return jsonify({"error": "Not found"}), 404
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
