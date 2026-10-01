#!/usr/bin/env python3
"""Synthetic tests only: no Mail access and no HTTP loopback server."""
import os
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
os.environ.pop("APPLE_MAIL_CLI_LIVE_TESTS", None)
root = Path(__file__).resolve().parents[1]

def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test

suite = unittest.defaultTestLoader.discover(str(root / "tests"))
tests = [t for t in cases(suite) if not t.id().startswith("test_ubd_http_client.")]
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(tests))
raise SystemExit(not result.wasSuccessful())
