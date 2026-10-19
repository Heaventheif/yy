import time
import unittest
from unittest.mock import patch

import app as application
from socialdl import youtube as yt
from socialdl.errors import ApiError


def fmt(provider="toshiro", key="video", kind="progressive", quality="720p", mime="video/mp4"):
    return {"id": f"{provider}:{key}", "provider": provider, "kind": kind, "quality": quality,
            "mime": mime, "bitrate": None, "size": None, "url": f"https://cdn.example.com/{provider}/{key}.mp4"}


class FakeUpstream:
    def __init__(self, status=200, ctype="video/mp4", body=b"data"):
        self.status_code = status
        self.headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        self._body = body

    def iter_content(self, _size):
        yield self._body

    def close(self):
        pass


class FakeSession:
    def close(self):
        pass


class VideoIdTests(unittest.TestCase):
    def test_accepts_common_youtube_shapes(self):
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ",
                    "https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/watch?feature=x&v=dQw4w9WgXcQ"):
            self.assertEqual(yt.extract_video_id(url), "dQw4w9WgXcQ")

    def test_rejects_other_hosts_and_missing_id(self):
        for url in ("https://example.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/", "", None):
            with self.assertRaises(ApiError) as ctx:
                yt.extract_video_id(url)
            self.assertEqual(ctx.exception.status, 400)


class NormalizeTests(unittest.TestCase):
    def test_vidsnap_urls_are_wrapped_in_proxy(self):
        item = yt.normalize_item("vidsnap", 0, {"url": "https://rr1.googlevideo.com/x", "qualityLabel": "360p",
                                                "mimeType": "video/mp4; codecs=avc1"}, wrap_vidsnap=True)
        self.assertTrue(item["url"].startswith("https://api.vidsnap.app/proxy?u="))
        self.assertEqual(item["mime"], "video/mp4")

    def test_sort_puts_audio_and_video_with_sound_first_and_caps_size(self):
        items = [fmt(key=str(i), kind="video", quality=f"{240 + i}p") for i in range(20)]
        items += [fmt(key="a", kind="audio", quality="medium", mime="audio/mp4"), fmt(key="p", quality="360p")]
        out = yt._sort_formats(items)
        self.assertEqual(out[0]["id"], "toshiro:p")
        self.assertEqual(out[1]["kind"], "audio")
        self.assertLessEqual(len(out), yt.MAX_FORMATS)


class LookupTests(unittest.TestCase):
    def setUp(self):
        yt._cache.clear()
        yt._health.clear()

    def test_falls_through_to_next_provider(self):
        def bad(*_):
            raise yt.ProviderError("http_502", 502)
        good = lambda *_: {"title": "T", "duration": 5, "formats": [fmt("vidsnap")]}
        with patch.dict(yt.PROVIDERS, {"toshiro": bad, "vidsnap": good}):
            result = yt.lookup("dQw4w9WgXcQ")
        self.assertEqual(result["provider"], "vidsnap")

    def test_all_failing_gives_502_with_diagnostics(self):
        def bad(*_):
            raise yt.ProviderError("http_502", 502)
        def empty(*_):
            return {"title": "T", "formats": []}
        with patch.dict(yt.PROVIDERS, {"toshiro": bad, "vidsnap": empty}):
            with self.assertRaises(ApiError) as ctx:
                yt.lookup("dQw4w9WgXcQ")
        err = ctx.exception
        self.assertEqual((err.status, err.code, err.retryable), (502, "youtube_providers_unavailable", True))
        self.assertEqual([f["reason"] for f in err.details["providers"]], ["http_502", "empty"])

    def test_provider_enters_cooldown_after_repeated_failures(self):
        for _ in range(yt.COOLDOWN_AFTER):
            yt._record("toshiro", False)
        self.assertEqual(yt._active(["toshiro", "vidsnap"]), ["vidsnap"])
        yt._record("toshiro", True)
        self.assertEqual(yt._active(["toshiro", "vidsnap"]), ["toshiro", "vidsnap"])


class TokenTests(unittest.TestCase):
    def test_roundtrip_tamper_and_expiry(self):
        token = yt.make_token("dQw4w9WgXcQ", "عنوان", fmt())
        self.assertEqual(yt.read_token(token)["v"], "dQw4w9WgXcQ")
        with self.assertRaises(ApiError) as bad:
            yt.read_token(token[:-2] + "xx")
        self.assertEqual(bad.exception.code, "invalid_token")
        with patch("socialdl.youtube.time.time", return_value=time.time() + yt.TOKEN_TTL + 5):
            with self.assertRaises(ApiError) as old:
                yt.read_token(token)
        self.assertEqual((old.exception.status, old.exception.retryable), (410, False))


class RouteTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()
        yt._cache.clear()
        yt._health.clear()

    @patch("app._rate_check")
    def test_formats_never_expose_raw_provider_urls(self, _rate):
        found = {"provider": "toshiro", "video_id": "dQw4w9WgXcQ", "normalized": "n", "title": "T",
                 "duration": 9, "formats": [fmt()], "cache": "miss"}
        with patch("app.yt.lookup", return_value=found):
            response = self.client.get("/api/v1/youtube/formats?url=https://youtu.be/dQw4w9WgXcQ")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("cdn.example.com", response.get_data(as_text=True))
        self.assertIn("/api/v1/youtube/download?t=", body["formats"][0]["download_url"])
        self.assertIn("i.ytimg.com", body["thumbnail"])

    @patch("app._rate_check")
    def test_provider_outage_reports_retryable_502_envelope(self, _rate):
        err = ApiError("youtube_providers_unavailable", "x", 502, retry_after=8,
                       details={"providers": [{"provider": "toshiro", "reason": "timeout"}]})
        with patch("app.yt.lookup", side_effect=err):
            response = self.client.get("/api/v1/youtube/formats?url=https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.headers["Retry-After"], "8")
        body = response.get_json()
        self.assertTrue(body["error"]["retryable"])
        self.assertEqual(body["error"]["details"]["providers"][0]["reason"], "timeout")

    @patch("app._rate_check")
    def test_download_refreshes_link_once_when_source_rejects(self, _rate):
        token = yt.make_token("dQw4w9WgXcQ", "Song", fmt(quality="720p"))
        refreshed = {"provider": "vidsnap", "video_id": "dQw4w9WgXcQ", "normalized": "n", "title": "Song",
                     "duration": 1, "formats": [fmt("vidsnap", "x", quality="720p")], "cache": "miss"}
        calls = []

        def opener(media, _range, _method, **_kw):
            calls.append(media["url"])
            return (FakeUpstream(403), FakeSession()) if len(calls) == 1 else (FakeUpstream(200), FakeSession())

        with patch("app._open_upstream", side_effect=opener), patch("app.yt.lookup", return_value=refreshed):
            response = self.client.get(f"/api/v1/youtube/download?t={token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertIn("vidsnap", calls[1])
        self.assertIn("attachment", response.headers["Content-Disposition"])

    @patch("app._rate_check")
    def test_download_gives_clear_502_after_two_failures(self, _rate):
        token = yt.make_token("dQw4w9WgXcQ", "Song", fmt())
        refreshed = {"provider": "vidsnap", "video_id": "v", "normalized": "n", "title": "Song",
                     "duration": 1, "formats": [fmt("vidsnap", "x")], "cache": "miss"}
        with patch("app._open_upstream", side_effect=lambda *a, **k: (FakeUpstream(403), FakeSession())), \
                patch("app.yt.lookup", return_value=refreshed):
            response = self.client.get(f"/api/v1/youtube/download?t={token}")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"]["code"], "youtube_download_failed")

    @patch("app._rate_check")
    def test_download_rejects_html_error_pages_served_with_200(self, _rate):
        token = yt.make_token("dQw4w9WgXcQ", "Song", fmt())
        refreshed = {"provider": "vidsnap", "video_id": "v", "normalized": "n", "title": "Song",
                     "duration": 1, "formats": [fmt("vidsnap", "x")], "cache": "miss"}
        with patch("app._open_upstream", side_effect=lambda *a, **k: (FakeUpstream(200, "text/html"), FakeSession())), \
                patch("app.yt.lookup", return_value=refreshed):
            response = self.client.get(f"/api/v1/youtube/download?t={token}")
        self.assertEqual(response.status_code, 502)

    def test_download_rejects_forged_token(self):
        response = self.client.get("/api/v1/youtube/download?t=abc.def")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["error"]["retryable"])

class HeightFilterTests(unittest.TestCase):
    def test_cap_excludes_1080p_and_keeps_audio(self):
        formats = [fmt(quality="2160p"), fmt(quality="1080p"), fmt(quality="720p"), fmt(quality="480p"),
                   fmt(key="a", kind="audio", quality="medium", mime="audio/mp4")]
        qs = {f["quality"] for f in yt.filter_by_height(formats, cap=720)}
        self.assertEqual(qs, {"720p", "480p", "medium"})

    def test_k_labels_and_unknown(self):
        self.assertEqual(yt._height("4K"), 2160)
        self.assertEqual(yt._height("8K"), 4320)
        self.assertEqual(yt._height("720p60"), 720)
        self.assertEqual(yt._height("جودة عالية"), 0)

    def test_unknown_height_dropped_strict_kept_relaxed(self):
        formats = [fmt(quality="جودة عالية")]
        self.assertEqual(yt.filter_by_height(formats, cap=720), [])
        self.assertEqual(len(yt.filter_by_height(formats, cap=720, relaxed=True)), 1)

    def test_cap_zero_disables_filtering(self):
        self.assertEqual(len(yt.filter_by_height([fmt(quality="1080p"), fmt(quality="720p")], cap=0)), 2)

    def test_merge_falls_back_to_unlabeled_progressive_not_silent_video(self):
        silent = fmt("vidsnap", "silent", kind="video", quality="720p")
        toshiro = fmt("toshiro", "hi", kind="progressive", quality="جودة عالية")
        out = yt._merge([(0, "toshiro", {}, [toshiro]), (1, "vidsnap", {}, [silent])])
        self.assertTrue(any(f["kind"] == "progressive" for f in out))

    def test_merge_caps_before_max_formats(self):
        many = [fmt(key=str(i), kind="video", quality=f"{1080 + i}p") for i in range(20)]
        keep = fmt("vidsnap", "ok", quality="480p")
        out = yt._merge([(0, "vidsnap", {}, many + [keep])])
        self.assertEqual([f["quality"] for f in out], ["480p"])


class TrimTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()

    def audio_token(self, mime="audio/mp4"):
        return yt.make_token("dQw4w9WgXcQ", "T", fmt(kind="audio", mime=mime, quality="medium"))

    @patch("app._rate_check")
    def test_rejects_non_audio_token(self, _rate):
        tok = yt.make_token("dQw4w9WgXcQ", "T", fmt(kind="progressive"))
        r = self.client.get(f"/api/v1/youtube/trim?t={tok}&start=10&end=20")
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "not_audio"))

    @patch("app._rate_check")
    def test_rejects_bad_times(self, _rate):
        tok = self.audio_token()
        for query, code in (("start=100&end=50", "invalid_request"), ("start=nan&end=5", "invalid_time"),
                            ("start=-3", "invalid_time"), ("", "invalid_request")):
            r = self.client.get(f"/api/v1/youtube/trim?t={tok}&{query}")
            self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, code), query)

    @patch("app._rate_check")
    def test_head_does_no_work(self, _rate):
        r = self.client.head(f"/api/v1/youtube/trim?t={self.audio_token()}&start=1&end=2")
        self.assertEqual(r.status_code, 200)

    @patch("app._rate_check")
    def test_real_trim_with_ffmpeg(self, _rate):
        import subprocess, tempfile, os
        ffmpeg = application._ffmpeg_exe()
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        src = os.path.join(tempfile.mkdtemp(), "tone.m4a")
        subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=10", "-c:a", "aac", src], check=True)
        body = open(src, "rb").read()

        class Up(FakeUpstream):
            def iter_content(self, _size):
                yield body

        for extra in ("", "&reencode=1"):
            with patch("app._open_upstream", return_value=(Up(ctype="audio/mp4"), FakeSession())):
                r = self.client.get(f"/api/v1/youtube/trim?t={self.audio_token()}&start=2&end=5{extra}")
                data = r.get_data()
            self.assertEqual(r.status_code, 200)
            out = os.path.join(tempfile.mkdtemp(), "o.m4a")
            open(out, "wb").write(data)
            probe = subprocess.run([ffmpeg, "-i", out, "-f", "null", "-"], capture_output=True, text=True).stderr
            self.assertRegex(probe, r"time=00:00:0[2-3]\.")
        self.assertEqual([f for f in os.listdir("/tmp") if f.startswith("yt_")], [])


if __name__ == "__main__":
    unittest.main()


class SpeedTests(unittest.TestCase):
    def setUp(self):
        yt._cache.clear()
        yt._health.clear()

    def test_fast_provider_wins_without_waiting_for_a_hung_one(self):
        def hung(*_):
            time.sleep(3)
            return {"title": "slow", "formats": [fmt("toshiro")]}
        quick = lambda *_: {"title": "T", "duration": 1, "formats": [fmt("vidsnap", str(i), quality=f"{200+i}p") for i in range(7)]}
        started = time.monotonic()
        with patch.dict(yt.PROVIDERS, {"toshiro": hung, "vidsnap": quick}):
            result = yt.lookup("dQw4w9WgXcQ")
        self.assertEqual(result["provider"], "vidsnap")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_all_hung_gives_timeout_within_budget(self):
        def hung(*_):
            time.sleep(1.5)
            return {"formats": [fmt()]}
        started = time.monotonic()
        with patch.object(yt, "TOTAL_BUDGET", 0.4), patch.dict(yt.PROVIDERS, {"toshiro": hung, "vidsnap": hung}):
            with self.assertRaises(ApiError) as ctx:
                yt.lookup("dQw4w9WgXcQ")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (504, "youtube_timeout"))
        self.assertLess(time.monotonic() - started, 1.2)


class MergeTests(unittest.TestCase):
    def setUp(self):
        yt._cache.clear()
        yt._health.clear()

    def test_formats_from_several_providers_are_combined(self):
        a = lambda *_: {"title": "T", "formats": [fmt("toshiro", "a", quality="720p")]}
        b = lambda *_: {"title": "T", "formats": [fmt("vidsnap", "b", quality="360p"),
                                                  fmt("vidsnap", "c", kind="audio", quality="medium", mime="audio/mp4"),
                                                  fmt("vidsnap", "d", quality="720p")]}
        with patch.dict(yt.PROVIDERS, {"toshiro": a, "vidsnap": b}):
            result = yt.lookup("dQw4w9WgXcQ")
        qualities = [(f["kind"], f["quality"]) for f in result["formats"]]
        self.assertEqual(len(result["formats"]), 3)            # 720p duplicate collapsed
        self.assertIn(("audio", "medium"), qualities)
        self.assertEqual(result["formats"][0]["provider"], "toshiro")
