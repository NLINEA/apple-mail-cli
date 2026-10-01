from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "apple-mail-cli" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import mail_index  # noqa: E402
import mailctl  # noqa: E402


@unittest.skipUnless(os.environ.get("APPLE_MAIL_CLI_LIVE_TESTS") == "1", "opt-in live Apple Mail test")
class LiveAppleMailTests(unittest.TestCase):
    def test_doctor_and_bounded_index_search(self) -> None:
        doctor = mailctl.doctor(30)
        self.assertTrue(doctor["ok"], doctor)
        result = mail_index.search_messages(limit=2)
        self.assertLessEqual(result["returned"], 2)
        self.assertFalse(result["body_searched"])


if __name__ == "__main__":
    unittest.main()
