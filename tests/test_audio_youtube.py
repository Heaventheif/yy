import http.server
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import app as app_module
from socialdl.errors import ApiError


def _fmt(kind, quality, url="https://example.com/f", bitrate=None, size=1000):
    return {"id": f"x:{quality}", "provider": "x", "kind": kind, "quality": quality,
            "mime": "audio/mp4" if kind == "audio" else "video/mp4", "bitrate": bitrate, "size": size, "url": url}


class CandidateTests(unittest.TestCase):
    def test_audio_near_128k_comes_first_then_lowest_progressive(self):
        formats = [_fmt("progressive", "720p", size=9), _fmt("progressive", "360p", size=5),
                   _fmt("audio", "high", bitrate=256_000), _fmt("audio", "medium", bitrate=128_000),
                   _fmt("video", "1080p")]
        picked = app_module._youtube_audio_candidates(formats)
        self.assertEqual([f["quality"] for f in picked], ["medium", "high", "360p", "720p"])

    def test_video_only_formats_are_never_sources(self):
        self.assertEqual(app_module._youtube_audio_candidates([_fmt("video", "1080p")]), [])

    def test_at_most_four_sources(self):
        formats = [_fmt("progressive", f"{h}p") for h in (144, 240, 360, 480, 720)]
        self.assertEqual(len(app_module._youtube_audio_candidates(formats)), app_module.YT_AUDIO_MAX_SOURCES)


class AudioTargetTests(unittest.TestCase):
    def test_youtube_links_become_canonical_watch_urls(self):
        for raw in ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
                    "انظر https://m.youtube.com/watch?v=dQw4w9WgXcQ"):
            with self.subTest(raw=raw):
                self.assertEqual(app_module._audio_target(raw),
                                 ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"))

    @patch("socialdl.platforms.is_public_url", return_value=True)   # clean_url resolves DNS; keep tests offline
    def test_other_sites_go_through_clean_url(self, _public):
        url, video_id = app_module._audio_target("https://www.tiktok.com/@u/video/1?utm_source=x")
        self.assertEqual((url, video_id), ("https://www.tiktok.com/@u/video/1", None))


@unittest.skipUnless(shutil.which("ffmpeg") or app_module._ffmpeg_exe(), "ffmpeg not available")
class YoutubeAudioConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = tempfile.mktemp(suffix=".m4a")
        subprocess.run([app_module._ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=2", "-c:a", "aac", src], check=True)
        data = open(src, "rb").read()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.source_url = f"http://127.0.0.1:{cls.server.server_address[1]}/a"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _found(self):
        return {"title": "Test Song", "duration": 2, "formats": [_fmt("audio", "medium", self.source_url, 128_000)]}

    def test_youtube_link_is_converted_to_mp3_and_m4a_with_ffmpeg(self):
        client = app_module.app.test_client()
        with patch.object(app_module.yt, "lookup", return_value=self._found()), \
             patch.object(app_module, "is_public_url", lambda url: True):
            for fmt, mimetype in (("mp3", "audio/mpeg"), ("m4a", "audio/mp4")):
                with self.subTest(fmt=fmt):
                    response = client.get(f"/api/v1/audio?url=https://youtu.be/dQw4w9WgXcQ&format={fmt}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.mimetype, mimetype)
                    self.assertIn(f"Test Song.{fmt}", response.headers["Content-Disposition"])
                    self.assertGreater(len(response.data), 1000)

    def test_over_long_video_is_refused_before_downloading(self):
        found = dict(self._found(), duration=app_module.AUDIO_MAX_SECONDS + 1)
        with patch.object(app_module.yt, "lookup", return_value=found):
            with self.assertRaises(ApiError) as caught:
                app_module._fetch_youtube_audio_source("dQw4w9WgXcQ", tempfile.mktemp())
        self.assertEqual(caught.exception.code, "too_long")

    def test_download_budget_is_shared_across_sources(self):
        with patch.object(app_module.yt, "lookup", return_value=self._found()), \
             patch.object(app_module, "YT_AUDIO_DOWNLOAD_BUDGET", 0):
            with self.assertRaises(ApiError) as caught:
                app_module._fetch_youtube_audio_source("dQw4w9WgXcQ", tempfile.mktemp())
        self.assertEqual(caught.exception.code, "youtube_download_failed")


class FormatsEndpointTests(unittest.TestCase):
    def test_formats_hide_direct_audio_and_offer_conversion_links(self):
        found = {"title": "T", "duration": 3, "provider": "x", "cache": "miss",
                 "normalized": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                 "formats": [_fmt("progressive", "360p"), _fmt("audio", "medium", bitrate=128_000), _fmt("video", "1080p")]}
        with patch.object(app_module.yt, "lookup", return_value=found):
            body = app_module.app.test_client().get("/api/v1/youtube/formats?url=https://youtu.be/dQw4w9WgXcQ").get_json()
        self.assertEqual([f["kind"] for f in body["formats"]], ["progressive", "video"])
        self.assertIn("/api/v1/audio?url=", body["audio"]["mp3"])
        self.assertTrue(body["audio"]["m4a"].endswith("&format=m4a"))


if __name__ == "__main__":
    unittest.main()
