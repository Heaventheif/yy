import unittest
from unittest.mock import patch

from socialdl.errors import ApiError
from socialdl.platforms import clean_url, host_matches, is_public_url, is_youtube_host, provider_for_host


class PlatformTests(unittest.TestCase):
    def setUp(self):
        import socialdl.platforms as platforms
        patcher = patch.object(platforms, "ALLOW_ALL_SITES", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_domain_matching_requires_label_boundary(self):
        self.assertTrue(host_matches("m.facebook.com", ("facebook.com",)))
        self.assertFalse(host_matches("notfacebook.com", ("facebook.com",)))

    def test_platform_lookup(self):
        self.assertEqual(provider_for_host("www.facebook.com"), "Facebook")
        self.assertEqual(provider_for_host("vm.tiktok.com"), "TikTok")
        self.assertIsNone(provider_for_host("example.org"))

    def test_youtube_and_lookalikes(self):
        self.assertTrue(is_youtube_host("youtu.be"))
        self.assertTrue(is_youtube_host("m.youtube.com"))
        self.assertFalse(is_youtube_host("youtube.com.example.org"))
        with self.assertRaises(ApiError) as raised:
            clean_url("https://youtu.be/abc123")
        self.assertEqual(raised.exception.code, "youtube_unsupported")

    def test_sites_requiring_official_api_are_not_scraped(self):
        for url in ("https://vimeo.com/123", "https://pin.it/abc", "https://twitch.tv/name"):
            with self.subTest(url=url), self.assertRaises(ApiError) as raised:
                clean_url(url)
            self.assertEqual(raised.exception.code, "official_api_required")
        with self.assertRaises(ApiError) as raised:
            clean_url("https://x.com/user/status/123")
        self.assertEqual(raised.exception.code, "official_api_required")

    def test_only_allowlisted_platforms(self):
        with self.assertRaises(ApiError) as raised:
            clean_url("https://example.org/video/1")
        self.assertEqual(raised.exception.code, "unsupported")

    def test_reject_credentials_and_nonstandard_ports(self):
        with self.assertRaises(ApiError):
            clean_url("https://name:pass@facebook.com/video/123")
        with self.assertRaises(ApiError):
            clean_url("https://facebook.com:8443/video/123")

    @patch("socialdl.platforms._public_addresses", return_value=True)
    def test_accepts_supported_public_social_url(self, _dns):
        self.assertEqual(clean_url("facebook.com/watch/?v=123"), "https://facebook.com/watch/?v=123")

    @patch("socialdl.platforms.socket.getaddrinfo")
    def test_rejects_private_ip_resolution(self, getaddrinfo):
        getaddrinfo.return_value = [(None, None, None, None, ("127.0.0.1", 0))]
        self.assertFalse(is_public_url("https://facebook.com/video/1"))


if __name__ == "__main__":
    unittest.main()


class OpenModeTests(unittest.TestCase):
    def test_open_mode_allows_other_sites_but_never_youtube(self):
        import socialdl.platforms as platforms
        with patch.object(platforms, "ALLOW_ALL_SITES", True):
            self.assertTrue(platforms.site_allowed("vimeo.com"))
            self.assertTrue(platforms.site_allowed("example.org"))
            self.assertFalse(platforms.site_allowed("m.youtube.com"))
            self.assertFalse(platforms.site_allowed("youtu.be"))


class YoutubeSeparationTests(unittest.TestCase):
    """YouTube goes only through the external providers, never through yt-dlp."""

    def test_ytdlp_options_exclude_youtube_and_generic(self):
        import yt_dlp
        from socialdl.extractor import _options
        with yt_dlp.YoutubeDL(_options()) as ydl:
            names = [ie.IE_NAME.lower() for ie in ydl._ies.values()]
        self.assertFalse([n for n in names if n.startswith("youtube")])
        self.assertNotIn("generic", names)

    def test_extract_media_never_calls_ytdlp_for_youtube(self):
        import socialdl.extractor as extractor
        with patch.object(extractor.yt_dlp, "YoutubeDL") as ydl:
            for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ"):
                with self.assertRaises(ApiError):
                    extractor.extract_media(url)
            ydl.assert_not_called()


class LinkNormalizationTests(unittest.TestCase):
    def setUp(self):
        import socialdl.platforms as platforms
        for target in ("is_public_url",):
            patcher = patch.object(platforms, target, lambda url: True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_tracking_params_and_fragment_removed(self):
        url = clean_url("انظر https://www.tiktok.com/@a/video/1?utm_source=x&igsh=abc&lang=ar#frag، شكرا")
        self.assertEqual(url, "https://www.tiktok.com/@a/video/1?lang=ar")

    def test_redirect_wrappers_unwrapped_without_network(self):
        wrapped = "https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.dailymotion.com%2Fvideo%2Fx1abc&h=1"
        self.assertEqual(clean_url(wrapped), "https://www.dailymotion.com/video/x1abc")

    def test_wrapper_to_youtube_signals_youtube_route(self):
        with self.assertRaises(ApiError) as raised:
            clean_url("https://www.google.com/url?q=https://www.youtube.com/watch?v=dQw4w9WgXcQ&sa=D")
        self.assertEqual(raised.exception.code, "youtube_redirect")
        self.assertIn("youtube.com", raised.exception.details["url"])

    def test_short_link_to_youtube_signals_youtube_route(self):
        import socialdl.extractor as extractor

        class Resp:
            status_code = 301
            headers = {"Location": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
            def close(self): pass
        extractor._expand_cache.clear()
        with patch.object(extractor.requests.Session, "get", return_value=Resp()):
            with self.assertRaises(ApiError) as raised:
                extractor.expand_url("https://bit.ly/abc")
        self.assertEqual(raised.exception.code, "youtube_redirect")

    def test_expansion_is_cached(self):
        import socialdl.extractor as extractor

        class Resp:
            status_code = 301
            headers = {"Location": "https://vimeo.com/123"}
            def close(self): pass
        extractor._expand_cache.clear()
        with patch.object(extractor, "is_public_url", lambda url: True), \
                patch.object(extractor.requests.Session, "get", return_value=Resp()) as get:
            first = extractor.expand_url("https://tinyurl.com/zz")
            second = extractor.expand_url("https://tinyurl.com/zz")
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)  # the second call is served from the cache
