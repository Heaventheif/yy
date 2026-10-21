import http.server
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import app as app_module
from socialdl.errors import ApiError

DATA = os.urandom(300_000)


def _server(honor_range=True, stall=False):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if stall:
                import time
                time.sleep(3)
            rng = self.headers.get("Range")
            if rng and honor_range:
                a, b = rng.split("=")[1].split("-")
                a, b = int(a), min(int(b), len(DATA) - 1)
                body = DATA[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
            else:
                body = DATA
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class RangedFetchTests(unittest.TestCase):
    def _run(self, **kw):
        srv = _server(**kw)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/f"

    def test_reassembles_file_from_ranges(self):
        url = self._run()
        with patch.object(app_module, "RANGE_PIECE", 70_000), patch.object(app_module, "is_public_url", lambda u: True):
            data = b"".join(app_module._ranged_pieces(url, {}, len(DATA)))
        self.assertEqual(data, DATA)

    def test_server_ignoring_range_still_works(self):
        url = self._run(honor_range=False)
        with patch.object(app_module, "RANGE_PIECE", 70_000), patch.object(app_module, "is_public_url", lambda u: True):
            data = b"".join(app_module._ranged_pieces(url, {}, len(DATA)))
        self.assertEqual(data, DATA)

    def test_fetch_to_file_enforces_deadline(self):
        url = self._run(stall=True)
        dest = tempfile.mktemp()
        self.addCleanup(lambda: os.path.exists(dest) and os.remove(dest))
        with patch.object(app_module, "is_public_url", lambda u: True), \
             patch.object(app_module.requests.Session, "request", side_effect=app_module.requests.Timeout()):
            with self.assertRaises(ApiError):
                app_module._fetch_to_file({"url": url, "headers": {}}, dest, 10**6, budget=1)

    def test_fetch_to_file_uses_ranges_for_throttled_hosts(self):
        url = self._run()
        dest = tempfile.mktemp()
        self.addCleanup(lambda: os.path.exists(dest) and os.remove(dest))
        with patch.object(app_module, "RANGE_PIECE", 70_000), patch.object(app_module, "is_public_url", lambda u: True), \
             patch.object(app_module, "_is_throttled", lambda u: True):
            app_module._fetch_to_file({"url": url, "headers": {}}, dest, 10**7, budget=20)
        self.assertEqual(open(dest, "rb").read(), DATA)


if __name__ == "__main__":
    unittest.main()
