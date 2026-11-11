#!/usr/bin/env python3
"""Live Niri regression test: closes/reopens dsqrd; requires a running UI.

Run: python3 tests/test_window_reopen.py /path/to/dsqrd-client
This changes desktop focus and leaves the Discord window open.
"""
import json
import subprocess
import sys
import time
import unittest

LAUNCHER = "dsqrd-client"


def windows():
    data = subprocess.check_output(["niri", "msg", "--json", "windows"])
    return [window for window in json.loads(data) if window["title"] == "dsqrd"]


def wait_for_windows(count):
    for _ in range(60):
        result = windows()
        if len(result) == count:
            return result
        time.sleep(0.1)
    raise AssertionError(f"Expected {count} dsqrd windows, got {result}")


@unittest.skipUnless(__name__ == "__main__", "live desktop test; run explicitly")
class WindowReopenTest(unittest.TestCase):
    def test_close_and_reopen_same_process(self):
        original = wait_for_windows(1)[0]["pid"]
        for cycle in range(3):
            with self.subTest(cycle=cycle):
                subprocess.run(
                    ["niri", "msg", "action", "close-window", "--id",
                     str(windows()[0]["id"])], check=True,
                )
                wait_for_windows(0)
                subprocess.run([LAUNCHER], check=True)
                self.assertEqual(wait_for_windows(1)[0]["pid"], original)
        # Launching again must not create another UI process/window.
        subprocess.run([LAUNCHER], check=True)
        self.assertEqual(wait_for_windows(1)[0]["pid"], original)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        LAUNCHER = sys.argv.pop(1)
    unittest.main()
