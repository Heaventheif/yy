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
