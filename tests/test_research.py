"""TikTok links, the TikTok data export, thumbnails, local-model analysis and export to BadMonkey.

Run: uv run --with flask python -m unittest discover -s tests
The pipeline test uses the real ffmpeg (when installed) and a stand-in Whisper; no model or network is used.
"""
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TEMP = tempfile.TemporaryDirectory()
os.environ.update(VA_DATA_DIR=TEMP.name, VA_BADMONKEY_URL="http://127.0.0.1:4180", VA_BADMONKEY_KEY="k" * 40, VA_LLM_URL="http://127.0.0.1:11434/v1", VA_LLM_MODEL="qwen-test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research  # noqa: E402
import app  # noqa: E402


class Reply:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
    def read(self):
        return self.body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


GOOD = {"lesson_name": "Rough-cut interviews with local Whisper", "summary": "Transcribe, then let a model pick takes.", "key_points": ["a", "a", "b"],
        "tools": ["Whisper"], "category": "Video & motion", "recommendation": {"verdict": "Trial", "value_score": "8", "production_fit": "Edit stage",
        "rationale": "Saves hours", "effort": "medium", "implementation_steps": ["Try on one film"]}}


class Links(unittest.TestCase):
    def test_normalize_and_reject(self):
        self.assertEqual(research.normalize_url("https://www.tiktok.com/@maker/video/7350000000000000001?is_from_webapp=1&sender=x"), "https://www.tiktok.com/@_/video/7350000000000000001")
        self.assertEqual(research.normalize_url("http://vm.tiktok.com/ZMabc123/"), "https://vm.tiktok.com/ZMabc123/")
        self.assertEqual(research.normalize_url("https://www.tiktokv.com/share/video/7350000000000000002/"), "https://www.tiktok.com/@_/video/7350000000000000002")
        for bad in ("file:///etc/passwd", "https://evil.example/video/123456789", "https://user:pw@www.tiktok.com/@a/video/123456789", "https://www.tiktok.com:8443/@a/video/123456789", "javascript:alert(1)"):
            with self.assertRaises(ValueError, msg=bad):
                research.normalize_url(bad)
        self.assertEqual(research.source_key_for_url("https://www.tiktok.com/@_/video/7350000000000000001"), "tiktok:7350000000000000001")
        self.assertIsNone(research.source_key_for_url("https://vm.tiktok.com/ZMabc123/"))

    def test_tiktok_data_export_favorites_and_likes(self):
        export = {"Activity": {"Favorite Videos": {"FavoriteVideoList": [{"Date": "2026-10-01", "Link": "https://www.tiktokv.com/share/video/7350000000000000011/"},
                                                                           {"Date": "2026-10-02", "Link": "https://www.tiktokv.com/share/video/7350000000000000011/"}]},
                              "Like List": {"ItemFavoriteList": [{"date": "x", "link": "https://www.tiktokv.com/share/video/7350000000000000012/"}]},
                              "Video Browsing History": {"VideoList": [{"Link": "https://www.tiktokv.com/share/video/7350000000000000013/"}]}}}
        self.assertEqual(research.links_from_tiktok_export(export), ["https://www.tiktok.com/@_/video/7350000000000000011"])
        self.assertEqual(len(research.links_from_tiktok_export(export, ("favorite", "like"))), 2)


class Analysis(unittest.TestCase):
    def test_parses_reasoning_output_and_clamps_fields(self):
        answer = "<think>long reasoning {not json}</think>\n```json\n" + json.dumps(GOOD) + "\n```"
        opener = lambda request, timeout=None: Reply({"choices": [{"message": {"content": answer}}]})
        result = research.analyze("transcript", {"platform": "tiktok", "author": "@maker"}, [], opener=opener)
        self.assertTrue(result["ok"])
        self.assertEqual(result["recommendation"]["verdict"], "trial")
        self.assertEqual(result["recommendation"]["value_score"], 8)
        self.assertEqual(result["recommendation"]["effort"], "M")
        self.assertEqual(result["key_points"], ["a", "b"])

    def test_falls_back_to_text_when_images_are_refused_and_never_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            thumb = Path(folder) / "thumb_1.jpg"
            thumb.write_bytes(b"\xff\xd8jpeg")
            seen = []
            def opener(request, timeout=None):
                body = json.loads(request.data)
                seen.append(isinstance(body["messages"][1]["content"], list))
                if seen[-1]:
                    from urllib.error import HTTPError
                    raise HTTPError(request.full_url, 400, "images unsupported", {}, io.BytesIO(b""))
                return Reply({"choices": [{"message": {"content": json.dumps(GOOD)}}]})
            result = research.analyze("t", {}, [thumb], opener=opener)
            self.assertEqual(seen, [True, False])
            self.assertTrue(result["ok"])
            self.assertFalse(result["used_images"])
        def down(request, timeout=None):
            raise ConnectionRefusedError("ollama is not running")
        result = research.analyze("t", {}, [], opener=down)
        self.assertFalse(result["ok"])
        self.assertIn("ConnectionRefusedError", result["error"])

    def test_refuses_a_hosted_model_url(self):
        with mock.patch.object(research, "LLM_URL", "https://api.openai.com/v1"):
            result = research.analyze("t", {}, [], opener=lambda *a, **k: self.fail("must not call a hosted model"))
        self.assertFalse(result["ok"])
        self.assertIn("local", result["error"])


class Export(unittest.TestCase):
    def test_export_sends_the_lesson_with_thumbnails_and_key(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "thumbs").mkdir()
            (Path(folder) / "thumbs" / "thumb_1.jpg").write_bytes(b"\xff\xd8abc")
            meta = {"summary": "Lesson", "transcript": "hi", "duration": 12.5, "thumbnails": ["thumb_1.jpg"],
                    "source": {"key": "tiktok:735", "url": "https://www.tiktok.com/@_/video/735", "platform": "tiktok", "author": "@maker"},
                    "analysis": dict(research.normalize(GOOD), ok=True, model="qwen-test")}
            sent = []
            def opener(request, timeout=None):
                sent.append((request.full_url, request.get_header("X-bm-research-key"), json.loads(request.data)))
                return Reply({"id": "a" * 40, "created": True}, 201)
            result = research.export(meta, folder, opener=opener)
            self.assertTrue(result["ok"])
            url, key, body = sent[0]
            self.assertEqual(url, "http://127.0.0.1:4180/api/v1/research/machine/ingest")
            self.assertEqual(key, "k" * 40)
            self.assertEqual(body["source_key"], "tiktok:735")
            self.assertEqual(base64.b64decode(body["thumbnails"][0]), b"\xff\xd8abc")
            self.assertEqual(body["analysis"]["recommendation"]["value_score"], 8)
            self.assertEqual(body["analysis_model"], "qwen-test")
            failed = research.export(meta, folder, opener=lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError("down")))
            self.assertFalse(failed["ok"])


class Routes(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_health_reports_tools(self):
        data = self.client.get("/api/health").get_json()
        self.assertEqual(set(data["tools"]), {"ffmpeg", "ffprobe", "whisper", "yt-dlp"})
        self.assertTrue(data["badmonkey_export"])
        self.assertEqual(data["analysis_model"], "qwen-test")

    def test_ingest_url_queues_supported_links_and_skips_saved_ones(self):
        queued = []
        with mock.patch.object(app, "_enqueue_url", lambda url, model, export=True: queued.append(url) or "deadbeef"):
            res = self.client.post("/api/ingest-url", json={"url": "https://www.tiktok.com/@a/video/7350000000000000021 https://evil.example/x  https://www.tiktok.com/@b/video/7350000000000000021"})
            data = res.get_json()
            self.assertEqual(res.status_code, 200)
            self.assertEqual(queued, ["https://www.tiktok.com/@_/video/7350000000000000021"])
            self.assertEqual(len(data["rejected"]), 1)
            out = app.OUTPUTS / "aaaaaaaa"
            out.mkdir(parents=True, exist_ok=True)
            (out / "metadata.json").write_text(json.dumps({"source": {"key": "tiktok:7350000000000000021"}}))
            data = self.client.post("/api/ingest-url", json={"url": "https://www.tiktok.com/@a/video/7350000000000000021"}).get_json()
            self.assertEqual(data["skipped"], ["https://www.tiktok.com/@_/video/7350000000000000021"])
            self.assertEqual(self.client.post("/api/ingest-url", json={"url": "nothing here"}).status_code, 400)
            self.assertEqual(self.client.post("/api/ingest-url", json={"url": "https://www.tiktok.com/@a/video/7350000000000000099", "model": "../../x"}).status_code, 400)

    def test_import_tiktok_export_file(self):
        queued = []
        export = {"Activity": {"Favorite Videos": {"FavoriteVideoList": [{"Link": f"https://www.tiktokv.com/share/video/73500000000000001{i:02d}/"} for i in range(5)]}}}
        with mock.patch.object(app, "_enqueue_url", lambda url, model, export=True: queued.append(url) or "deadbeef"):
            res = self.client.post("/api/import-tiktok", data={"file": (io.BytesIO(json.dumps(export).encode()), "user_data.json"), "limit": "3"}, content_type="multipart/form-data")
        data = res.get_json()
        self.assertEqual((data["found"], len(data["queued"]), data["left_for_later"]), (5, 3, 2))
        res = self.client.post("/api/import-tiktok", data={"file": (io.BytesIO(b"not json"), "x.json")}, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)

    def test_ids_are_validated(self):
        self.assertEqual(self.client.post("/api/export/..%2f..").status_code, 404)
        self.assertEqual(self.client.get("/api/thumbs/aaaaaaaa/../metadata.json").status_code, 404)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class Pipeline(unittest.TestCase):
    def test_link_to_lesson_end_to_end(self):
        folder = Path(TEMP.name) / "pipeline"
        folder.mkdir(exist_ok=True)
        whisper = folder / "whisper"
        whisper.write_text('#!/bin/sh\nwhile [ "$1" != "--output_dir" ]; do shift; done\necho "Today: rough-cut interviews with a local model." > "$2/audio.txt"\n')
        whisper.chmod(0o755)
        clip = folder / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x568:rate=15", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "5", "-shortest", "-pix_fmt", "yuv420p", str(clip), "-y"], check=True)

        def fake_download(ytdlp, url, uploads, job_id):
            target = Path(uploads) / f"{job_id}.mp4"
            shutil.copy(clip, target)
            return target, {"platform": "tiktok", "url": url, "id": "7350000000000000031", "author": "@maker", "title": "rough cut trick"}

        sent = []
        def chat(messages, opener=None, timeout=None):
            return json.dumps(GOOD)
        def export(meta, out_dir, opener=None):
            sent.append(research.payload(meta, out_dir))
            return {"ok": True, "module_id": "b" * 40}
        with mock.patch.object(app, "WHISPER", str(whisper)), mock.patch.object(research, "download", fake_download), \
             mock.patch.object(research, "_chat", chat), mock.patch.object(research, "export", export):
            job = app._enqueue_url("https://www.tiktok.com/@_/video/7350000000000000031", "tiny")
            events = []
            deadline = time.time() + 120
            while time.time() < deadline:
                event = app.jobs[job].get(timeout=120)
                events.append(event["status"])
                if event["status"] in ("complete", "error"):
                    break
        self.assertEqual(events[-1], "complete", events)
        self.assertIn("downloading", events)
        self.assertIn("exporting", events)
        meta = json.loads((app.OUTPUTS / job / "metadata.json").read_text())
        self.assertEqual(meta["summary"], "Rough-cut interviews with local Whisper")
        self.assertEqual(len(meta["thumbnails"]), 3)
        self.assertEqual(meta["source"]["key"], "tiktok:7350000000000000031")
        self.assertTrue(meta["export"]["ok"])
        body = sent[0]
        self.assertEqual(len(body["thumbnails"]), 3)
        self.assertTrue(all(base64.b64decode(t).startswith(b"\xff\xd8") and len(base64.b64decode(t)) < 400_000 for t in body["thumbnails"]))
        self.assertIn("rough-cut", body["transcript"])
        res = app.app.test_client().get(f"/api/thumbs/{job}/thumb_2.jpg")
        self.assertEqual(res.status_code, 200)
        res.close()


if __name__ == "__main__":
    unittest.main()
