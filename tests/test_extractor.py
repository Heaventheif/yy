import unittest
from unittest.mock import patch

from socialdl.extractor import expand_url


class ExtractorTests(unittest.TestCase):
    @patch("socialdl.extractor.is_public_url", return_value=True)
    @patch("socialdl.extractor.yt_dlp.YoutubeDL")
    def test_facebook_share_is_converted_to_canonical_media_url(self, youtube_dl, _public):
        youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
            "webpage_url": "https://www.facebook.com/reel/1420203020258201/",
        }

        result = expand_url("https://www.facebook.com/share/r/1DoKfAWoTX/")

        self.assertEqual(result, "https://www.facebook.com/reel/1420203020258201/")
        youtube_dl.return_value.__enter__.return_value.extract_info.assert_called_once_with(
            "https://www.facebook.com/share/r/1DoKfAWoTX/", download=False,
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
