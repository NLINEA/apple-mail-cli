"""Synthetic saved-copy reviews and save receipts. Never starts Apple Mail."""
from email.message import EmailMessage
from email import policy
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/apple-mail-cli/scripts"))
import draft_review
import mailctl
import operation_state
import send_guard


class SavedDraftReviewTests(unittest.TestCase):
    def fixture(self, *, attachment=True, html=False):
        message = EmailMessage()
        message["Message-ID"] = "<fixture-1@example.test>"
        message["From"] = "Sender <sender@example.test>"
        message["To"] = "recipient@example.test"
        message["Subject"] = "香港 café"
        message.set_content("Hello\n\nEnd\n", charset="utf-8")
        if html:
            message.add_alternative("<p>Different HTML for visual review</p>", subtype="html")
        files = []
        if attachment:
            data = b"synthetic attachment\x00\xff"
            message.add_attachment(data, maintype="application", subtype="octet-stream", filename="synthetic.txt")
            files = [{"name": "synthetic.txt", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
        intent = {"schema": send_guard.INTENT_SCHEMA, "sender": "sender@example.test", "to": ["recipient@example.test"],
                  "cc": [], "bcc": [], "subject": "香港 café", "body": "Hello\n\nEnd\n", "attachments": files}
        return message, intent

    def snapshot(self, message):
        return {"account": "Work", "account_id": "account-1", "numeric_id": 42,
                "message_id": "fixture-1@example.test", "source_stable": True,
                "source": message.as_bytes(policy=policy.SMTP).decode("utf-8")}

    def inspect(self, message, intent, **kwargs):
        return draft_review.inspect_saved(self.snapshot(message), intent, mailctl.body_readback_comparison, **kwargs)

    def test_complete_evidence_still_requires_visual_and_human_review(self):
        message, intent = self.fixture(html=True)
        result = self.inspect(message, intent)
        self.assertEqual(result["status"], "awaiting_human_confirmation")
        self.assertTrue(result["attachment_bytes_verified"])
        self.assertTrue(result["envelope_verified"])
        self.assertEqual(result["body_comparison"], "exact")
        self.assertTrue(result["has_html"])
        for field in ("send_authorized", "send_attempted", "delivery_verified", "open_draft_link_verified", "html_rendering_verified", "mail_mutated"):
            self.assertIs(result[field], False)
        self.assertTrue(result["requires_human_confirmation"])
        self.assertTrue(result["requires_visual_review"])
        self.assertEqual(result["intent_digest"], send_guard.intent_digest(intent))
        self.assertNotIn("source", result)

    def test_attachment_name_size_hash_and_duplicates_are_exact(self):
        for change in ("name", "size_bytes", "sha256", "duplicate", "missing"):
            message, intent = self.fixture()
            if change == "duplicate":
                intent["attachments"].append(copy.deepcopy(intent["attachments"][0]))
            elif change == "missing":
                intent["attachments"] = []
            else:
                intent["attachments"][0][change] = 1 if change == "size_bytes" else "different"
            with self.subTest(change=change):
                result = self.inspect(message, intent)
                self.assertFalse(result["attachment_bytes_verified"])
                self.assertIn("attachments_mismatch_or_unverified", result["issues"])

    def test_envelope_fields_must_all_match(self):
        for field in ("sender", "to", "cc", "bcc", "subject"):
            message, intent = self.fixture()
            intent[field] = ["other@example.test"] if field in {"to", "cc", "bcc"} else "other"
            with self.subTest(field=field):
                result = self.inspect(message, intent)
                self.assertFalse(result["envelope_verified"])
                self.assertIn("envelope:" + field, result["issues"])

    def test_body_never_strips_leading_whitespace_signature_or_arbitrary_separators(self):
        for text, expected in (("Hello\n\u2028\nEnd\n", "mail_empty_paragraphs"),
                               ("\nHello\n\u2028\nEnd\n", "mismatch"),
                               ("Hello\n\nEnd\nsignature\n", "mismatch"),
                               ("Hello\u2028\n\nEnd\n", "mismatch")):
            message, intent = self.fixture(attachment=False)
            message.set_content(text, charset="utf-8")
            result = self.inspect(message, intent)
            self.assertEqual(result["body_comparison"], expected)
            self.assertEqual("body_requires_review" in result["issues"], expected == "mismatch")

    def test_missing_or_multiple_plain_parts_need_review(self):
        for multiple in (False, True):
            message, intent = self.fixture(attachment=False)
            if multiple:
                message.make_mixed()
                other = EmailMessage()
                other.set_content(intent["body"])
                message.attach(other)
            else:
                message.set_content("<p>Hello</p>", subtype="html")
            result = self.inspect(message, intent)
            self.assertIn("body_requires_review", result["issues"])

    def test_empty_plain_part_does_not_certify_or_discard_html_body(self):
        message, intent = self.fixture(attachment=False)
        message.set_content("")
        message.add_alternative("<p>Visible content is here</p>", subtype="html")
        result = self.inspect(message, intent)
        self.assertEqual(result["status"], "needs_content_review")
        self.assertTrue(result["has_html"])
        self.assertIn("body_requires_review", result["issues"])
        self.assertEqual(result["expected_body"], intent["body"])
        self.assertFalse(result["html_rendering_verified"])

    def test_alternate_routing_headers_and_opaque_content_are_not_approved(self):
        message, intent = self.fixture()
        message["Sender"] = "other@example.test"
        result = self.inspect(message, intent)
        self.assertIn("envelope:alternate_routing_headers", result["issues"])
        message, intent = self.fixture()
        part = message.get_payload()[-1]
        del part["Content-Disposition"]
        result = self.inspect(message, intent)
        self.assertFalse(result["attachment_bytes_verified"])
        self.assertIn("uninspectable_unnamed_part", result["issues"])

    def test_malformed_mime_fails_closed(self):
        for defect in ("base64", "duplicate_encoding", "duplicate_subject", "duplicate_from", "duplicate_id", "wrong_id", "unsafe_filename", "encrypted", "invalid_charset", "invalid_disposition"):
            message, intent = self.fixture()
            part = message.get_payload()[-1]
            snapshot = self.snapshot(message)
            source = snapshot["source"]
            if defect == "base64":
                part.set_payload("!!!!")
                snapshot = self.snapshot(message)
            elif defect == "duplicate_encoding":
                snapshot["source"] = source.replace("Content-Transfer-Encoding: base64", "Content-Transfer-Encoding: base64\r\nContent-Transfer-Encoding: 7bit")
            elif defect in {"duplicate_subject", "duplicate_from", "duplicate_id"}:
                header = {"duplicate_subject": "Subject: Another", "duplicate_from": "From: other@example.test", "duplicate_id": "Message-ID: <extra@example.test>"}[defect]
                snapshot["source"] = header + "\r\n" + source
            elif defect == "wrong_id":
                snapshot["message_id"] = "different@example.test"
            elif defect == "unsafe_filename":
                part.set_param("filename", "../file", header="Content-Disposition")
                snapshot = self.snapshot(message)
            elif defect == "encrypted":
                message.set_type("multipart/encrypted")
                snapshot = self.snapshot(message)
            elif defect == "invalid_charset":
                snapshot["source"] = source.replace('charset="utf-8"', 'charset="unsupported-charset"')
            else:
                snapshot["source"] = source.replace('filename="synthetic.txt"', 'filename="synthetic.txt"; filename="other.txt"')
            with self.subTest(defect=defect), self.assertRaises(draft_review.DraftReviewError):
                draft_review.inspect_saved(snapshot, intent, mailctl.body_readback_comparison)

    def test_snapshot_identity_limits_and_stale_fingerprints(self):
        message, intent = self.fixture()
        snapshot = self.snapshot(message)
        original = self.inspect(message, intent)
        self.assertEqual(self.inspect(message, intent, expected_review_digest=original["review_digest"])["review_digest"], original["review_digest"])
        for change in ({"numeric_id": 43}, {"account_id": "other"}, {"source": snapshot["source"] + "\r\n"}):
            with self.subTest(change=list(change)), self.assertRaises(draft_review.DraftReviewError):
                draft_review.inspect_saved({**snapshot, **change}, intent, mailctl.body_readback_comparison, expected_review_digest=original["review_digest"])
        for change in ({"numeric_id": True}, {"account_id": ""}, {"source_stable": False}, {"source": "\ud800"}):
            with self.subTest(change=list(change)), self.assertRaises(draft_review.DraftReviewError):
                draft_review.inspect_saved({**snapshot, **change}, intent, mailctl.body_readback_comparison)
        with mock.patch.object(draft_review, "MAX_SOURCE_BYTES", 8), self.assertRaises(draft_review.DraftReviewError):
            self.inspect(message, intent)
        with mock.patch.object(draft_review, "MAX_ATTACHMENT_BYTES", 1), self.assertRaises(draft_review.DraftReviewError):
            self.inspect(message, intent)

    def test_message_id_validation_keeps_case_and_rejects_controls(self):
        self.assertEqual(draft_review.message_id("ABC@example.test"), "<ABC@example.test>")
        for bad in ("a@b@c", "<a@b>extra", "a b@c", "a@b\x00", None):
            with self.subTest(value=bad), self.assertRaises(draft_review.DraftReviewError):
                draft_review.message_id(bad)

    def review_args(self, *extra):
        return mailctl.build_parser().parse_args(["draft", "review-saved", "fixture-1@example.test", "--account", "Work",
            "--sender", "sender@example.test", "--to", "recipient@example.test", "--subject", "香港 café", "--body", "Hello\n\nEnd\n", *extra])

    def test_cli_report_is_private_no_raw_source_and_never_reserves_a_send(self):
        message, _ = self.fixture(attachment=False)
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(mailctl, "run_backend", return_value={"data": self.snapshot(message)}) as backend, mock.patch.object(send_guard, "reserve_attempt") as reserve, mock.patch.object(operation_state, "begin") as begin:
            output = Path(temporary) / "review.json"
            result = mailctl.execute(self.review_args("--output", str(output)))
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(json.loads(output.read_text())["data"], result["data"])
            self.assertNotIn("source", result["data"])
            self.assertEqual(backend.call_args.args[0]["command"], "draft_saved_source")
            self.assertEqual(backend.call_count, 1)
            reserve.assert_not_called()
            begin.assert_not_called()
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(self.review_args("--output", str(output)))

    def test_cli_preflights_identity_and_expectations_before_backend(self):
        with mock.patch.object(mailctl, "run_backend") as backend:
            for change in ({"rfc_message_id": "bad"}, {"account": ""}, {"expected_review_digest": "yes"}, {"sender": None}):
                args = self.review_args()
                for key, value in change.items():
                    setattr(args, key, value)
                with self.subTest(change=change), self.assertRaises(mailctl.MailCtlError):
                    mailctl.execute(args)
            backend.assert_not_called()

    def test_cli_rejects_backend_scope_drift_and_stale_digest(self):
        message, _ = self.fixture(attachment=False)
        for change in ({"account": "Other"}, {"message_id": "wrong@example.test"}, {}):
            with mock.patch.object(mailctl, "run_backend", return_value={"data": {**self.snapshot(message), **change}}):
                with self.assertRaises(mailctl.MailCtlError):
                    mailctl.execute(self.review_args("--expected-review-digest", "0" * 64))

    def test_save_requires_revision_and_has_replay_safe_receipt(self):
        snapshot = {"draft_id": 7, "sender": "sender@example.test", "subject": "Synthetic", "content": "Body",
                    "to": ["recipient@example.test"], "cc": [], "bcc": [], "attachments": None, "visible": True}
        operation_id = str(uuid.uuid4())
        args = mailctl.build_parser().parse_args(["draft", "save", "7", "--expected-revision", operation_state.digest(snapshot), "--confirm-change", "--operation-id", operation_id])
        def backend(payload, timeout):
            if payload["command"] == "draft_read":
                return {"ok": True, "data": snapshot}
            self.assertEqual(payload["command"], "draft_save")
            self.assertEqual(payload["expected_snapshot"], snapshot)
            return {"ok": True, "data": {"draft": snapshot, "saved_copy_verified": False, "sent": False}}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"MAILCTL_STATE_DIR": temporary}), mock.patch.object(mailctl, "run_backend", side_effect=backend) as call:
            result = mailctl.execute(args)
            self.assertFalse(result["data"]["saved_copy_verified"])
            replay = mailctl.execute(args)
            self.assertTrue(replay["replayed_receipt"])
            self.assertEqual(call.call_count, 2)
            args.operation_id = str(uuid.uuid4())
            args.expected_revision = "0" * 64
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
            self.assertEqual(call.call_count, 3)  # Only the read, never the save.
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
            self.assertEqual(call.call_count, 3)

    def test_save_unknown_is_not_retried(self):
        args = mailctl.build_parser().parse_args(["draft", "save", "7", "--expected-revision", "0" * 64, "--confirm-change", "--operation-id", str(uuid.uuid4())])
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"MAILCTL_STATE_DIR": temporary}), mock.patch.object(mailctl, "run_backend", side_effect=mailctl.MailCtlError("uncertain")) as backend:
            for _ in range(2):
                with self.assertRaises(mailctl.MailCtlError):
                    mailctl.execute(args)
            self.assertEqual(backend.call_count, 1)
            self.assertEqual(operation_state.get(args.operation_id)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
