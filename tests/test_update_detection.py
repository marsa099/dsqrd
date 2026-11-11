"""Exercise the daemon's real update methods without Discord/Qt dependencies."""
import ast
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1] / "dsqrd.py"
tree = ast.parse(SOURCE.read_text())
original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DQS")
methods = [node for node in original.body if isinstance(node, ast.FunctionDef)
           and node.name in {"_check_update_once", "_poll_update", "_gh", "_check_plain", "_check_fork",
                             "_missing_commits", "_emit_update", "_clear_update"}]
namespace = {"os": os, "json": json, "urllib": urllib, "time": time, "GIT_REV": "installed"}
module = ast.Module(body=[ast.ClassDef(name="Detector", bases=[], keywords=[], body=methods,
                                      decorator_list=[])], type_ignores=[])
exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
Detector = namespace["Detector"]


class UpdateDetectionTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DSQRD_UPDATE_REPO": "fork/dsqrd",
                                          "DSQRD_UPSTREAM_REPO": "upstream/dsqrd"})
        self.env.start()
        for key in ("DSQRD_FORK_REV", "DSQRD_UPSTREAM_REV"):
            os.environ.pop(key, None)
        self.addCleanup(self.env.stop)
        self.detector = Detector()
        self.detector._update_cache = {}
        self.detector._update_compare_bases = {}
        self.detector._update_lock = threading.Lock()
        self.detector.update_event = None
        self.events = []
        self.detector.broadcast = self.events.append
        self.detector._fetch_changelog = lambda *_: []

    def comparisons(self, fork_ahead, upstream_ahead):
        def gh(url, accept=None):
            if "/commits/" in url:
                self.assertNotIn(":", url.split("/commits/", 1)[1])
                return "upstream-tip" if "/upstream/dsqrd/" in url else "fork-tip"
            return {"ahead_by": upstream_ahead if "upstream:main" in url else fork_ahead}
        self.detector._gh = gh

    def test_current_or_local_ahead_does_not_advertise_update(self):
        self.comparisons(0, 0)
        self.assertIsNone(self.detector._poll_update())
        self.assertEqual(self.events, [])

    def test_fork_and_upstream_updates(self):
        for fork, upstream, expected in ((1, 0, "fork-tip"), (0, 1, "upstream-tip"),
                                         (1, 1, "fork-tip"), (300, 0, "fork-tip")):
            with self.subTest(fork=fork, upstream=upstream):
                self.comparisons(fork, upstream)
                self.assertEqual(self.detector._poll_update(), expected)

    def test_unpublished_merge_uses_installed_remote_baselines(self):
        os.environ.update(DSQRD_FORK_REV="fork-base", DSQRD_UPSTREAM_REV="upstream-base")
        urls = []
        self.detector._gh = lambda url: urls.append(url) or {"ahead_by": 0}
        self.assertIsNone(self.detector._poll_update())
        self.assertTrue(urls[0].endswith("fork-base...main"))
        self.assertTrue(urls[1].endswith("upstream-base...upstream:main"))

    def test_failed_second_leg_does_not_clear_or_replace_badge(self):
        self.detector.update_event = {"latest": "previous"}
        def fail(url, accept=None):
            if "upstream:main" in url:
                raise OSError("network down")
            return {"ahead_by": 0}
        self.detector._gh = fail
        self.detector._check_update_once()
        self.assertEqual(self.detector.update_event, {"latest": "previous"})
        self.assertEqual(self.events, [])

    def test_successful_current_check_clears_old_badge(self):
        self.detector.update_event = {"latest": "previous"}
        self.comparisons(0, 0)
        self.detector._poll_update()
        self.assertIsNone(self.detector.update_event)
        self.assertEqual(self.events[-1]["latest"], "")

    def test_overlapping_poll_is_skipped(self):
        self.detector._update_lock.acquire()
        self.detector._poll_update = lambda: self.fail("overlapping poll")
        self.detector._check_update_once()

    def test_conditional_requests_cache_body_and_etag_per_url(self):
        class Response:
            def __init__(self, etag, value):
                self.headers, self.value = {"ETag": etag}, value
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps(self.value).encode()
        requests = []
        def urlopen(request, timeout):
            requests.append(request)
            if len(requests) <= 2:
                return Response(f'"etag-{len(requests)}"', {"ahead_by": len(requests)})
            raise urllib.error.HTTPError(request.full_url, 304, "unchanged", {}, None)
        with patch("urllib.request.urlopen", urlopen):
            for url, expected in (("https://api.github.com/a", 1), ("https://api.github.com/b", 2),
                                  ("https://api.github.com/a", 1), ("https://api.github.com/b", 2)):
                self.assertEqual(self.detector._gh(url), {"ahead_by": expected})
        self.assertIsNone(requests[1].get_header("If-none-match"))
        self.assertEqual(requests[2].get_header("If-none-match"), '"etag-1"')
        self.assertEqual(requests[3].get_header("If-none-match"), '"etag-2"')


if __name__ == "__main__":
    unittest.main()
