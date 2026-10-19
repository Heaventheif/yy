import unittest
from unittest.mock import patch

from socialdl.errors import ApiError
from socialdl.platforms import clean_url, host_matches, is_public_url, is_youtube_host, provider_for_host


class PlatformTests(unittest.TestCase):
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
