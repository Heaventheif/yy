import unittest
from unittest.mock import MagicMock, patch

from socialdl import fbshare as f


class _Resp:
    def __init__(self, code, loc=None, body=""):
        self.status_code, self.headers = code, ({"Location": loc} if loc else {})
        self.raw = MagicMock()
        self.raw.read.return_value = body.encode()

    def close(self):
        pass


def _run(responses, url="https://www.facebook.com/share/v/1HkkyeQLw1/"):
    it = iter(responses)
    with patch.object(f.requests.Session, "get", lambda self, *a, **k: next(it)):
        return f.facebook_share_to_id_url(url)


class FbShareTests(unittest.TestCase):
    def test_login_wall_next_is_unwrapped(self):
        login = "https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com%2Freel%2F1080741757962951"
        self.assertEqual(_run([_Resp(302, login)]), "https://www.facebook.com/reel/1080741757962951")

    def test_crawler_fallback_reads_og_url(self):
        meta = '<meta property="og:url" content="https://www.facebook.com/reel/1080741757962951/" />'
        self.assertEqual(_run([_Resp(200, body="login"), _Resp(403), _Resp(200, body=meta)]),
                         "https://www.facebook.com/reel/1080741757962951")

    def test_share_r_is_reel_and_share_v_is_watch(self):
        target = "https://www.facebook.com/watch/?v=1080741757962951"
        self.assertEqual(_run([_Resp(302, target)], "https://www.facebook.com/share/r/A/"),
                         "https://www.facebook.com/reel/1080741757962951")
        self.assertEqual(_run([_Resp(302, target)]), target)

    def test_never_follows_redirects_off_facebook(self):
        self.assertIsNone(_run([_Resp(302, "https://evil.example/x")] * 4))


class LoginWallTests(unittest.TestCase):
    def test_login_wall_next_is_unwrapped_once(self):
        wall = "https://www.facebook.com/login/?next=https%3A%2F%2Fwww.facebook.com%2Freel%2F1080741757962951"
        self.assertEqual(f.unwrap_login_wall(wall), "https://www.facebook.com/reel/1080741757962951")

    def test_relative_next_is_resolved(self):
        self.assertEqual(f.unwrap_login_wall("https://www.facebook.com/login/?next=%2Freel%2F1080741757962951"),
                         "https://www.facebook.com/reel/1080741757962951")

    def test_login_wall_without_target_gives_none(self):
        self.assertIsNone(f.unwrap_login_wall("https://www.facebook.com/login/"))
        self.assertIsNone(f.unwrap_login_wall("https://www.facebook.com/checkpoint/?next=javascript%3Aalert(1)"))

    def test_ordinary_urls_are_unchanged(self):
        for url in ("https://www.facebook.com/reel/1080741757962951", "https://example.com/login/?next=/x"):
            self.assertEqual(f.unwrap_login_wall(url), url)
