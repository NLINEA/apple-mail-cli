"""Frozen independent-review regressions; synthetic fixtures and mocks only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/apple-mail-cli/scripts"))
import campaign
import mail_index
import mailctl
import ubd_connector
import test_campaign
import test_mail_index
import test_ubd_connector


class ReviewBodyPropagationTests(unittest.TestCase):
    def test_body_access_error_blocks_classify_and_triage(self):
        failure = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "command": "read", "ok": False,
            "error": {"message": "synthetic body access denied"},
        }), stderr="")
        listing = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "command": "list", "ok": True, "data": {"messages": [{"id": 42}]},
        }), stderr="")
        for argv, responses in ((["classify", "42"], [failure]),
                                (["triage", "--limit", "1"], [listing, failure])):
            with self.subTest(command=argv[0]), mock.patch.object(mailctl.shutil, "which", return_value="synthetic-osascript"), mock.patch.object(mailctl.subprocess, "run", side_effect=responses), mock.patch.object(mailctl, "classify_message") as classify:
                with self.assertRaises(mailctl.MailCtlError) as caught:
                    mailctl.execute(mailctl.build_parser().parse_args(argv))
                self.assertEqual(caught.exception.code, "mail_command_failed")
                classify.assert_not_called()


class ReviewCampaignTests(unittest.TestCase):
    def manifest(self, root, **overrides):
        return test_campaign.CampaignTests.write_manifest(self, root, **overrides)

    def row(self, **overrides):
        value = dict(row_id=1, message_id="<good@example.test>", subject="A complete subject",
                     state="sent", to=["recipient@example.com"], cc=["copy@example.com"],
                     bcc=["private-copy@example.com"], sender_addresses=["sender@example.com"],
                     attachments=[], attachment_count=0, scheduled_at=None)
        value.update(overrides)
        return value

    def audit(self, manifest, rows, evidence=None):
        evidence = evidence or {}
        store = {"evidence": evidence, "diagnostics": {"evidence_loaded": len(evidence)}}
        with mock.patch.object(campaign, "list_messages", return_value=(manifest.parent / "synthetic-index", rows)), mock.patch.object(campaign, "load_send_later_evidence", return_value=store):
            return campaign.audit_campaign(str(manifest))

    def test_exact_copy_never_hides_recipient_drift(self):
        variants = [
            {"to": ["recipient@example.com", "second@example.com", "extra@example.com"]},
            {"to": ["recipient@example.com"]},
            {"to": ["second@example.com"], "cc": ["copy@example.com", "recipient@example.com"]},
            {"cc": ["copy@example.com", "extra@example.com"]},
            {"cc": []},
            {"cc": [], "bcc": ["private-copy@example.com", "copy@example.com"]},
            {"bcc": ["private-copy@example.com", "extra@example.com"]},
            {"bcc": []},
            {"bcc": [], "cc": ["copy@example.com", "private-copy@example.com"]},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary), to=["recipient@example.com", "second@example.com"])
            good = self.row(to=["recipient@example.com", "second@example.com"])
            for fields in variants:
                wrong = {**good, "row_id": 2, "message_id": "<wrong@example.test>", "state": "scheduled", "scheduled_at": "2026-08-19T01:00:00+00:00", **fields}
                for rows in ([good, wrong], [wrong, good]):
                    with self.subTest(fields=fields, reverse=rows[0] is wrong):
                        result = self.audit(manifest, rows)
                        item = result["results"][0]
                        self.assertFalse(result["complete"])
                        self.assertFalse(result["safe_to_resume_missing"])
                        self.assertEqual(item["status"], "duplicate")
                        self.assertIn("wrong_recipients", item["issues"])
                        self.assertEqual(item["distinct_message_ids"], ["<good@example.test>", "<wrong@example.test>"])
                        self.assertEqual(len(item["matches"]), 2)

    def test_exact_scheduled_and_conflicting_sent_are_also_unsafe(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary))
            good = self.row(state="scheduled", scheduled_at="2026-08-19T01:00:00+00:00")
            wrong = self.row(row_id=2, message_id="<wrong@example.test>", cc=[])
            result = self.audit(manifest, [good, wrong])
        self.assertFalse(result["safe_to_resume_missing"])
        self.assertEqual(result["results"][0]["status"], "duplicate")
        self.assertIn("wrong_recipients", result["results"][0]["issues"])

    def test_legitimate_replicas_and_unrelated_same_subject_stay_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary))
            good = self.row()
            replica = {**good, "row_id": 2}
            unrelated = self.row(row_id=3, message_id="<unrelated@example.test>", to=["another@example.com"])
            result = self.audit(manifest, [replica, good, unrelated])
        self.assertTrue(result["complete"])
        self.assertTrue(result["safe_to_resume_missing"])
        self.assertEqual(result["results"][0]["distinct_message_ids"], ["<good@example.test>"])
        self.assertEqual(result["results"][0]["issues"], [])

    def test_conflict_blocks_queue_and_session_before_private_state_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest(root)
            queue = campaign.render_campaign(str(manifest), str(root / "rendered"))["queue_path"]
            good = self.row()
            wrong = self.row(row_id=2, message_id="<wrong@example.test>", state="scheduled", to=["recipient@example.com", "extra@example.com"])
            with mock.patch.object(campaign, "list_messages", return_value=(root / "index", [good, wrong])), mock.patch.object(campaign, "load_send_later_evidence", return_value={"evidence": {}, "diagnostics": {}}):
                with self.assertRaises(campaign.CampaignError):
                    campaign.next_batch(str(manifest), queue)
                with self.assertRaises(campaign.CampaignError):
                    campaign.start_schedule_session(str(manifest), str(root / "session.json"), action="schedule", queue_path=queue)
            self.assertFalse((root / "session.json").exists())

    def test_scheduled_names_and_count_never_replace_byte_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attachment = root / "proposal.pdf"
            attachment.write_bytes(b"approved bytes")
            manifest = self.manifest(root, attachments=[{"path": str(attachment)}])
            row = self.row(state="scheduled", scheduled_at="2026-08-19T01:00:00+00:00", attachments=[attachment.name], attachment_count=1)
            audit = self.audit(manifest, [row])
        self.assertFalse(audit["complete"])
        self.assertFalse(audit["safe_to_resume_missing"])
        item = audit["results"][0]
        self.assertIn("attachment_unverified", item["issues"])
        self.assertEqual(item["verification"]["attachments"]["status"], "unverified")
        self.assertFalse(item["verification"]["attachments"]["sha256_checked"])

    def test_scheduled_exact_bytes_pass_and_size_or_hash_drift_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attachment = root / "proposal.pdf"
            attachment.write_bytes(b"approved bytes")
            manifest = self.manifest(root, attachments=[{"path": str(attachment)}])
            row = self.row(state="scheduled", scheduled_at="2026-08-19T01:00:00+00:00", attachments=[attachment.name], attachment_count=1)
            descriptor = {"name": attachment.name, "size_bytes": attachment.stat().st_size, "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest()}
            for drift in ({}, {"size_bytes": 999}, {"sha256": "0" * 64}):
                with self.subTest(drift=drift):
                    evidence = {row["message_id"]: {"sender_addresses": ["sender@example.com"], "attachments": [{**descriptor, **drift}]}}
                    result = self.audit(manifest, [row], evidence)
                    verification = result["results"][0]["verification"]["attachments"]
                    self.assertEqual(result["safe_to_resume_missing"], not bool(drift))
                    self.assertTrue(verification["sha256_checked"])
                    if drift:
                        self.assertIn("wrong_attachment", result["results"][0]["issues"])
                        self.assertIn(next(iter(drift)), verification["mismatch_fields"])

    def test_sent_attachment_assurance_is_explicitly_metadata_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attachment = root / "proposal.pdf"
            attachment.write_bytes(b"approved bytes")
            manifest = self.manifest(root, attachments=[{"path": str(attachment)}])
            audit = self.audit(manifest, [self.row(attachments=[attachment.name], attachment_count=1)])
        self.assertTrue(audit["complete"])
        self.assertEqual(audit["results"][0]["verification"]["attachments"]["status"], "metadata_matched")
        self.assertFalse(audit["results"][0]["verification"]["attachments"]["sha256_checked"])

    def test_empty_scheduled_attachments_need_readable_absence_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary))
            row = self.row(state="scheduled", scheduled_at="2026-08-19T01:00:00+00:00")
            missing = self.audit(manifest, [row])
            proved = self.audit(manifest, [row], {row["message_id"]: {"sender_addresses": ["sender@example.com"], "attachments": []}})
        self.assertFalse(missing["safe_to_resume_missing"])
        self.assertIn("attachment_unverified", missing["results"][0]["issues"])
        self.assertTrue(proved["complete"])
        self.assertTrue(proved["safe_to_resume_missing"])


class ReviewMailboxTests(unittest.TestCase):
    def test_custom_paths_never_claim_a_special_role(self):
        for url in ("imap://account/Projects/Sentiment", "imap://account/SentArchive",
                    "imap://account/Projects/Sent", "imap://account/Projects/SendLater",
                    "imap://account/SendLaterNotes", "imap://account/INBOX-Old",
                    "imap://account/DraftsArchive", "imap://account/TrashBackup",
                    "imap://account/Projects%2FSent", "imap://account/Sent/Archive"):
            with self.subTest(url=url):
                self.assertEqual(mail_index.mailbox_state(url), "other")

    def test_documented_root_role_fixtures(self):
        for path, state in {"INBOX": "inbox", "In": "inbox", "Sent": "sent", "Sent%20Messages": "sent", "SendLater": "scheduled", "Drafts": "draft", "Trash": "trash", "Junk": "junk", "Outbox": "outbox"}.items():
            with self.subTest(path=path):
                self.assertEqual(mail_index.mailbox_state("imap://account/" + path), state)

    def test_sql_filters_and_audit_share_conservative_classification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "Envelope Index"
            test_mail_index.MailIndexTests.create_index_fixture(self, path)
            manifest = test_campaign.CampaignTests.write_manifest(self, root, subject="Alpha proposal", cc=[], bcc=[])
            for folder in ("Projects/Sentiment", "Projects/Sent", "Sent"):
                with self.subTest(folder=folder):
                    with sqlite3.connect(path) as db:
                        db.execute("UPDATE mailboxes SET url=?", ("imap://synthetic-account/" + folder,))
                    listed = mail_index.list_messages(database=str(path), states={"sent"})[1]
                    searched = mail_index.search_messages(database=str(path), states={"sent"})["messages"]
                    with mock.patch.object(campaign, "load_send_later_evidence", return_value={"evidence": {}, "diagnostics": {}}):
                        audit = campaign.audit_campaign(str(manifest), database=str(path))
                    if folder == "Sent":
                        self.assertEqual(len(listed), 3)
                        self.assertEqual(len(searched), 3)
                        self.assertTrue(audit["complete"])
                        self.assertTrue(audit["safe_to_resume_missing"])
                    else:
                        self.assertEqual(listed, [])
                        self.assertEqual(searched, [])
                        self.assertFalse(audit["complete"])
                        self.assertFalse(audit["safe_to_resume_missing"])
                        self.assertIn("mailbox_unverified", audit["results"][0]["issues"])

    def test_mailbox_unverified_is_a_redacted_supported_issue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = test_campaign.CampaignTests.write_manifest(self, root)
            audit = test_ubd_connector.UbdConnectorTests.campaign_audit(self)
            audit["summary"]["issues"] = {"mailbox_unverified": 1}
            audit["safe_to_resume_missing"] = False
            audit["results"][0]["mailbox_url"] = "imap://synthetic-account/Projects/Sentiment"
            event = ubd_connector.campaign_audit_event(str(manifest), audit, business_unit_id=test_ubd_connector.BUSINESS_UNIT_ID, observed_at="2026-10-01T00:00:00Z")
        self.assertEqual(event["event"]["payload"]["issue_counts"]["mailbox_unverified"], 1)
        serialized = json.dumps(event)
        for value in ("private@example.com", "Private subject", "private-message-id", "Sentiment"):
            self.assertNotIn(value, serialized)
