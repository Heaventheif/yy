import unittest
from unittest.mock import patch

from socialdl.extractor import expand_url


class ExtractorTests(unittest.TestCase):
    def setUp(self):
        # no real network in tests: HTTP hop finds nothing, so yt-dlp (mocked per test) is the fallback
        import socialdl.extractor as extractor
        extractor._expand_cache.clear()
        patcher = patch.object(extractor, "_http_hop", return_value=None)
        self.http_hop = patcher.start()
        self.addCleanup(patcher.stop)
        fb = patch.object(extractor, "facebook_share_to_id_url", return_value=None)
        fb.start()
        self.addCleanup(fb.stop)

    @patch("socialdl.extractor.is_public_url", return_value=True)
    @patch("socialdl.extractor.yt_dlp.YoutubeDL")
    def test_http_redirect_is_used_first_and_skips_ytdlp(self, youtube_dl, _public):
        self.http_hop.return_value = "https://www.tiktok.com/@user/video/7123?_r=1&lang=ar"

        result = expand_url("https://vt.tiktok.com/ZSabc/")

        self.assertEqual(result, "https://www.tiktok.com/@user/video/7123?lang=ar")
        youtube_dl.assert_not_called()

    @patch("socialdl.extractor.is_public_url", return_value=True)
    @patch("socialdl.extractor.yt_dlp.YoutubeDL")
    def test_url_type_result_from_ytdlp_is_followed_without_processing_formats(self, youtube_dl, _public):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "_type": "url", "url": "https://www.facebook.com/watch/?v=999",
        }

        result = expand_url("https://www.facebook.com/share/v/AbC/")

        self.assertEqual(result, "https://www.facebook.com/watch/?v=999")

    def test_ordinary_video_pages_are_not_expanded(self):
        for url in ("https://www.instagram.com/stories/user/123/", "https://www.tiktok.com/@u/video/1",
                    "https://example.org/t/topic", "https://www.facebook.com/reel/1/"):
            with self.subTest(url=url):
                self.assertEqual(expand_url(url), url)
        self.http_hop.assert_not_called()

    @patch("socialdl.extractor.is_public_url", return_value=True)
    @patch("socialdl.extractor.yt_dlp.YoutubeDL")
    def test_facebook_share_is_converted_to_canonical_media_url(self, youtube_dl, _public):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "webpage_url": "https://www.facebook.com/reel/1420203020258201/",
        }

        result = expand_url("https://www.facebook.com/share/r/1DoKfAWoTX/")

        self.assertEqual(result, "https://www.facebook.com/reel/1420203020258201/")
        youtube_dl.return_value.__enter__.return_value.extract_info.assert_called_once_with(
            "https://www.facebook.com/share/r/1DoKfAWoTX/", download=False, process=False,
        )

    @patch("socialdl.extractor.is_public_url", return_value=True)
    @patch("socialdl.extractor.yt_dlp.YoutubeDL")
    def test_supported_non_facebook_share_is_also_normalized(self, youtube_dl, _public):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "webpage_url": "https://www.instagram.com/reel/ABC123/",
        }

        result = expand_url("https://www.instagram.com/share/reel/XYZ789/")

        self.assertEqual(result, "https://www.instagram.com/reel/ABC123/")

    @patch("socialdl.extractor.is_public_url", return_value=True)
    @patch("socialdl.extractor.yt_dlp.YoutubeDL")
    def test_supported_short_alias_is_normalized(self, youtube_dl, _public):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "webpage_url": "https://www.dailymotion.com/video/x123abc/",
        }

        result = expand_url("https://dai.ly/x123abc")

        self.assertEqual(result, "https://www.dailymotion.com/video/x123abc/")

    @patch("socialdl.extractor.yt_dlp.YoutubeDL")
    def test_youtube_is_not_normalized_by_share_resolver(self, youtube_dl):
        result = expand_url("https://youtu.be/abc123")

        self.assertEqual(result, "https://youtu.be/abc123")
        youtube_dl.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class SoundCloudHlsTests(unittest.TestCase):
    """SoundCloud tracks without a progressive stream only expose audio-only HLS."""

    HLS_INFO = {
        "title": "Track", "ext": "m4a", "protocol": "m3u8_native", "vcodec": "none", "acodec": "mp4a.40.2",
        "url": "https://playback.media-streaming.soundcloud.cloud/x/playlist.m3u8", "extractor_key": "Soundcloud",
        "duration": 120,
    }

    def _extract(self, info):
        from socialdl.extractor import extract_media
        with patch("socialdl.extractor.clean_url", side_effect=lambda u: u), \
                patch("socialdl.extractor.expand_url", side_effect=lambda u: u), \
                patch("socialdl.extractor.is_public_url", return_value=True), \
                patch("socialdl.extractor.yt_dlp.YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = dict(info)
            return extract_media("https://soundcloud.com/artist/track")

    def test_hls_only_audio_is_accepted_for_the_audio_converter(self):
        media = self._extract(self.HLS_INFO)
        self.assertTrue(media["hls"])
        self.assertTrue(media["has_audio"])
        self.assertEqual(media["audio_url"], self.HLS_INFO["url"])
        self.assertEqual(media["ext"], "m4a")

    def test_hls_video_is_still_rejected(self):
        from socialdl.errors import ApiError
        with self.assertRaises(ApiError) as ctx:
            self._extract({**self.HLS_INFO, "vcodec": "avc1"})
        self.assertEqual(ctx.exception.code, "no_single_file")

    def test_format_selector_falls_back_to_hls_audio(self):
        import yt_dlp
        from socialdl.extractor import _options
        select = yt_dlp.YoutubeDL({"quiet": True}).build_format_selector(_options()["format"])
        hls = {"format_id": "hls_aac_160k", "url": "https://x/p.m3u8", "ext": "m4a", "vcodec": "none",
               "acodec": "aac", "protocol": "m3u8_native"}
        http = {**hls, "format_id": "http_mp3_128", "ext": "mp3", "protocol": "http"}
        self.assertEqual([f["format_id"] for f in select({"formats": [hls], "incomplete_formats": False})], ["hls_aac_160k"])
        self.assertEqual([f["format_id"] for f in select({"formats": [hls, http], "incomplete_formats": False})], ["http_mp3_128"])
