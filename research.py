"""Research add-on: saved TikTok links in, an analysed "lesson" out to the BadMonkey Research module.

- download():  yt-dlp fetches a public or saved video (optionally with your browser's TikTok cookies)
- thumbnails(): three spread-out frames, small enough to store with the lesson
- analyze():   a LOCAL model (Ollama / vllm-mlx / LM Studio, OpenAI-compatible) names the lesson, summarises it
               and recommends whether it adds value to the BadMonkey production flow
- export():    sends the lesson to the BadMonkey workspace on this machine (machine route + research key)

Settings (environment or ~/.video-transcriber/config.json):
  VA_LLM_URL       default http://127.0.0.1:11434/v1   (Ollama)
  VA_LLM_MODEL     default qwen3.8:27b
  VA_LLM_VISION    1 (default) sends the thumbnails as images too; falls back to text when the model refuses them
  VA_PRODUCTION_CONTEXT  path to a file describing your production flow (BadMonkey: research/PRODUCTION-CONTEXT.md)
  VA_BADMONKEY_URL default http://127.0.0.1:4181      VA_BADMONKEY_KEY  = the workspace's BM_RESEARCH_KEY
  VA_AUTO_EXPORT   1 (default) exports every finished link/upload; 0 = only with the "Send to BadMonkey" button
  VA_YTDLP_COOKIES_BROWSER  e.g. safari or chrome, for videos TikTok only shows when signed in
"""
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import research_egress


logger = logging.getLogger(__name__)


class InboxLeaseLost(RuntimeError):
    """The workspace no longer recognizes this item's fencing token."""

LLM_URL = os.environ.get("VA_LLM_URL", "http://127.0.0.1:11434/v1").rstrip("/")
LLM_MODEL = os.environ.get("VA_LLM_MODEL", "qwen3.8:27b")
LLM_VISION = os.environ.get("VA_LLM_VISION", "1") == "1"
# Thinking models (qwen3.8) spent the whole reply budget thinking and returned no JSON; off by default.
LLM_THINK = os.environ.get("VA_LLM_THINK", "0") == "1"
BADMONKEY_URL = os.environ.get("VA_BADMONKEY_URL", "http://127.0.0.1:4181").rstrip("/")
BADMONKEY_KEY = os.environ.get("VA_BADMONKEY_KEY", "")
AUTO_EXPORT = os.environ.get("VA_AUTO_EXPORT", "1") == "1"
COOKIES_BROWSER = os.environ.get("VA_YTDLP_COOKIES_BROWSER", "")

ALLOWED_HOSTS = {
    "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "tiktokv.com", "www.tiktokv.com",
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "instagram.com", "www.instagram.com",
} | {h.strip().lower() for h in os.environ.get("VA_EXTRA_HOSTS", "").split(",") if h.strip()}

PHONE_POLL = int(os.environ.get("VA_INBOX_POLL", "20"))
PHONE_MODEL = os.environ.get("VA_PHONE_WHISPER_MODEL", "small")   # "base" misheard product names ("mu spark" for Muse Spark)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

CATEGORIES = ("AI agents", "Video & motion", "Design", "Dev tooling", "Marketing", "Sales", "Operations", "Other")
VERDICTS = ("adopt", "trial", "watch", "skip")
TIKTOK_ID = re.compile(r"/(?:video|v|share/video)/(\d{8,25})")


# ---- links ---------------------------------------------------------------------------------------
def normalize_url(raw):
    url = raw.strip().strip("<>\"'")
    if url.startswith("http://"):
        url = "https://" + url[7:]
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ALLOWED_HOSTS or parts.username or parts.port:
        raise ValueError("not a supported video link")
    if "tiktok" in host:
        match = TIKTOK_ID.search(parts.path)
        if match:   # canonical form; tracking query strings dropped
            return f"https://www.tiktok.com/@_/video/{match.group(1)}"
        return f"https://{host}{parts.path}"   # short vm./vt. links: yt-dlp follows the redirect
    return url


def platform_for(url):
    host = (urlsplit(url).hostname or "").lower()
    return "tiktok" if "tiktok" in host else "youtube" if "youtu" in host else "instagram" if "instagram" in host else "web"


def source_key_for_url(url):
    match = TIKTOK_ID.search(urlsplit(url).path)
    if match:
        return f"tiktok:{match.group(1)}"
    return None   # short links are only known after download


def source_key(source, job_id):
    if source.get("platform") and source.get("id"):
        return f"{source['platform']}:{re.sub(r'[^A-Za-z0-9._-]', '', str(source['id']))[:120]}"
    if source.get("url"):
        return "url:" + hashlib.sha1(source["url"].encode()).hexdigest()[:20]
    return f"upload:{job_id}"


def links_from_tiktok_export(data, include=("favorite",)):
    """Find saved-video links in TikTok's 'Download your data' JSON, whatever its exact layout this year."""
    found = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + [str(key).lower()])
        elif isinstance(value, list):
            for child in value:
                walk(child, path)
        elif isinstance(value, str) and "tiktok" in value and TIKTOK_ID.search(value):
            joined = " ".join(path)
            # TikTok names the likes list "Like List" → "ItemFavoriteList", so likes are told apart by "like".
            kind = "like" if "like" in joined else "favorite" if "favorite" in joined else ""
            if kind and kind in include:
                try:
                    found.append(normalize_url(value))
                except ValueError:
                    pass

    walk(data, [])
    return list(dict.fromkeys(found))


# ---- download ------------------------------------------------------------------------------------
def download(ytdlp, url, uploads, job_id, run=subprocess.run):
    template = str(Path(uploads) / f"{job_id}.%(ext)s")
    with research_egress.loopback_proxy() as proxy_url:
        cmd = [ytdlp, "--proxy", proxy_url, "--no-playlist", "--no-progress", "--no-warnings", "-f", "mp4/bestvideo*+bestaudio/best",
               "--merge-output-format", "mp4", "--max-filesize", "300M", "--write-info-json", "-o", template,
               "--print", "after_move:filepath", url]
        if COOKIES_BROWSER:
            cmd[1:1] = ["--cookies-from-browser", COOKIES_BROWSER]
        sandbox = shutil.which("sandbox-exec")
        if sandbox:
            port = proxy_url.rsplit(":", 1)[1]
            profile = ("(version 1) (allow default) (deny network-outbound) "
                       f'(allow network-outbound (remote ip "localhost:{port}"))')
            cmd = [sandbox, "-p", profile] + cmd
        else:
            logger.warning("sandbox-exec is unavailable; running yt-dlp without the macOS sandbox wrapper")
        try:
            result = run(cmd, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            raise RuntimeError("yt-dlp is not installed on the Studio. Run: brew install yt-dlp")
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        hint = " TikTok may need you signed in: set VA_YTDLP_COOKIES_BROWSER=safari (or chrome)." if "login" in tail[0].lower() or "private" in tail[0].lower() else ""
        raise RuntimeError("Download failed: " + tail[0][:300] + hint)
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    video = Path(lines[-1]) if lines else None
    if not video or not video.is_file():
        candidates = [p for p in Path(uploads).glob(f"{job_id}.*") if not p.name.endswith(".json")]
        if not candidates:
            raise RuntimeError("Download finished but no video file was saved.")
        video = candidates[0]
    info_file = Path(uploads) / f"{job_id}.info.json"
    info = {}
    if info_file.is_file():
        try:
            info = json.loads(info_file.read_text())
        except ValueError:
            pass
        info_file.unlink(missing_ok=True)
    page = info.get("webpage_url") or url
    source = {
        "platform": platform_for(page),
        "url": page if page.startswith("https://") else url,
        "id": str(info.get("id") or ""),
        "author": "@" + info["uploader_id"] if info.get("uploader_id") and not str(info["uploader_id"]).startswith("@") else (info.get("uploader_id") or info.get("uploader") or ""),
        "title": (info.get("title") or info.get("description") or "")[:200],
        "description": (info.get("description") or "")[:2000],
        "tags": [t for t in (info.get("tags") or []) if isinstance(t, str)][:20],
    }
    if not source["id"]:
        source["id"] = (source_key_for_url(url) or "").split(":")[-1]
    return video, source


# ---- thumbnails ----------------------------------------------------------------------------------
def thumbnails(ffmpeg, video, duration, out_dir, frame_files=(), run=subprocess.run, count=3):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []
    if duration and duration > 1:
        for index, fraction in enumerate((0.15, 0.5, 0.85)[:count], 1):
            target = out_dir / f"thumb_{index}.jpg"
            at = max(0.5, duration * fraction)
            done = run([ffmpeg, "-v", "error", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "5", str(target), "-y"],
                       capture_output=True, timeout=60)
            if done.returncode == 0 and target.is_file() and 0 < target.stat().st_size < 400_000:
                names.append(target.name)
    if not names:   # fall back to the key frames already extracted
        frames = list(frame_files)
        picks = [frames[i] for i in sorted({0, len(frames) // 2, len(frames) - 1})] if frames else []
        for index, frame in enumerate(picks[:count], 1):
            target = out_dir / f"thumb_{index}.jpg"
            data = Path(frame).read_bytes()
            if len(data) < 400_000:
                target.write_bytes(data)
                names.append(target.name)
    return names


# ---- analysis ------------------------------------------------------------------------------------
DEFAULT_CONTEXT = """BadMonkey is a small creative and AI studio. Production flow: client brief → ideation → video prompts and
storyboards → AI image generation → AI video (Kling) → edit and continuity → 60fps interpolation → web media encodes →
scroll-driven motion websites → mobile → QA → publish. Also: an admin workspace (CRM, campaigns, client portal with chat and
voice), outreach email, and a private local AI stack (Qwen and GLM models on a Mac Studio). We value tools that save
production hours, raise visual quality, or let local agents do more of the work, without sending client data to the cloud."""

SYSTEM = """You turn a short video (a tutorial, a demo, or a news roundup) into a research lesson for a studio's knowledge base, and judge whether it is worth adopting.
Answer with ONE JSON object only, no prose:
{"lesson_name": "5-9 words, specific, names the tool or technique",
 "summary": "3-6 sentences: what is taught, how it works, what you would need",
 "key_points": ["up to 8 short, concrete points"],
 "tools": ["named products, models, libraries, commands"],
 "category": one of %s,
 "recommendation": {"verdict": one of "adopt" | "trial" | "watch" | "skip",
   "value_score": integer 0-10 (value to OUR production flow, not general coolness),
   "production_fit": "which stage of our flow it improves and how, or why it does not fit",
   "rationale": "2-4 sentences, honest about hype, cost, risk, privacy (client data must stay local)",
   "effort": "S" | "M" | "L",
   "implementation_steps": ["up to 6 concrete steps to try it in our stack"]}}
If the video covers several tools or releases (a roundup), the lesson covers ALL of them: lesson_name names the topic and the
items (e.g. "Local AI roundup: Muse Spark, Nemotron 3.5, Bonsai 2B"), the summary gives each item at least one sentence,
key_points has at least one point per item, and tools lists every one. Judge the recommendation on the item most useful to
our production and name it in production_fit.
Speech-to-text often misspells names. Correct a name only when the caption, hashtags or pictures show the right spelling;
otherwise keep the transcript's spelling.
If the video is mostly hype with no usable technique, say so and use "skip". Never invent tools that were not mentioned or shown.""" % json.dumps(list(CATEGORIES))


def production_context():
    path = os.environ.get("VA_PRODUCTION_CONTEXT", "")
    if path and Path(path).is_file():
        return Path(path).read_text()[:8000]
    return DEFAULT_CONTEXT


def _local(url):
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in {h.strip() for h in os.environ.get("VA_LOCAL_MODEL_HOSTS", "").split(",") if h.strip()}
    return address.is_loopback or address.is_private or address in ipaddress.ip_network("100.64.0.0/10")


def _chat(messages, opener=urlopen, timeout=600):
    if not _local(LLM_URL):
        raise RuntimeError("VA_LLM_URL must be a local address; lessons never go to a hosted model")
    body = {"model": LLM_MODEL, "messages": messages, "temperature": 0.2, "max_tokens": 4000, "stream": False, "response_format": {"type": "json_object"}}
    if not LLM_THINK:
        body["reasoning_effort"] = "none"
    request = Request(LLM_URL + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
    with opener(request, timeout=timeout) as response:
        data = json.loads(response.read())
    return data["choices"][0]["message"]["content"]


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    break
        start = text.find("{", start + 1)
    raise ValueError("no JSON in the model answer")


def _strings(value, count, limit):
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str):
            item = " ".join(item.split())[:limit]
            if item and item not in out:
                out.append(item)
    return out[:count]


def normalize(raw):
    rec = raw.get("recommendation") if isinstance(raw.get("recommendation"), dict) else {}
    score = rec.get("value_score")
    try:
        score = max(0, min(10, int(round(float(score))))) if score is not None and not isinstance(score, bool) else None
    except (TypeError, ValueError):
        score = None
    verdict = str(rec.get("verdict", "")).lower().strip()
    effort = str(rec.get("effort", "")).upper().strip()[:1]
    category = raw.get("category") if raw.get("category") in CATEGORIES else "Other"
    name = " ".join(str(raw.get("lesson_name", "")).split()).strip("\"'. ")[:120]
    return {
        "lesson_name": name,
        "summary": str(raw.get("summary", "")).strip()[:4000],
        "key_points": _strings(raw.get("key_points"), 8, 300),
        "tools": _strings(raw.get("tools"), 15, 80),
        "category": category,
        "recommendation": {
            "verdict": verdict if verdict in VERDICTS else "watch",
            "value_score": score,
            "production_fit": str(rec.get("production_fit", "")).strip()[:1500],
            "rationale": str(rec.get("rationale", "")).strip()[:2000],
            "effort": effort if effort in ("S", "M", "L") else "",
            "implementation_steps": _strings(rec.get("implementation_steps"), 6, 400),
        },
    }


def analyze(transcript, source, thumb_paths=(), opener=urlopen):
    """Never raises: a down model leaves {"ok": False, "error": ...} and the lesson is still saved."""
    started = time.time()
    lines = [f"# Our production flow\n{production_context()}", "", "# The video"]
    for label, key in (("Platform", "platform"), ("Creator", "author"), ("Caption", "title"), ("Description", "description")):
        if source.get(key):
            lines.append(f"{label}: {source[key]}")
    if source.get("tags"):
        lines.append("Hashtags: " + ", ".join(source["tags"]))
    lines += ["", "## Transcript", (transcript or "")[:24000]]
    text = "\n".join(lines)
    images = []
    if LLM_VISION:
        for path in list(thumb_paths)[:3]:
            try:
                images.append("data:image/jpeg;base64," + base64.b64encode(Path(path).read_bytes()).decode())
            except OSError:
                pass
    attempts = []
    if images:
        attempts.append([{"type": "text", "text": text + "\n\nThe attached pictures are frames from the video (on-screen text and UI often carry the lesson)."}]
                        + [{"type": "image_url", "image_url": {"url": u}} for u in images])
    attempts.append(text)
    error = ""
    for content in attempts:
        for _ in range(2):   # one retry for malformed JSON
            try:
                answer = _chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}], opener=opener)
                result = normalize(parse_json(answer))
                if result["lesson_name"]:
                    result.update(ok=True, model=LLM_MODEL, seconds=round(time.time() - started, 1), used_images=content is not text)
                    return result
                error = "the model gave no lesson name"
            except Exception as e:   # unreachable model, refused images, bad JSON
                error = f"{type(e).__name__}: {e}"[:300]
                if "HTTP Error 4" in error:
                    break   # a 4xx (e.g. images unsupported) will not change on retry
    return {"ok": False, "error": error, "model": LLM_MODEL}


# ---- export to BadMonkey --------------------------------------------------------------------------
def export_configured():
    return len(BADMONKEY_KEY) >= 32 and _local(BADMONKEY_URL)


def payload(meta, out_dir):
    analysis = meta.get("analysis") or {}
    source = meta.get("source") or {}
    thumbs = []
    for name in meta.get("thumbnails", [])[:6]:
        path = Path(out_dir) / "thumbs" / name
        if path.is_file() and path.stat().st_size < 400_000:
            thumbs.append(base64.b64encode(path.read_bytes()).decode())
    body_analysis = {k: v for k, v in analysis.items() if k in ("lesson_name", "summary", "key_points", "tools", "category", "recommendation")}
    if not body_analysis.get("recommendation"):
        body_analysis["recommendation"] = {"verdict": "unrated"}
    return {
        "source_key": source.get("key") or f"upload:{Path(out_dir).name}",
        "source_url": source.get("url", "") if str(source.get("url", "")).startswith("https://") else "",
        "platform": source.get("platform", "upload"),
        "author": source.get("author", "")[:160],
        "title": (meta.get("summary") or source.get("title") or meta.get("original_name") or "")[:120],
        "transcript": (meta.get("transcript") or "")[:150000],
        "duration": float(meta.get("duration") or 0),
        "analysis_model": analysis.get("model", "") if analysis.get("ok") else "",
        "thumbnails": thumbs,
        "analysis": body_analysis,
    }


def export(meta, out_dir, opener=urlopen):
    if not export_configured():
        return {"ok": False, "error": "Set VA_BADMONKEY_KEY (the workspace's BM_RESEARCH_KEY) to send lessons to BadMonkey R&D."}
    request = Request(BADMONKEY_URL + "/api/v1/research/machine/ingest", data=json.dumps(payload(meta, out_dir)).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-BM-Research-Key": BADMONKEY_KEY})
    try:
        with opener(request, timeout=60) as response:
            result = json.loads(response.read())
        return {"ok": True, "module_id": result.get("id"), "at": time.strftime("%Y-%m-%d %H:%M")}
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = json.loads(e.read()).get("error", {}).get("message", "")
            except Exception:
                pass
        return {"ok": False, "error": (detail or f"{type(e).__name__}: {e}")[:300], "at": time.strftime("%Y-%m-%d %H:%M")}


# ---- phone inbox and watched folder ---------------------------------------------------------------
def _machine(path, body, opener=urlopen):
    request = Request(BADMONKEY_URL + "/api/v1/research/machine/" + path, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-BM-Research-Key": BADMONKEY_KEY})
    with opener(request, timeout=30) as response:
        return json.loads(response.read())


def inbox_next(opener=urlopen):
    """Claim the oldest phone link and return its item plus fencing token."""
    return _machine("inbox/next", {}, opener)


def inbox_report(item_id, token, status, module_id="", error="", job="", opener=urlopen):
    try:
        _machine("inbox/" + item_id, {"token": token, "status": status, "module_id": module_id or "", "error": (error or "")[:500], "job": job}, opener)
        return True
    except HTTPError as exception:
        if exception.code == 409:
            logger.warning("Inbox report rejected with HTTP 409 for %s; abandoning the item", item_id)
            raise InboxLeaseLost(item_id) from exception
        logger.warning("Inbox report failed for %s: HTTPError %s", item_id, exception.code)
    except Exception as exception:
        logger.warning("Inbox report failed for %s: %s: %s", item_id, type(exception).__name__, exception)
    return False


def watch_dir():
    """VA_WATCH_DIR, or iCloud Drive/BadMonkey Inbox when this Mac uses iCloud Drive. VA_WATCH_DIR=off disables it."""
    configured = os.environ.get("VA_WATCH_DIR", "")
    if configured.lower() == "off":
        return None
    folder = Path(configured).expanduser() if configured else None
    if not folder:
        icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        if not icloud.is_dir():
            return None
        folder = icloud / "BadMonkey Inbox"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def ready_files(folder, seen):
    """Video files that finished syncing: same size on two scans, untouched for 10 s, not an iCloud placeholder."""
    ready = []
    now = time.time()
    for file in sorted(Path(folder).iterdir()):
        if not file.is_file() or file.suffix.lower() not in VIDEO_EXTENSIONS or file.name.startswith("."):
            continue
        stat = file.stat()
        if seen.get(file) == stat.st_size and now - stat.st_mtime > 10 and stat.st_size > 0:
            ready.append((file, stat.st_size))
            seen.pop(file, None)
        else:
            seen[file] = stat.st_size
    return ready


def file_done(file):
    """Move a picked-up file to <folder>/Done so it is never processed twice (and stays yours in iCloud)."""
    done = Path(file).parent / "Done"
    done.mkdir(exist_ok=True)
    target = done / Path(file).name
    if target.exists():
        target = done / f"{Path(file).stem}-{int(time.time())}{Path(file).suffix}"
    Path(file).rename(target)
