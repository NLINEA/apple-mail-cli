from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "apple-mail-cli" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import mail_store  # noqa: E402


class MailStoreTests(unittest.TestCase):
    def write_emlx(self, version_root: Path, *, message_id: str, payload: bytes) -> Path:
        message = EmailMessage()
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "recipient@example.com"
        message["Subject"] = "Evidence"
        message["Message-ID"] = message_id
        message.set_content("Body text")
        message.add_attachment(payload, maintype="application", subtype="pdf", filename="evidence.pdf")
        message_bytes = message.as_bytes(policy=policy.SMTP)
        target = version_root / "ACCOUNT" / "SendLater.mbox" / "STORE" / "Data" / "Messages" / "1.emlx"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(str(len(message_bytes)).encode("ascii") + b"\n" + message_bytes + b"trailing plist")
        return target

    def test_load_send_later_evidence_reads_only_matching_emlx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            version_root = Path(temporary) / "V10"
            payload = b"attachment payload"
            self.write_emlx(version_root, message_id="<one@example.test>", payload=payload)

            result = mail_store.load_send_later_evidence(
                {"<one@example.test>", "<missing@example.test>"},
                mail_version_root=version_root,
            )

        evidence = result["evidence"]["<one@example.test>"]
        self.assertEqual(evidence["sender_addresses"], ["sender@example.com"])
        self.assertEqual(evidence["attachment_count"], 1)
        self.assertEqual(
            evidence["attachments"],
            [
                {
                    "name": "evidence.pdf",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        )
        self.assertEqual(result["diagnostics"]["files_scanned"], 1)
        self.assertEqual(result["diagnostics"]["message_ids_found"], 1)
        self.assertEqual(result["diagnostics"]["evidence_loaded"], 1)

    def test_private_cache_reuses_evidence_and_invalidates_on_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version_root = root / "V10"
            cache_path = root / "cache" / "evidence.json"
            target = self.write_emlx(version_root, message_id="<one@example.test>", payload=b"first payload")
            first = mail_store.load_send_later_evidence(
                {"<one@example.test>"},
                mail_version_root=version_root,
                cache_path=cache_path,
            )
            with unittest.mock.patch.object(mail_store, "read_emlx_message", side_effect=AssertionError("cache miss")):
                second = mail_store.load_send_later_evidence(
                    {"<one@example.test>"},
                    mail_version_root=version_root,
                    cache_path=cache_path,
                )
            previous_mtime = target.stat().st_mtime_ns
            self.write_emlx(version_root, message_id="<one@example.test>", payload=b"updated payload with a new size")
            os.utime(target, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
            third = mail_store.load_send_later_evidence(
                {"<one@example.test>"},
                mail_version_root=version_root,
                cache_path=cache_path,
            )
            cache_mode = stat.S_IMODE(cache_path.stat().st_mode)
            encoded_cache = cache_path.read_text(encoding="utf-8")

        self.assertTrue(first["diagnostics"]["cache_written"])
        self.assertEqual(second["diagnostics"]["cache_hits"], 1)
        self.assertEqual(second["diagnostics"]["evidence_cache_hits"], 1)
        self.assertEqual(second["diagnostics"]["cache_misses"], 0)
        self.assertNotEqual(
            first["evidence"]["<one@example.test>"]["attachments"][0]["sha256"],
            third["evidence"]["<one@example.test>"]["attachments"][0]["sha256"],
        )
        self.assertEqual(third["diagnostics"]["cache_misses"], 1)
        self.assertEqual(cache_mode, 0o600)
        self.assertNotIn(str(target), encoded_cache)
        self.assertNotIn("Body text", encoded_cache)
        self.assertEqual(json.loads(encoded_cache)["schema_version"], mail_store.CACHE_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
