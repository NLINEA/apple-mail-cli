from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "apple-mail-cli" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import campaign  # noqa: E402


class CampaignTests(unittest.TestCase):
    def write_manifest(self, directory: Path, **message_overrides: object) -> Path:
        message = {
            "key": "row-0001",
            "company": "Example Company",
            "to": ["recipient@example.com"],
            "cc": ["copy@example.com"],
            "bcc": ["private-copy@example.com"],
            "subject": "A complete subject",
            "body": "Hello,\n\nA complete body.\n",
            "schedule_at": "2026-08-19T09:00:00+08:00",
        }
        message.update(message_overrides)
        path = directory / "campaign.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "campaign_id": "test-campaign-2026",
                    "sender": "sender@example.com",
                    "policy": {"minimum_gap_minutes": 5, "daily_maximum": 10},
                    "messages": [message],
                }
            ),
            encoding="utf-8",
        )
        return path

    def missing_audit(self) -> dict:
        return {
            "ok": True,
            "safe_to_resume_missing": True,
            "campaign_id": "test-campaign-2026",
            "summary": {"active": 1, "statuses": {"missing": 1}, "issues": {}},
            "results": [{"key": "row-0001", "status": "missing"}],
        }

    def test_validation_rejects_header_injection_bad_sender_and_bad_policy_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, subject="Hello\nBcc: leak@example.com", sender="invalid")
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["policy"]["minimum_gap_minutes"] = "not-an-integer"
            value["policy"]["excluded_risks"] = "not-an-array"
            manifest.write_text(json.dumps(value), encoding="utf-8")

            report = campaign.validate_manifest(str(manifest))

        self.assertFalse(report["ok"])
        joined = "\n".join(report["errors"])
        self.assertIn("subject cannot contain control characters", joined)
        self.assertIn("sender must be a valid email address", joined)
        self.assertIn("minimum_gap_minutes must be an integer", joined)
        self.assertIn("excluded_risks must be an array", joined)

    def test_rendered_queue_is_private_bound_to_manifest_and_eml_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root)
            output = root / "rendered"
            result = campaign.render_campaign(str(manifest), str(output))
            queue_path = Path(result["queue_path"])
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            row = queue["messages"][0]
            eml_path = Path(row["eml_path"])

            self.assertEqual(queue["schema_version"], campaign.QUEUE_SCHEMA_VERSION)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for artifact in [queue_path, Path(result["checkpoint_path"]), eml_path]:
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            self.assertEqual(row["bcc"], ["private-copy@example.com"])
            rendered_text = eml_path.read_text(encoding="utf-8")
            self.assertNotIn("Bcc:", rendered_text)
            self.assertIn("Cc: copy@example.com", rendered_text)

            with mock.patch.object(campaign, "audit_campaign", return_value=self.missing_audit()):
                batch = campaign.next_batch(str(manifest), str(queue_path))
            self.assertEqual(batch["batch_size"], 1)
            checkpoint = json.loads(Path(result["checkpoint_path"]).read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["pending_keys"], ["row-0001"])
            self.assertEqual(checkpoint["completed_keys"], [])
            self.assertIsNotNone(checkpoint["last_audit"])

            eml_path.write_bytes(eml_path.read_bytes() + b"tampered")
            with mock.patch.object(campaign, "audit_campaign", return_value=self.missing_audit()):
                with self.assertRaisesRegex(campaign.CampaignError, "integrity check failed"):
                    campaign.next_batch(str(manifest), str(queue_path))

            campaign.render_campaign(str(manifest), str(output), force=True)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["messages"][0]["body"] = "Changed after rendering."
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(campaign, "audit_campaign", return_value=self.missing_audit()):
                with self.assertRaisesRegex(campaign.CampaignError, "manifest changed"):
                    campaign.next_batch(str(manifest), str(output / "queue.json"))

    def test_audit_fails_closed_on_unverified_sender_and_extra_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.write_manifest(Path(temporary), cc=[], bcc=[])
            row = {
                "message_id": "<one@example.test>",
                "state": "sent",
                "to": ["recipient@example.com"],
                "subject": "A complete subject",
                "scheduled_at": None,
                "sender_addresses": [],
                "attachments": ["unexpected.pdf"],
                "attachment_count": 1,
            }
            with mock.patch.object(campaign, "list_messages", return_value=(Path("/tmp/index"), [row])):
                audit = campaign.audit_campaign(str(manifest))

        self.assertEqual(audit["results"][0]["status"], "sent")
        self.assertEqual(audit["results"][0]["issues"], ["sender_unverified", "wrong_attachment"])
        self.assertFalse(audit["safe_to_resume_missing"])

    def test_scheduled_audit_uses_emlx_sender_and_attachment_hash_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attachment = root / "proposal.pdf"
            attachment.write_bytes(b"verified attachment payload")
            manifest = self.write_manifest(
                root,
                cc=[],
                bcc=[],
                attachments=[{"path": str(attachment)}],
            )
            message = EmailMessage()
            message["From"] = "Sender <sender@example.com>"
            message["To"] = "recipient@example.com"
            message["Subject"] = "A complete subject"
            message["Message-ID"] = "<scheduled@example.test>"
            message.set_content("A complete body.")
            message.add_attachment(
                attachment.read_bytes(),
                maintype="application",
                subtype="pdf",
                filename=attachment.name,
            )
            message_bytes = message.as_bytes(policy=policy.SMTP)
            emlx = root / "V10" / "ACCOUNT" / "SendLater.mbox" / "STORE" / "Data" / "Messages" / "1.emlx"
            emlx.parent.mkdir(parents=True)
            emlx.write_bytes(str(len(message_bytes)).encode("ascii") + b"\n" + message_bytes)
            index = root / "V10" / "MailData" / "Envelope Index"
            row = {
                "message_id": "<scheduled@example.test>",
                "state": "scheduled",
                "to": ["recipient@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "A complete subject",
                "scheduled_at": "2026-08-19T09:00:00+08:00",
                "sender_addresses": [],
                "attachments": [],
                "attachment_count": 0,
            }
            with mock.patch.object(campaign, "list_messages", return_value=(index, [row])):
                audit = campaign.audit_campaign(str(manifest))

        result = audit["results"][0]
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["verification"]["sender"], {"source": "send_later_emlx", "status": "matched"})
        self.assertEqual(
            result["verification"]["attachments"],
            {"source": "send_later_emlx", "status": "matched", "sha256_checked": True, "mismatch_fields": []},
        )
        self.assertTrue(audit["safe_to_resume_missing"])

    def test_scheduled_audit_explains_attachment_mismatch_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = root / "proposal.pdf"
            expected.write_bytes(b"expected payload")
            manifest = self.write_manifest(root, cc=[], bcc=[], attachments=[{"path": str(expected)}])
            message = EmailMessage()
            message["From"] = "sender@example.com"
            message["To"] = "recipient@example.com"
            message["Subject"] = "A complete subject"
            message["Message-ID"] = "<scheduled@example.test>"
            message.set_content("Body")
            message.add_attachment(b"different payload", maintype="application", subtype="pdf", filename="proposal.pdf")
            message_bytes = message.as_bytes(policy=policy.SMTP)
            emlx = root / "V10" / "ACCOUNT" / "SendLater.mbox" / "STORE" / "Data" / "Messages" / "1.emlx"
            emlx.parent.mkdir(parents=True)
            emlx.write_bytes(str(len(message_bytes)).encode("ascii") + b"\n" + message_bytes)
            row = {
                "message_id": "<scheduled@example.test>",
                "state": "scheduled",
                "to": ["recipient@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "A complete subject",
                "scheduled_at": "2026-08-19T09:00:00+08:00",
                "sender_addresses": [],
                "attachments": [],
                "attachment_count": 0,
            }
            index = root / "V10" / "MailData" / "Envelope Index"
            with mock.patch.object(campaign, "list_messages", return_value=(index, [row])):
                audit = campaign.audit_campaign(str(manifest))

        verification = audit["results"][0]["verification"]["attachments"]
        self.assertEqual(verification["status"], "mismatched")
        self.assertEqual(verification["mismatch_fields"], ["size_bytes", "sha256"])

    def test_scheduled_audit_distinguishes_missing_attachment_evidence_from_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, cc=[], bcc=[])
            row = {
                "message_id": "<scheduled@example.test>",
                "state": "scheduled",
                "to": ["recipient@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "A complete subject",
                "scheduled_at": "2026-08-19T09:00:00+08:00",
                "sender_addresses": ["sender@example.com"],
                "attachments": [],
                "attachment_count": 0,
            }
            index = root / "V10" / "MailData" / "Envelope Index"
            with mock.patch.object(campaign, "list_messages", return_value=(index, [row])):
                audit = campaign.audit_campaign(str(manifest))

        self.assertEqual(audit["results"][0]["issues"], ["attachment_unverified"])
        self.assertNotIn("wrong_attachment", audit["results"][0]["issues"])
        self.assertFalse(audit["safe_to_resume_missing"])

    def test_audit_refuses_a_truncated_100000_row_scan(self) -> None:
        class BoundedRows:
            def __len__(self) -> int:
                return 100_000

        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.write_manifest(Path(temporary))
            with mock.patch.object(campaign, "list_messages", return_value=(Path("/tmp/index"), BoundedRows())):
                with self.assertRaisesRegex(campaign.CampaignError, "100,000-row"):
                    campaign.audit_campaign(str(manifest))

    def test_policy_timezone_controls_daily_counts_and_allowed_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, schedule_at="2026-08-18T23:30:00+00:00")
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["policy"].update({"timezone": "Asia/Hong_Kong", "allowed_hours": ["07:00", "09:00"]})
            value["messages"].append(
                {
                    **value["messages"][0],
                    "key": "row-0002",
                    "to": ["second@example.com"],
                    "schedule_at": "2026-08-19T00:30:00+00:00",
                }
            )
            manifest.write_text(json.dumps(value), encoding="utf-8")
            report = campaign.validate_manifest(str(manifest), require_schedule=True)

        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["timezone"], "Asia/Hong_Kong")
        self.assertEqual(report["summary"]["by_day"], {"2026-08-19": 2})

    def test_audit_rejects_extra_or_wrong_recipient_sets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.write_manifest(Path(temporary))
            row = {
                "message_id": "<one@example.test>",
                "state": "scheduled",
                "to": ["recipient@example.com", "extra@example.com"],
                "cc": ["copy@example.com"],
                "bcc": ["private-copy@example.com"],
                "subject": "A complete subject",
                "scheduled_at": "2026-08-19T09:00:00+08:00",
                "sender_addresses": ["sender@example.com"],
                "attachments": [],
                "attachment_count": 0,
            }
            with mock.patch.object(campaign, "list_messages", return_value=(Path("/tmp/index"), [row])):
                audit = campaign.audit_campaign(str(manifest))

        self.assertEqual(audit["results"][0]["status"], "missing")
        self.assertIn("wrong_recipients", audit["results"][0]["issues"])
        self.assertFalse(audit["safe_to_resume_missing"])

    def test_rebalance_preserves_safe_slots_and_moves_only_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["policy"].update(
                {
                    "timezone": "Asia/Hong_Kong",
                    "minimum_gap_minutes": 10,
                    "daily_maximum": 3,
                    "allowed_hours": ["09:00", "10:00"],
                }
            )
            value["messages"] = [
                {**value["messages"][0], "key": "one", "to": ["one@example.com"], "schedule_at": "2026-08-19T09:00:00+08:00"},
                {**value["messages"][0], "key": "two", "to": ["two@example.com"], "schedule_at": "2026-08-21T09:00:00+08:00"},
                {**value["messages"][0], "key": "three", "to": ["three@example.com"], "schedule_at": "2026-08-19T09:10:00+08:00"},
            ]
            manifest.write_text(json.dumps(value), encoding="utf-8")
            output = root / "rebalanced.json"
            result = campaign.rebalance_campaign(
                str(manifest),
                str(output),
                start_at="2026-08-19",
                finish_by="2026-08-19",
                include_account_volume=False,
            )
            generated = json.loads(output.read_text(encoding="utf-8"))

        schedules = {item["key"]: item["schedule_at"] for item in generated["messages"]}
        self.assertEqual(result["preserved"], 2)
        self.assertEqual(result["moved"], 1)
        self.assertEqual(schedules["one"], "2026-08-19T09:00:00+08:00")
        self.assertEqual(schedules["three"], "2026-08-19T09:10:00+08:00")
        self.assertEqual(schedules["two"], "2026-08-19T09:20:00+08:00")

    def test_rebalance_counts_existing_account_volume_and_time_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root, cc=[], bcc=[])
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["policy"].update(
                {
                    "timezone": "Asia/Hong_Kong",
                    "minimum_gap_minutes": 10,
                    "daily_maximum": 2,
                    "allowed_hours": ["09:00", "10:00"],
                }
            )
            manifest.write_text(json.dumps(value), encoding="utf-8")
            external = {
                "message_id": "<external@example.test>",
                "state": "scheduled",
                "to": ["other@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "Another campaign",
                "scheduled_at": "2026-08-19T09:00:00+08:00",
                "date_sent": None,
                "date_received": None,
                "sender_addresses": ["sender@example.com"],
                "attachments": [],
                "attachment_count": 0,
            }
            output = root / "quota-aware.json"
            with mock.patch.object(campaign, "list_messages", return_value=(Path("/tmp/index"), [external])):
                result = campaign.rebalance_campaign(
                    str(manifest),
                    str(output),
                    start_at="2026-08-19",
                    finish_by="2026-08-19",
                )
            generated = json.loads(output.read_text(encoding="utf-8"))

        self.assertTrue(result["account_volume_included"])
        self.assertEqual(generated["messages"][0]["schedule_at"], "2026-08-19T09:10:00+08:00")

    def test_rebalance_handles_1500_message_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["policy"].update(
                {
                    "timezone": "Asia/Hong_Kong",
                    "minimum_gap_minutes": 1,
                    "daily_maximum": 600,
                    "allowed_hours": ["00:00", "23:59"],
                }
            )
            base = datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))
            value["messages"] = [
                {
                    **value["messages"][0],
                    "key": f"row-{index:04d}",
                    "to": [f"recipient-{index}@example.com"],
                    "schedule_at": (base + timedelta(minutes=index)).isoformat(),
                }
                for index in range(1500)
            ]
            manifest.write_text(json.dumps(value), encoding="utf-8")
            output = root / "stress.json"
            result = campaign.rebalance_campaign(
                str(manifest),
                str(output),
                start_at="2026-08-19",
                finish_by="2026-08-21",
                include_account_volume=False,
            )

        self.assertEqual(result["moved"], 1500)
        self.assertEqual(sum(result["by_day"].values()), 1500)
        self.assertLessEqual(max(result["by_day"].values()), 600)

    def test_resumable_session_requires_post_ui_audit_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_manifest(root)
            session_path = root / "session.json"
            scheduled = {
                **self.missing_audit(),
                "results": [
                    {
                        "key": "row-0001",
                        "company": "Example Company",
                        "subject": "A complete subject",
                        "expected_schedule": "2026-08-19T09:00:00+08:00",
                        "status": "scheduled",
                        "issues": [],
                        "matches": [{"scheduled_at": "2026-08-18T09:00:00+08:00", "state": "scheduled"}],
                    }
                ],
            }
            verified = {
                **scheduled,
                "summary": {"active": 1, "statuses": {"scheduled": 1}, "issues": {}},
                "results": [{**scheduled["results"][0], "matches": [], "status": "scheduled"}],
            }
            with mock.patch.object(campaign, "audit_campaign", return_value=scheduled):
                campaign.start_schedule_session(str(manifest), str(session_path), action="reschedule")
            campaign.record_schedule_session(str(session_path), key="row-0001", result="applied")
            self.assertEqual(campaign.session_status(str(session_path))["statuses"], {"applied_unverified": 1})
            with mock.patch.object(campaign, "audit_campaign", return_value=verified):
                campaign.reconcile_schedule_session(str(session_path))

            status = campaign.session_status(str(session_path))
        self.assertTrue(status["complete"])
        self.assertEqual(status["statuses"], {"verified": 1})


if __name__ == "__main__":
    unittest.main()
