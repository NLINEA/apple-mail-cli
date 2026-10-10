"""0.5.1 regressions: private temporary artifacts and mocked Mail reads only."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_campaign
import campaign
import mailctl


class Patch051Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mailctl-051-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.manifest = test_campaign.CampaignTests().write_manifest(self.root)
        self.output = self.root / "rendered"

    def render(self, force=False):
        return campaign.render_campaign(str(self.manifest), str(self.output), force=force)

    def current_bytes(self):
        return {path.name: path.read_bytes() for path in self.output.iterdir() if path.is_file()}

    def test_force_preserves_unrelated_eml_and_private_backup_of_previous_render(self):
        original = self.render()
        saved = self.current_bytes()
        unrelated = self.output / "unrelated-export.eml"
        unrelated.write_bytes(b"Do not touch this independent export.")
        result = self.render(force=True)
        backup = Path(result["backup_directory"])
        self.assertEqual(unrelated.read_bytes(), b"Do not touch this independent export.")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(unrelated.name, [p.name for p in backup.iterdir()])
        for name, value in saved.items():
            self.assertEqual((backup / name).read_bytes(), value)
            self.assertEqual((backup / name).stat().st_mode & 0o777, 0o600)
        report = campaign.validate_manifest(str(self.manifest))
        campaign.verify_queue(report, result["queue_path"])
        self.assertEqual(original["queue_path"], result["queue_path"])

    def test_force_never_replaces_unowned_filename_collision(self):
        self.output.mkdir()
        collision = self.output / campaign.safe_filename(0, "row-0001")
        collision.write_bytes(b"Unrelated existing email with a matching filename")
        with self.assertRaisesRegex(campaign.CampaignError, "unowned"):
            self.render(force=True)
        self.assertEqual(collision.read_bytes(), b"Unrelated existing email with a matching filename")
        self.assertFalse((self.output / "queue.json").exists())

    def test_force_refuses_invalid_previous_metadata_without_changes(self):
        self.render()
        (self.output / "queue.json").write_text("invalid json")
        before = self.current_bytes()
        with self.assertRaises(campaign.CampaignError):
            self.render(force=True)
        self.assertEqual(self.current_bytes(), before)

    def test_force_refuses_queue_from_different_campaign(self):
        self.render()
        before = self.current_bytes()
        value = json.loads(self.manifest.read_text())
        value["campaign_id"] = "different-campaign"
        self.manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(campaign.CampaignError, "does not belong"):
            self.render(force=True)
        self.assertEqual(self.current_bytes(), before)

    def test_force_refuses_external_path_in_previous_queue(self):
        self.render()
        outside = self.root / "outside.eml"
        outside.write_bytes(b"outside file")
        queue = json.loads((self.output / "queue.json").read_text())
        queue["messages"][0]["eml_path"] = str(outside)
        (self.output / "queue.json").write_text(json.dumps(queue))
        before = self.current_bytes()
        with self.assertRaises(campaign.CampaignError):
            self.render(force=True)
        self.assertEqual(outside.read_bytes(), b"outside file")
        self.assertEqual(self.current_bytes(), before)

    def test_force_refuses_symlink_owned_eml(self):
        self.render()
        outside = self.root / "outside.eml"
        outside.write_bytes(b"outside file")
        target = self.output / campaign.safe_filename(0, "row-0001")
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaises(campaign.CampaignError):
            self.render(force=True)
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside file")

    def test_staging_failure_keeps_previous_render_untouched(self):
        self.render()
        before = self.current_bytes()
        original = campaign.render_staged_campaign
        def fail_after_staging(*args):
            original(*args)
            raise OSError("Simulated failure before publication")
        with mock.patch.object(campaign, "render_staged_campaign", side_effect=fail_after_staging):
            with self.assertRaises(OSError):
                self.render(force=True)
        self.assertEqual(self.current_bytes(), before)

    def test_publish_failure_restores_previous_render_and_retains_backup(self):
        self.render()
        before = self.current_bytes()
        value = json.loads(self.manifest.read_text())
        value["messages"][0]["body"] = "Changed body for the new render."
        self.manifest.write_text(json.dumps(value))
        original = os.replace
        failed = []
        def fail_once(source, destination):
            if Path(destination) == self.output / "queue.json" and not failed:
                failed.append(True)
                raise OSError("Simulated publication failure")
            return original(source, destination)
        with mock.patch.object(campaign.os, "replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.render(force=True)
        self.assertEqual(self.current_bytes(), before)
        self.assertEqual(len(list(self.output.glob(".mailctl-backup-*"))), 1)

    def test_removed_row_eml_is_retained_not_deleted(self):
        self.render()
        previous = self.output / campaign.safe_filename(0, "row-0001")
        value = json.loads(self.manifest.read_text())
        value["messages"][0]["key"] = "row-new"
        self.manifest.write_text(json.dumps(value))
        result = self.render(force=True)
        self.assertTrue(previous.is_file())
        self.assertEqual(result["retained_unreferenced_eml"], [str(previous)])
        self.assertEqual(json.loads((self.output / "queue.json").read_text())["messages"][0]["key"], "row-new")

    def test_plain_opt_out_requests_are_not_stripped_as_footers(self):
        cases = ["I would like to unsubscribe.", "We want to unsubscribe.", "I'd like to unsubscribe.",
                 "Please unsubscribe me.", "Unsubscribe", "Please remove us from your list.", "請停止寄送，謝謝。"]
        for body in cases:
            with self.subTest(body=body):
                result = mailctl.classify_message({"subject": "Re: Proposal", "content": body, "attachments": []})
                self.assertEqual(result["kind"], "unsubscribe_request")

    def test_positive_reply_with_quoted_opt_out_and_footer_stays_positive(self):
        for footer in ["To unsubscribe, click here.", "Click here to unsubscribe.", "> I would like to unsubscribe.", "On Monday, X wrote:\nI would like to unsubscribe."]:
            with self.subTest(footer=footer):
                result = mailctl.classify_message({"subject": "Re: Proposal", "content": "Interested, sounds good.\n\n" + footer, "attachments": []})
                self.assertEqual(result["kind"], "human_positive")

    def test_negated_opt_out_is_not_explicit_opt_out(self):
        for body in ["Please do not unsubscribe me.", "Don't unsubscribe me.", "I don't want to unsubscribe."]:
            with self.subTest(body=body):
                self.assertFalse(mailctl.explicit_opt_out(body))

    def test_dmarc_missing_and_malformed_attachments_require_review_not_crash(self):
        for attachments in [None, {}, [None], [{"name": None}], [{"size_bytes": 12}]]:
            with self.subTest(attachments=attachments):
                result = mailctl.classify_message({"subject": "DMARC report", "content": "Report attached", "attachments": attachments})
                self.assertTrue(result["requires_review"])
                self.assertTrue(result["attachment_metadata_unavailable"])

    def test_dmarc_can_use_text_or_valid_filename_evidence(self):
        result = mailctl.classify_message({"subject": "DMARC aggregate report", "attachments": None})
        self.assertEqual(result["kind"], "dmarc_report")
        self.assertTrue(result["requires_review"])
        result = mailctl.classify_message({"subject": "DMARC report", "attachments": [{"name": "report.XML"}]})
        self.assertEqual(result["kind"], "dmarc_report")
        self.assertFalse(result["attachment_metadata_unavailable"])

    def triage_args(self, output=None):
        return mailctl.build_parser().parse_args(["triage", "--limit", "3", "--batch-size", "1"] + (["--output", str(output)] if output else []))

    def listing(self):
        return {"ok": True, "data": {"messages": [{"id": 1}, {"id": 2}, {"id": 3}], "mailbox": "INBOX", "scanned": 3, "truncated": False}}

    def message(self, id):
        return {"ok": True, "data": {"id": id, "subject": "Re: Proposal", "content": "Private body: Interested!", "attachments": [], "sender": "person@example.com"}}

    def test_per_message_failure_preserves_successes_and_writes_private_partial_artifact(self):
        output = self.root / "partial.json"
        responses = [self.listing(), self.message(1), mailctl.MailCtlError("secret body in error", code="mail_command_failed"), self.message(3)]
        with mock.patch.object(mailctl, "run_backend", side_effect=responses):
            result = mailctl.triage_mailbox(self.triage_args(output))
        self.assertFalse(result["ok"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["partial"])
        self.assertEqual([item["id"] for item in result["items"]], [1, 3])
        self.assertEqual(result["errors"][0]["id"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["attempted"], 3)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        for private in ["Private body", "secret body"]:
            self.assertNotIn(private, output.read_text())
        with self.assertRaisesRegex(campaign.CampaignError, "incomplete"):
            campaign.load_triage_summary(str(output), {"person@example.com"})

    def test_permission_denial_stops_further_reads_but_returns_completed_results(self):
        responses = [self.listing(), self.message(1), mailctl.MailCtlError("Permission denied", code="mail_permission_denied")]
        with mock.patch.object(mailctl, "run_backend", side_effect=responses) as backend:
            result = mailctl.triage_mailbox(self.triage_args())
        self.assertEqual(backend.call_count, 3)
        self.assertEqual(result["returned"], 1)
        self.assertTrue(result["stopped_early"])
        self.assertEqual(result["not_attempted_ids"], [3])

    def test_classifier_exception_isolated_to_one_message(self):
        original = mailctl.classify_message
        def classify(message):
            if message["id"] == 2:
                raise TypeError("Unexpected fixture")
            return original(message)
        with mock.patch.object(mailctl, "run_backend", side_effect=[self.listing(), *[self.message(i) for i in (1, 2, 3)]]), mock.patch.object(mailctl, "classify_message", side_effect=classify):
            result = mailctl.triage_mailbox(self.triage_args())
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["errors"][0]["code"], "message_classification_failed")

    def test_successful_bounded_triage_distinguishes_truncation_from_failure(self):
        listing = self.listing()
        listing["data"]["truncated"] = True
        with mock.patch.object(mailctl, "run_backend", side_effect=[listing, *[self.message(i) for i in (1, 2, 3)]]):
            result = mailctl.triage_mailbox(self.triage_args())
        self.assertTrue(result["ok"])
        self.assertTrue(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
