import unittest
from unittest.mock import patch

from socialdl.errors import ApiError
import socialdl.tiktok as tiktok


TIKTOK_URL = "https://www.tiktok.com/@user/video/1234567890123456789"
CDN_URL = "https://v16m.tiktokcdn-us.com/video/test.mp4"


class TikTokProviderTests(unittest.TestCase):
    @patch("socialdl.tiktok.is_public_url", return_value=True)
    @patch("socialdl.tiktok._get_json")
    def test_toshiro_response_becomes_progressive_media(self, get_json, _public):
        get_json.return_value = {
            "success": True,
            "platform": "TikTok",
            "result": {"success": True, "title": "Video title", "video": CDN_URL,
                       "cover": "https://p19-common-sign.tiktokcdn-us.com/cover.jpg", "author": "Creator"},
        }

        media = tiktok._toshiro(TIKTOK_URL)

        self.assertEqual(media["url"], CDN_URL)
        self.assertEqual(media["title"], "Video title")
        self.assertEqual(media["site"], "TikTok")
        self.assertEqual(media["ext"], "mp4")
        self.assertEqual(media["headers"]["Referer"], "https://www.tiktok.com/")

    @patch("socialdl.tiktok.is_public_url", return_value=True)
    def test_vidsnap_selects_video_format_not_audio(self, _public):
        with patch("socialdl.tiktok._get_json", return_value={
            "title": "VidSnap title",
            "formats": [
                {"kind": "audio", "mimeType": "audio/mpeg", "url": "https://audio.example/a.mp3"},
                {"kind": "progressive", "mimeType": "video/mp4", "url": CDN_URL,
                 "height": 720, "hasAudio": True, "size": 1234},
            ],
            "thumbnails": [],
        }):
            media = tiktok._vidsnap(TIKTOK_URL)

        self.assertEqual(media["url"], CDN_URL)
        self.assertEqual(media["title"], "VidSnap title")
        self.assertEqual(media["size"], 1234)

    @patch("socialdl.tiktok.is_public_url", return_value=True)
    def test_toshiro_failure_falls_back_to_vidsnap(self, _public):
        fallback = {"url": CDN_URL, "title": "fallback", "site": "TikTok"}
        with patch.dict(tiktok.PROVIDERS, {
            "toshiro": lambda _url: (_ for _ in ()).throw(tiktok.ProviderError("unreachable")),
            "vidsnap": lambda _url: fallback,
        }):
            self.assertIs(tiktok.resolve_tiktok(TIKTOK_URL), fallback)

    def test_all_provider_failures_return_retryable_api_error(self):
        with patch.dict(tiktok.PROVIDERS, {
            "toshiro": lambda _url: (_ for _ in ()).throw(tiktok.ProviderError("unreachable")),
            "vidsnap": lambda _url: (_ for _ in ()).throw(tiktok.ProviderError("empty")),
        }):
            with self.assertRaises(ApiError) as ctx:
                tiktok.resolve_tiktok(TIKTOK_URL)
        self.assertEqual(ctx.exception.code, "tiktok_providers_unavailable")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual([x["provider"] for x in ctx.exception.details["providers"]], ["toshiro", "vidsnap"])

    @patch("socialdl.extractor.resolve_tiktok", return_value={"site": "TikTok", "url": CDN_URL})
    @patch("socialdl.extractor.yt_dlp.YoutubeDL")
    @patch("socialdl.extractor.site_allowed", return_value=True)
    @patch("socialdl.extractor.expand_url", side_effect=lambda value: value)
    @patch("socialdl.extractor.clean_url", side_effect=lambda value: value)
    def test_extractor_routes_tiktok_to_providers_only(self, _clean, _expand, _allowed, youtube_dl, resolve):
        from socialdl.extractor import extract_media

        media = extract_media(TIKTOK_URL)

        self.assertEqual(media["site"], "TikTok")
        resolve.assert_called_once_with(TIKTOK_URL)
        youtube_dl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
