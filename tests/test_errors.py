import unittest

from socialdl.errors import classify_extractor_error, from_upstream_status


class UpstreamErrorTests(unittest.TestCase):
    def test_402_is_attributed_to_upstream(self):
        error = from_upstream_status(402)
        self.assertEqual(error.status, 502)
        self.assertEqual(error.upstream_status, 402)
        self.assertEqual(error.code, "source_payment_required")

    def test_403_is_not_reported_as_generic_failure(self):
        error = classify_extractor_error(Exception("HTTP Error 403: Forbidden"))
        self.assertEqual(error.code, "source_forbidden")
        self.assertEqual(error.upstream_status, 403)

    def test_tls_port_443_has_actionable_message(self):
        error = classify_extractor_error(Exception("SSLError: TLS handshake failed"))
        self.assertEqual(error.code, "upstream_tls_error")
        self.assertIn("443", error.message)

    def test_https_connection_failure_mentions_unreachable_source(self):
        error = classify_extractor_error(Exception("HTTPSConnectionPool(host='example.com', port=443): Max retries exceeded"))
        self.assertEqual(error.code, "upstream_unreachable")

    def test_private_media_does_not_offer_cookie_workaround(self):
        error = classify_extractor_error(Exception("This video is private. Login required"))
        self.assertEqual(error.code, "source_forbidden")
        self.assertIn("تسجيل الدخول", error.message)


if __name__ == "__main__":
    unittest.main()
