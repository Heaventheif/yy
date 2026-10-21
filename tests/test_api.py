import unittest
from unittest.mock import patch

import app as application
from socialdl.errors import ApiError


MEDIA = {
    "title": "Demo video",
    "ext": "mp4",
    "url": "https://cdn.example.com/media.mp4",
    "headers": {},
    "size": 1024,
    "duration": 3,
    "thumbnail": None,
    "site": "Facebook",
}


class ApiTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()

    def test_health_marks_youtube_disabled_and_omits_x(self):
        response = self.client.get("/api/v1/health")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["youtube"], "disabled")
        self.assertNotIn("X", body["platforms"])

    @patch("app._rate_check")
    @patch("app.resolve_media", return_value=MEDIA)
    @patch("app.clean_url", return_value="https://www.facebook.com/watch/?v=1")
    def test_resolve_returns_service_download_route_not_raw_cdn(self, _clean, _resolve, _rate):
        response = self.client.get("/api/v1/resolve?url=https://www.facebook.com/watch/?v=1")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("/api/v1/download?url=", body["download"])
        self.assertNotIn("source", body)
        self.assertNotIn("cdn.example.com", response.get_data(as_text=True))

    @patch("app._rate_check")
    @patch("app.resolve_media", return_value=MEDIA)
    @patch("app.clean_url", side_effect=lambda value: value)
    def test_batch_returns_results_in_input_order(self, _clean, _resolve, _rate):
        response = self.client.post("/api/v1/batch", json={"urls": ["first", "second"]})
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["input"] for item in body["results"]], ["first", "second"])
        self.assertTrue(all(item["download"].startswith("http://localhost") for item in body["results"]))

    @patch("app._rate_check")
    @patch("app.resolve_media", side_effect=ApiError("source_payment_required", "source returned 402", 502,
                                                       upstream_status=402))
    @patch("app.clean_url", return_value="https://www.facebook.com/watch/?v=1")
    def test_resolve_keeps_upstream_402_diagnostic(self, _clean, _resolve, _rate):
        response = self.client.get("/api/v1/resolve?url=https://www.facebook.com/watch/?v=1")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"]["upstream_status"], 402)

    @patch("app._rate_check")
    @patch("app._open_upstream")
    @patch("app.resolve_media", return_value=MEDIA)
    @patch("app.clean_url", return_value="https://www.facebook.com/watch/?v=1")
    def test_head_download_passes_content_headers(self, _clean, _resolve, open_upstream, _rate):
        class FakeUpstream:
            status_code = 200
            headers = {"Content-Type": "video/mp4", "Content-Length": "1024", "Accept-Ranges": "bytes"}
            def close(self):
                pass
        class FakeSession:
            def close(self):
                pass
        open_upstream.return_value = (FakeUpstream(), FakeSession())
        response = self.client.head("/api/v1/download?url=https://www.facebook.com/watch/?v=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Length"], "1024")
        self.assertIn("attachment", response.headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()


class AudioConversionTests(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.m = app_module
        self.client = app_module.app.test_client()

    def _media(self, **extra):
        base = {"title": "اختبار", "ext": "mp4", "url": "https://cdn.example.com/v.mp4", "headers": {},
                "size": 1, "duration": 3, "site": "TikTok", "thumbnail": None,
                "has_audio": True, "audio_url": None, "audio_headers": None}
        base.update(extra)
        return base

    def test_resolve_exposes_audio_links_only_when_audio_exists(self):
        from unittest.mock import patch
        with patch.object(self.m, "clean_url", lambda u: u), patch.object(self.m, "resolve_media", lambda u, fresh=False: self._media()):
            data = self.client.get("/api/v1/resolve?url=https://example.com/v").json
        self.assertIn("/api/v1/audio?url=", data["audio"]["mp3"])
        self.assertTrue(data["audio"]["m4a"].endswith("format=m4a"))
        with patch.object(self.m, "clean_url", lambda u: u), patch.object(self.m, "resolve_media", lambda u, fresh=False: self._media(has_audio=False, ext="jpg")):
            self.assertIsNone(self.client.get("/api/v1/resolve?url=https://example.com/p").json["audio"])

    def test_converts_to_mp3_and_m4a(self):
        import subprocess, tempfile, os
        from unittest.mock import patch
        ffmpeg = self.m._ffmpeg_exe()
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        tmp = tempfile.mkdtemp()
        sample = os.path.join(tmp, "s.mp4")
        subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-shortest", sample], check=True)
        data = open(sample, "rb").read()

        class Up:
            status_code = 200
            headers = {"Content-Length": str(len(data))}
            def iter_content(self, n): yield data
            def close(self): pass

        class Sess:
            def close(self): pass

        for fmt, magic in (("mp3", (b"ID3", b"\xff\xfb", b"\xff\xf3")), ("m4a", (b"ftyp",))):
            with patch.object(self.m, "clean_url", lambda u: u), \
                 patch.object(self.m, "resolve_media", lambda u, fresh=False: self._media()), \
                 patch.object(self.m, "_open_upstream", lambda *a, **k: (Up(), Sess())):
                r = self.client.get(f"/api/v1/audio?url=https://example.com/v&format={fmt}")
                body = r.data
                r.close()
            self.assertEqual(r.status_code, 200, body[:200])
            self.assertTrue(any(body.startswith(m) or body[4:8] == m for m in magic), body[:12])
        self.assertEqual(self.client.get("/api/v1/audio?url=https://example.com/v&format=wav").status_code, 400)
