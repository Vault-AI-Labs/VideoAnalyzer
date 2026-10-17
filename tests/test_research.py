"""TikTok links, the TikTok data export, thumbnails, local-model analysis and export to BadMonkey.

Run: uv run --with flask python -m unittest discover -s tests
The pipeline test uses the real ffmpeg (when installed) and a stand-in Whisper; no model or network is used.
"""
import base64
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

TEMP = tempfile.TemporaryDirectory()
os.environ.update(VA_BACKGROUND="0", VA_DATA_DIR=TEMP.name, VA_BADMONKEY_URL="http://127.0.0.1:4181", VA_BADMONKEY_KEY="k" * 40, VA_LLM_URL="http://127.0.0.1:11434/v1", VA_LLM_MODEL="qwen-test")
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


class Terminology(unittest.TestCase):
    def test_badmonkey_module_is_user_facing_rd(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text()
        template = (root / "templates" / "index.html").read_text()
        app_source = (root / "app.py").read_text()
        research_source = (root / "research.py").read_text()

        self.assertIn("BadMonkey R&D", readme)
        self.assertNotIn("BadMonkey Research", readme)
        self.assertNotIn('"Send to Research"', readme)
        self.assertIn("Send to BadMonkey R&D", template)
        self.assertIn("Saved to BadMonkey R&D", template)
        self.assertNotIn("BadMonkey Research", template)
        self.assertIn("Saving to BadMonkey R&D...", app_source)
        self.assertNotIn("saved to Research", app_source)
        self.assertIn("send lessons to BadMonkey R&D", research_source)


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


class DownloadSecurity(unittest.TestCase):
    @staticmethod
    def _successful_run(folder, commands):
        def run(command, **kwargs):
            commands.append(command)
            video = Path(folder) / "deadbeef.mp4"
            video.write_bytes(b"video")
            return subprocess.CompletedProcess(command, 0, str(video) + "\n", "")
        return run

    def test_download_uses_loopback_proxy_and_300m_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            commands = []
            research.download(
                "yt-dlp",
                "https://www.tiktok.com/@_/video/7350000000000000001",
                folder,
                "deadbeef",
                run=self._successful_run(folder, commands),
            )

        command = commands[0]
        self.assertIn("--no-playlist", command)
        self.assertEqual(command[command.index("--max-filesize") + 1], "300M")
        proxy = command[command.index("--proxy") + 1]
        self.assertRegex(proxy, r"^http://127\.0\.0\.1:\d+$")

    def test_download_sandbox_allows_only_its_proxy_port(self):
        with tempfile.TemporaryDirectory() as folder:
            commands = []
            with mock.patch.object(research.shutil, "which", return_value="/usr/bin/sandbox-exec"):
                research.download(
                    "yt-dlp",
                    "https://www.tiktok.com/@_/video/7350000000000000001",
                    folder,
                    "deadbeef",
                    run=self._successful_run(folder, commands),
                )

        command = commands[0]
        self.assertEqual(command[:2], ["/usr/bin/sandbox-exec", "-p"])
        profile = command[2]
        proxy = command[command.index("--proxy") + 1]
        port = proxy.rsplit(":", 1)[1]
        self.assertIn("(deny network-outbound)", profile)
        self.assertIn(f'(allow network-outbound (remote ip "localhost:{port}"))', profile)

    def test_download_logs_when_sandbox_exec_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            commands = []
            with mock.patch.object(research.shutil, "which", return_value=None), \
                 self.assertLogs(research.logger, level="WARNING") as logs:
                research.download(
                    "yt-dlp",
                    "https://www.tiktok.com/@_/video/7350000000000000001",
                    folder,
                    "deadbeef",
                    run=self._successful_run(folder, commands),
                )

        self.assertEqual(commands[0][0], "yt-dlp")
        self.assertIn("sandbox-exec is unavailable", "\n".join(logs.output))


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
            self.assertEqual(url, "http://127.0.0.1:4181/api/v1/research/machine/ingest")
            self.assertEqual(key, "k" * 40)
            self.assertEqual(body["source_key"], "tiktok:735")
            self.assertEqual(base64.b64decode(body["thumbnails"][0]), b"\xff\xd8abc")
            self.assertEqual(body["analysis"]["recommendation"]["value_score"], 8)
            self.assertEqual(body["analysis_model"], "qwen-test")
            failed = research.export(meta, folder, opener=lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError("down")))
            self.assertFalse(failed["ok"])


class Routes(unittest.TestCase):
    def setUp(self):
        app.app.config["SERVER_NAME"] = "localhost:7745"
        self.client = app.app.test_client()
        self.client.environ_base["HTTP_ORIGIN"] = "http://localhost:7745"

    def test_host_header_is_restricted_to_loopback_with_a_port(self):
        for host in ("localhost:7745", "127.0.0.1:7745"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/health", headers={"Host": host}).status_code, 200)
        for host in ("evil.example:7745", "localhost", "127.0.0.1", "localhost:99999"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/health", headers={"Host": host}).status_code, 400)

    def test_mutations_require_same_origin_or_local_token_without_origin(self):
        url = "https://www.tiktok.com/@a/video/7350000000000000021"
        with mock.patch.object(app, "_enqueue_url", return_value="deadbeef"):
            no_credentials = self.client.post(
                "/api/ingest-url",
                json={"url": url},
                environ_overrides={"HTTP_ORIGIN": ""},
            )
            wrong_origin = self.client.post(
                "/api/ingest-url",
                json={"url": url},
                headers={"Origin": "http://evil.example", "X-VideoAnalyzer-Token": app.LOCAL_API_TOKEN},
            )
            same_origin = self.client.post("/api/ingest-url", json={"url": url})
            token = self.client.post(
                "/api/ingest-url",
                json={"url": url},
                headers={"X-VideoAnalyzer-Token": app.LOCAL_API_TOKEN},
                environ_overrides={"HTTP_ORIGIN": ""},
            )

        self.assertEqual(no_credentials.status_code, 403)
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(same_origin.status_code, 200)
        self.assertEqual(token.status_code, 200)

    def test_local_api_token_is_private(self):
        token_file = app.DATA_DIR / "token"
        self.assertEqual(token_file.read_text().strip(), app.LOCAL_API_TOKEN)
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)

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

        unsafe = app.OUTPUTS / "invalid"
        (unsafe / "frames").mkdir(parents=True, exist_ok=True)
        (unsafe / "frames" / "frame.jpg").write_bytes(b"not a real jpeg")
        (unsafe / "metadata.json").write_text(json.dumps({"item_status": "new"}))
        self.assertEqual(self.client.get("/api/frames/invalid/frame.jpg").status_code, 404)
        self.assertEqual(self.client.get("/api/stream/invalid").status_code, 404)
        self.assertEqual(self.client.put("/api/status/invalid", json={"status": "doing"}).status_code, 404)

    def test_delete_only_removes_an_immediate_real_directory_child(self):
        real = app.OUTPUTS / "deadbeef"
        real.mkdir(exist_ok=True)
        (real / "metadata.json").write_text("{}")
        response = self.client.put("/api/status/deadbeef", json={"status": "delete"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(real.exists())

        with tempfile.TemporaryDirectory() as outside:
            canary = Path(outside) / "canary.txt"
            canary.write_text("keep")
            symlink = app.OUTPUTS / "feedface"
            symlink.symlink_to(outside, target_is_directory=True)
            response = self.client.put("/api/status/feedface", json={"status": "delete"})
            self.assertEqual(response.status_code, 404)
            self.assertTrue(symlink.is_symlink())
            self.assertEqual(canary.read_text(), "keep")

        plain_file = app.OUTPUTS / "cafebabe"
        plain_file.write_text("keep")
        response = self.client.put("/api/status/cafebabe", json={"status": "delete"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(plain_file.read_text(), "keep")


class Phone(unittest.TestCase):
    def test_claim_and_reports_carry_the_fencing_token(self):
        sent = []
        def opener(request, timeout=None):
            sent.append((request.full_url, request.get_header("X-bm-research-key"), json.loads(request.data)))
            return Reply({"item": {"id": "a" * 40, "url": "https://vm.tiktok.com/ZMabc/"}, "token": "lease-token"})
        claim = research.inbox_next(opener=opener)
        self.assertEqual(claim["item"]["url"], "https://vm.tiktok.com/ZMabc/")
        self.assertEqual(claim["token"], "lease-token")
        research.inbox_report("a" * 40, "lease-token", "done", module_id="m" * 40, job="abcd1234", opener=opener)
        self.assertEqual(sent[0][0], "http://127.0.0.1:4181/api/v1/research/machine/inbox/next")
        self.assertEqual(sent[1][0], "http://127.0.0.1:4181/api/v1/research/machine/inbox/" + "a" * 40)
        self.assertEqual(sent[1][2]["token"], "lease-token")
        self.assertEqual(sent[1][2]["module_id"], "m" * 40)
        self.assertEqual(sent[0][1], "k" * 40)

    def test_report_failures_are_logged_and_409_means_lease_lost(self):
        from urllib.error import HTTPError

        def conflict(request, timeout=None):
            raise HTTPError(request.full_url, 409, "conflict", {}, io.BytesIO(b'{}'))

        with self.assertLogs(research.logger, level="WARNING") as conflict_logs, \
             self.assertRaises(research.InboxLeaseLost):
            research.inbox_report("a" * 40, "lease-token", "processing", opener=conflict)
        self.assertIn("409", "\n".join(conflict_logs.output))

        with self.assertLogs(research.logger, level="WARNING") as failure_logs:
            result = research.inbox_report(
                "a" * 40,
                "lease-token",
                "failed",
                opener=lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError("down")),
            )
        self.assertFalse(result)
        self.assertIn("ConnectionRefusedError", "\n".join(failure_logs.output))

    def test_watched_folder_waits_for_sync_and_ignores_placeholders(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "screen recording.mov"
            clip.write_bytes(b"x" * 100)
            (Path(folder) / ".other.mov.icloud").write_bytes(b"p")
            (Path(folder) / "notes.txt").write_text("no")
            seen = {}
            self.assertEqual(research.ready_files(folder, seen), [])          # first sight
            self.assertEqual(research.ready_files(folder, seen), [])          # same size but just modified
            old = time.time() - 30
            os.utime(clip, (old, old))
            self.assertEqual(research.ready_files(folder, seen), [(clip, 100)])
            research.file_done(clip)
            self.assertFalse(clip.exists())
            self.assertTrue((Path(folder) / "Done" / "screen recording.mov").exists())
        with mock.patch.dict(os.environ, {"VA_WATCH_DIR": "off"}):
            self.assertIsNone(research.watch_dir())

    def test_worker_reports_phone_links_and_files_back_to_the_inbox(self):
        reports = []
        def fake_process(job_id, video_path, name, model, source=None, export=True, cancel_event=None):
            out = app.OUTPUTS / job_id
            out.mkdir(parents=True, exist_ok=True)
            (out / "metadata.json").write_text(json.dumps({"export": {"ok": True, "module_id": "m" * 40}, "source": source}))
            Path(video_path).unlink()
        def fail_download(*a, **k):
            raise RuntimeError("Download failed: This video is private")
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "clip.mp4"
            clip.write_bytes(b"video")
            with mock.patch.object(app, "process_video", fake_process), mock.patch.object(research, "download", fail_download), \
                 mock.patch.object(research, "inbox_report", lambda *a, **k: reports.append((a, k)) or True):
                app._enqueue_url("", "tiny", True, inbox_id="f" * 40, inbox_token="file-token", file=clip)
                app._enqueue_url("https://www.tiktok.com/@_/video/7350000000000000077", "tiny", True, inbox_id="e" * 40, inbox_token="url-token")
                app.url_queue.join()
            self.assertTrue((Path(folder) / "Done" / "clip.mp4").exists())
        self.assertEqual(reports[0][0][:3], ("f" * 40, "file-token", "done"))
        self.assertEqual(reports[0][1]["module_id"], "m" * 40)
        self.assertEqual(reports[1][0][:3], ("e" * 40, "url-token", "failed"))
        self.assertIn("private", reports[1][1]["error"])

    def test_heartbeat_reports_processing_every_two_minutes(self):
        stop = mock.Mock()
        stop.wait.side_effect = [False, True]
        lease_lost = threading.Event()
        reports = []
        with mock.patch.object(research, "inbox_report", lambda *a, **k: reports.append((a, k)) or True):
            app._inbox_heartbeat("a" * 40, "lease-token", "deadbeef", stop, lease_lost)
        stop.wait.assert_has_calls([mock.call(120), mock.call(120)])
        self.assertEqual(reports[0][0], ("a" * 40, "lease-token", "processing"))
        self.assertEqual(reports[0][1]["job"], "deadbeef")
        self.assertFalse(lease_lost.is_set())

    def test_worker_abandons_an_item_after_heartbeat_409(self):
        reports = []

        def report(item_id, token, status, **kwargs):
            reports.append(status)
            if status == "processing":
                raise research.InboxLeaseLost("lease lost")
            return True

        def slow_process(*args, **kwargs):
            time.sleep(0.05)

        with mock.patch.object(app, "INBOX_HEARTBEAT_SECONDS", 0.01), \
             mock.patch.object(research, "inbox_report", report), \
             mock.patch.object(app, "process_video", slow_process), \
             mock.patch.object(research, "download", return_value=(Path(TEMP.name) / "video.mp4", {})):
            app._enqueue_url(
                "https://www.tiktok.com/@_/video/7350000000000000077",
                "tiny",
                True,
                inbox_id="a" * 40,
                inbox_token="lease-token",
            )
            app.url_queue.join()

        self.assertEqual(reports, ["processing"])

    def test_worker_does_not_start_a_claim_whose_lease_is_already_lost(self):
        job_id = "baddcafe"
        app.jobs[job_id] = __import__("queue").Queue()
        with app.waiting_lock:
            app.waiting.append(job_id)
        stop = mock.Mock()
        lease_lost = threading.Event()
        lease_lost.set()
        heartbeat = mock.Mock()
        lease = (stop, lease_lost, heartbeat)

        with mock.patch.object(research, "download") as download, \
             mock.patch.object(app, "process_video") as process:
            app.url_queue.put((
                job_id,
                "https://www.tiktok.com/@_/video/7350000000000000077",
                "tiny",
                True,
                "a" * 40,
                "lease-token",
                lease,
                None,
            ))
            app.url_queue.join()

        download.assert_not_called()
        process.assert_not_called()
        stop.set.assert_called_once()
        heartbeat.join.assert_called_once()

    def test_active_lease_loss_stops_before_export(self):
        job_id = "c0ffee00"
        app.jobs[job_id] = __import__("queue").Queue()
        cancel = threading.Event()
        video = app.UPLOADS / f"{job_id}.mp4"
        video.write_bytes(b"video")

        commands = [
            SimpleNamespace(stdout="1\n", returncode=0),
            SimpleNamespace(returncode=1),
        ]

        def lose_lease(*args, **kwargs):
            cancel.set()
            return dict(research.normalize(GOOD), ok=True)

        with mock.patch.object(app.subprocess, "run", side_effect=commands), \
             mock.patch.object(app, "_run_with_heartbeat", return_value=(0, b"", b"")), \
             mock.patch.object(research, "thumbnails", return_value=[]), \
             mock.patch.object(research, "analyze", side_effect=lose_lease), \
             mock.patch.object(research, "export") as export:
            app.process_video(
                job_id,
                video,
                "video.mp4",
                "tiny",
                source={"platform": "tiktok", "id": "735"},
                export=True,
                cancel_event=cancel,
            )

        export.assert_not_called()
        statuses = []
        while not app.jobs[job_id].empty():
            statuses.append(app.jobs[job_id].get_nowait()["status"])
        self.assertEqual(statuses[-1], "error")
        self.assertNotIn("complete", statuses)

    def test_inbox_finalization_errors_are_contained(self):
        with mock.patch.object(app, "_outcome", side_effect=ValueError("bad metadata")), \
             self.assertLogs(app.app.logger, level="WARNING") as logs:
            reported = app._report_inbox_outcome(
                "deadbeef", "a" * 40, "lease-token", ""
            )
        self.assertFalse(reported)
        self.assertIn("bad metadata", "\n".join(logs.output))

    def test_missing_inbox_token_does_not_leave_a_zombie_job(self):
        before_jobs = set(app.jobs)
        with app.waiting_lock:
            before_waiting = list(app.waiting)
        with self.assertRaises(ValueError):
            app._enqueue_url("https://example.test/video", "tiny", inbox_id="a" * 40)
        self.assertEqual(set(app.jobs), before_jobs)
        with app.waiting_lock:
            self.assertEqual(app.waiting, before_waiting)


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
        res = app.app.test_client().get(f"/api/thumbs/{job}/thumb_2.jpg", base_url="http://localhost:7745")
        self.assertEqual(res.status_code, 200)
        res.close()


if __name__ == "__main__":
    unittest.main()


class TranscribeTimeout(unittest.TestCase):
    def test_long_videos_get_time_in_proportion(self):
        self.assertEqual(app.transcribe_timeout(0), 600)
        self.assertEqual(app.transcribe_timeout(60), 600)
        self.assertEqual(app.transcribe_timeout(1129), 3387)   # the 19-minute TikTok that hit the old 10-minute cap

    def test_a_timed_out_transcription_says_so(self):
        with mock.patch.object(app, "_run_with_heartbeat", return_value=(-9, b"", b"")) as run:
            app.jobs["abcdef12"] = __import__("queue").Queue()
            source = Path(TEMP.name) / "clip.mp4"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2", "-f", "lavfi", "-i", "color=size=64x64:duration=2", "-shortest", str(source)], check=True)
            app.process_video("abcdef12", source, "clip.mp4", "base", export=False)
            messages = []
            while not app.jobs["abcdef12"].empty():
                messages.append(app.jobs["abcdef12"].get()["message"])
            self.assertTrue(any("Transcription stopped after 10 minutes" in m for m in messages), messages)
            self.assertEqual(run.call_args.kwargs["timeout"], 600)


class ThinkingOff(unittest.TestCase):
    def test_analysis_asks_the_model_not_to_think(self):
        seen = {}
        def opener(request, timeout=None):
            seen.update(json.loads(request.data))
            return Reply({"choices": [{"message": {"content": "{}"}}]})
        research._chat([{"role": "user", "content": "x"}], opener=opener)
        self.assertEqual(seen.get("reasoning_effort"), "none")
        self.assertEqual(seen.get("max_tokens"), 4000)

    def test_fast_whisper_is_used_only_when_its_model_file_and_cli_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "whisper-cli"; cli.write_text("#!/bin/sh\n"); cli.chmod(0o755)
            models = Path(folder) / "models"; models.mkdir()
            with mock.patch.object(research, "WHISPER_CPP_MODELS", models), mock.patch.dict(os.environ, {"PATH": folder}):
                self.assertIsNone(research.fast_whisper("small"))   # no model file: the CPU Whisper is used
                (models / "ggml-small.bin").write_bytes(b"x")
                self.assertEqual(research.fast_whisper("small"), (str(cli), models / "ggml-small.bin"))
                self.assertIsNone(research.fast_whisper("base"))

    def test_roundup_videos_cover_every_item(self):
        # A three-release news roundup was saved as a lesson about only one of them (2026-10-17).
        for words in ("news roundup", "covers ALL of them", "at least one point per item", "tools lists every one",
                      "Speech-to-text often misspells names"):
            self.assertIn(words, research.SYSTEM)
        sent = []
        def opener(request, timeout=None):
            sent.append(json.loads(request.data))
            return Reply({"choices": [{"message": {"content": json.dumps(GOOD)}}]})
        research.analyze("Three releases today: mu spark, Neymotron 3.5 and bonds.", {"title": "Muse Spark, Nemotron 3.5, Bonsai"}, opener=opener)
        self.assertIn("covers ALL of them", sent[0]["messages"][0]["content"])
