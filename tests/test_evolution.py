"""Regression coverage for the 0.5 audit; never mutates real Mail or UBD."""
import argparse
import copy
import hashlib
import json
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import test_campaign
import test_mail_index
import test_mail_store
import mailctl
import campaign
import mail_index
import mail_store
import operation_state
import send_guard


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.dict(os.environ, {"MAILCTL_STATE_DIR": str(self.root / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        guard = mock.patch.object(send_guard, "state_root", return_value=self.root / "send-attempts")
        guard.start()
        self.addCleanup(guard.stop)

    def manifest(self, **changes):
        return test_campaign.CampaignTests().write_manifest(self.root, **changes)

    def missing(self):
        audit = test_campaign.CampaignTests().missing_audit()
        audit["results"][0]["issues"] = []
        return audit

    def session(self):
        manifest = self.manifest()
        rendered = campaign.render_campaign(str(manifest), str(self.root / "rendered"))
        session = self.root / "session.json"
        with mock.patch.object(campaign, "audit_campaign", return_value=self.missing()):
            campaign.start_schedule_session(str(manifest), str(session), action="schedule", queue_path=rendered["queue_path"])
        return manifest, Path(rendered["queue_path"]), session

    def row(self, **changes):
        row = {"message_id": "<one@test>", "row_id": 1, "state": "sent", "subject": "A complete subject",
               "to": ["recipient@example.com"], "cc": [], "bcc": [], "sender_addresses": ["sender@example.com"],
               "attachments": [], "attachment_count": 0, "scheduled_at": None, "deleted": False}
        row.update(changes)
        return row

    def test_queue_actual_bcc_cannot_be_changed_behind_original_digest(self):
        manifest, queue_path, _ = self.session()
        queue = json.loads(queue_path.read_text())
        queue["messages"][0]["bcc"] = ["unexpected@example.com"]
        queue_path.write_text(json.dumps(queue))
        with mock.patch.object(campaign, "audit_campaign", return_value=self.missing()):
            with self.assertRaises(campaign.CampaignError):
                campaign.next_batch(str(manifest), str(queue_path))

    def test_session_rechecks_queue_and_eml_after_resume(self):
        _, queue_path, session = self.session()
        queue = json.loads(queue_path.read_text())
        eml = Path(queue["messages"][0]["eml_path"])
        eml.write_bytes(eml.read_bytes() + b"tampered")
        with mock.patch.object(campaign, "audit_campaign", return_value=self.missing()):
            with self.assertRaises(campaign.CampaignError):
                campaign.next_session_operations(str(session))

    def test_session_blocks_new_recipient_issue(self):
        _, _, session = self.session()
        audit = self.missing()
        audit["results"][0]["issues"] = ["wrong_recipients"]
        with mock.patch.object(campaign, "audit_campaign", return_value=audit):
            with self.assertRaises(campaign.CampaignError):
                campaign.next_session_operations(str(session))

    def test_two_sessions_cannot_claim_same_mail_and_wrong_token_is_refused(self):
        _, _, session = self.session()
        second = self.root / "second.json"
        second.write_bytes(session.read_bytes())
        with mock.patch.object(campaign, "audit_campaign", return_value=self.missing()):
            first = campaign.next_session_operations(str(session), owner="agent-one")
            other = campaign.next_session_operations(str(second), owner="agent-two")
        self.assertEqual(first["batch_size"], 1)
        self.assertEqual(other["batch_size"], 0)
        with self.assertRaises(campaign.CampaignError):
            campaign.record_schedule_session(str(session), key="row-0001", result="applied", claim_token="wrong")
        self.assertEqual(campaign.session_status(str(session))["active_claims"]["row-0001"]["owner"], "agent-one")

    def test_global_claim_is_cross_process_atomic(self):
        source = "import operation_state,uuid; print(operation_state.claim('same-mail','worker',str(uuid.uuid4()),'session'))"
        env = {**os.environ, "PYTHONPATH": str(mailctl.SCRIPT_DIR)}
        workers = [subprocess.Popen([sys.executable, "-c", source], stdout=subprocess.PIPE, text=True, env=env) for _ in range(4)]
        output = [p.communicate(timeout=10)[0].strip() for p in workers]
        self.assertEqual(output.count("True"), 1)

    def test_metadata_names_are_not_sha256_verification(self):
        file = self.root / "test.pdf"
        file.write_bytes(b"original")
        manifest = self.manifest(cc=[], bcc=[], attachments=[{"path": str(file)}])
        row = self.row(attachments=["test.pdf"], attachment_count=1)
        with mock.patch.object(campaign, "list_messages", return_value=(self.root / "Envelope Index", [row])):
            audit = campaign.audit_campaign(str(manifest))
        self.assertIn("attachment_unverified", audit["results"][0]["issues"])
        self.assertFalse(audit["results"][0]["verification"]["attachments"]["sha256_checked"])

    def test_contradictory_complete_replicas_include_extra_bcc(self):
        rows = [self.row(), self.row(row_id=2, bcc=["hidden@example.com"])]
        self.assertIn("bcc", mail_index.unique_messages(rows)[0]["replica_conflicts"])

    def test_quoted_unsubscribe_does_not_override_positive_reply(self):
        result = mailctl.classify_message({"subject": "Re: Proposal", "content": "Interested, let's discuss.\n\nOn Monday X wrote:\n> unsubscribe me", "attachments": []})
        self.assertEqual(result["kind"], "human_positive")
        self.assertTrue(result["quoted_or_signature_text_removed"])

    def test_truncated_classification_demands_review(self):
        result = mailctl.classify_message({"subject": "Re: proposal", "content": "Interested", "content_truncated": True})
        self.assertTrue(result["requires_review"])
        self.assertLessEqual(result["confidence"], 0.5)

    def test_new_mail_does_not_duplicate_next_page(self):
        path = self.root / "Envelope Index"
        test_mail_index.MailIndexTests().create_index_fixture(path)
        first = mail_index.search_messages(database=str(path), limit=1)
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO message_global_data VALUES (4, '<four@test>', NULL)")
            db.execute("INSERT INTO messages VALUES (4,104,4,1,1,1,40,40,1,0,0,0,100,42)")
        following = mail_index.search_messages(database=str(path), limit=2, cursor=first["next_cursor"])
        self.assertEqual([r["row_id"] for r in following["messages"]], [2, 1])

    def test_exact_address_and_account_filter(self):
        path = self.root / "Envelope Index"
        test_mail_index.MailIndexTests().create_index_fixture(path)
        found = mail_index.search_messages(database=str(path), account="test", recipient="recipient@example.com", exact_address=True)
        self.assertEqual(found["returned"], 3)
        self.assertEqual(mail_index.search_messages(database=str(path), recipient="example.com", exact_address=True)["returned"], 0)

    def test_shared_attachment_hashed_once_per_validation(self):
        file = self.root / "test.pdf"
        file.write_bytes(b"pdf")
        manifest = self.manifest(attachments=[{"path": str(file)}])
        content = json.loads(manifest.read_text())
        content["messages"] = [{**content["messages"][0], "key": f"row-{i}", "to": [f"p{i}@example.com"]} for i in range(100)]
        manifest.write_text(json.dumps(content))
        with mock.patch.object(campaign, "sha256", wraps=campaign.sha256) as digest:
            campaign.validate_manifest(str(manifest), enforce_schedule_policy=False)
        self.assertEqual(sum(call.args[0] == file.resolve() for call in digest.call_args_list), 1)

    def test_swapped_attachment_hashes_explain_pair_mismatch(self):
        expected = [{"name": "a.pdf", "size_bytes": 3, "sha256": "aaa"}, {"name": "b.pdf", "size_bytes": 3, "sha256": "bbb"}]
        actual = [{**expected[0], "sha256": "bbb"}, {**expected[1], "sha256": "aaa"}]
        self.assertIn("sha256", campaign.attachment_mismatch_fields(expected, actual))

    def test_bad_allowed_hours_returns_validation_error(self):
        manifest = self.manifest()
        content = json.loads(manifest.read_text())
        content["policy"]["allowed_hours"] = {"start": "09:00", "end": "18:00"}
        manifest.write_text(json.dumps(content))
        self.assertFalse(campaign.validate_manifest(str(manifest))["ok"])

    def test_cache_checks_ctime_even_if_size_and_mtime_preserved(self):
        version = self.root / "V10"
        cache = self.root / "cache.json"
        fixture = test_mail_store.MailStoreTests()
        file = fixture.write_emlx(version, message_id="<one@test>", payload=b"first payload")
        before = mail_store.load_send_later_evidence({"<one@test>"}, mail_version_root=version, cache_path=cache)
        stat = file.stat()
        # Equal-length replacement inside the encoded attachment, preserving original boundary and mtime.
        data = file.read_bytes().replace(b"Zmlyc3QgcGF5bG9hZA==", b"b3RoZXIgcGF5bG9hZA==")
        file.write_bytes(data)
        os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(file.stat().st_size, stat.st_size)
        after = mail_store.load_send_later_evidence({"<one@test>"}, mail_version_root=version, cache_path=cache)
        self.assertNotEqual(before["evidence"]["<one@test>"]["attachments"], after["evidence"]["<one@test>"]["attachments"])

    def test_fresh_cache_bypass_reads_bytes_again(self):
        version = self.root / "V10"
        cache = self.root / "cache.json"
        test_mail_store.MailStoreTests().write_emlx(version, message_id="<one@test>", payload=b"data")
        mail_store.load_send_later_evidence({"<one@test>"}, mail_version_root=version, cache_path=cache)
        with mock.patch.object(mail_store, "read_emlx_message", wraps=mail_store.read_emlx_message) as reader:
            mail_store.load_send_later_evidence({"<one@test>"}, mail_version_root=version, cache_path=cache, fresh=True)
        self.assertTrue(reader.called)

    def send_args(self):
        args = mailctl.build_parser().parse_args(["send", "--to", "person@example.com", "--sender", "sender@example.com", "--subject", "subject", "--body", "body", "--operation-id", str(uuid.uuid4())])
        args.confirm_send = mailctl.guarded_send(args)["data"]["intent_digest"]
        return args

    def draft(self, **changes):
        value = {"draft_id": 123, "sender": "sender@example.com", "to": ["person@example.com"], "cc": [], "bcc": [],
                 "subject": "subject", "content": "body", "attachments": [], "visible": True}
        value.update(changes)
        return value

    def test_guarded_send_reports_accepted_not_delivered_and_replays_receipt(self):
        args = self.send_args()
        calls = []
        def backend(payload, timeout):
            calls.append(payload)
            return {"ok": True, "data": {"submission_accepted": True, "delivery_confirmed": False, "intent_digest": payload["confirm_send"]}}
        with mock.patch.object(mailctl, "run_backend", side_effect=backend):
            result = mailctl.execute(args)
            replay = mailctl.execute(args)
        self.assertEqual([c["command"] for c in calls], ["send"])
        self.assertFalse(result["data"]["delivery_confirmed"])
        self.assertTrue(result["data"]["submission_accepted"])
        self.assertTrue(replay["replayed_receipt"])

    def test_backend_readback_failure_keeps_send_intent_reserved(self):
        args = self.send_args()
        with mock.patch.object(mailctl, "run_backend", side_effect=mailctl.MailCtlError("Draft BCC changed")) as backend:
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
            with self.assertRaises(send_guard.SendGuardError):
                mailctl.guarded_send(args)
        self.assertEqual(backend.call_count, 1)
        self.assertEqual(operation_state.get(args.operation_id)["state"], "unknown")

    def test_unknown_mail_mutation_never_auto_retries(self):
        args = self.send_args()
        with mock.patch.object(mailctl, "run_backend", side_effect=mailctl.MailCtlError("timeout")) as backend:
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
        self.assertEqual(backend.call_count, 1)
        self.assertEqual(operation_state.get(args.operation_id)["state"], "unknown")

    def test_draft_update_refuses_changed_revision_before_mutation(self):
        args = mailctl.build_parser().parse_args(["draft", "update", "123", "--expected-revision", "stale", "--confirm-change", "--to", "person@example.com", "--subject", "new", "--body", "body"])
        with mock.patch.object(mailctl, "run_backend", return_value={"ok": True, "data": self.draft()}) as backend:
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
        self.assertEqual(backend.call_count, 1)

    def test_ubd_retry_keeps_exact_saved_payload_and_id(self):
        client = mock.Mock(origin="https://example.com")
        client.tool.side_effect = [TimeoutError("uncertain"), {"ok": True}]
        operation_id = str(uuid.uuid4())
        proposal = {"workItemId": str(uuid.uuid4()), "expectedRevision": 3, "patch": {"detail": "original timestamp"}}
        operation_state.begin(operation_id, "ubd proposal", {"proposal": proposal, "origin": client.origin}, retain_request=True)
        with mock.patch.object(mailctl.ubd_connector, "verify_proposal_response", side_effect=lambda result, **kwargs: result):
            with self.assertRaises(TimeoutError):
                mailctl.submit_saved_proposal(client, operation_id)
            mailctl.submit_saved_proposal(client, operation_id, retry=True)
        self.assertEqual(client.tool.call_args_list[0], client.tool.call_args_list[1])
        self.assertEqual(operation_state.get(operation_id)["state"], "completed")

    def test_attachment_save_never_overwrites_existing_output(self):
        output = self.root / "saved.pdf"
        output.write_bytes(b"preserve")
        args = mailctl.build_parser().parse_args(["attachments", "save", "1", "--attachment-id", "a", "--output", str(output)])
        with mock.patch.object(mailctl, "run_backend", return_value={"ok": True, "data": [{"id": "a"}]}) as backend:
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
        self.assertEqual(backend.call_count, 1)
        self.assertEqual(output.read_bytes(), b"preserve")

    def test_doctor_fails_when_index_is_unreadable(self):
        with mock.patch.object(mail_index, "find_index", side_effect=mail_index.MailIndexError("unreadable")), mock.patch.object(mailctl, "run_backend", return_value={"ok": True, "data": {"version": mailctl.VERSION}}):
            report = mailctl.doctor(5)
        self.assertFalse(report["ok"])
        self.assertFalse(report["data"]["mail_index_read_only"])

    def test_schema_requires_summaries_and_sender_tables(self):
        path = self.root / "Envelope Index"
        test_mail_index.MailIndexTests().create_index_fixture(path)
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE summaries")
        self.assertIn("summaries", mail_index.schema_report(str(path))["missing"])

    def test_cache_evidence_expires_after_ttl(self):
        version = self.root / "V10"
        cache = self.root / "cache.json"
        test_mail_store.MailStoreTests().write_emlx(version, message_id="<one@test>", payload=b"data")
        mail_store.load_send_later_evidence({"<one@test>"}, mail_version_root=version, cache_path=cache)
        future = mail_store.time.time() + mail_store.MAX_CACHE_AGE_SECONDS + 1
        with mock.patch.object(mail_store.time, "time", return_value=future), mock.patch.object(mail_store, "read_emlx_message", wraps=mail_store.read_emlx_message) as reader:
            mail_store.load_send_later_evidence({"<one@test>"}, mail_version_root=version, cache_path=cache)
        self.assertTrue(reader.called)

    def test_attachment_export_is_private_and_reports_actual_hash(self):
        output = self.root / "saved.pdf"
        args = mailctl.build_parser().parse_args(["attachments", "save", "1", "--attachment-id", "a", "--output", str(output)])
        def backend(payload, timeout):
            if payload["command"] == "attachments_list":
                return {"ok": True, "data": [{"id": "a", "size_bytes": 999}]}
            Path(payload["output"]).write_bytes(b"actual bytes")
            return {"ok": True, "data": {}}
        with mock.patch.object(mailctl, "run_backend", side_effect=backend):
            result = mailctl.execute(args)
        self.assertEqual(result["data"]["sha256"], hashlib.sha256(b"actual bytes").hexdigest())
        self.assertEqual(result["data"]["size_bytes"], 12)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_interrupted_claim_write_is_recoverable_but_never_reissued(self):
        _, _, session = self.session()
        token = str(uuid.uuid4())
        resource = "test-campaign-2026:row-0001"
        operation_state.claim(resource, "worker", token, str(session.resolve()))
        with mock.patch.object(campaign, "audit_campaign", return_value=self.missing()):
            self.assertEqual(campaign.next_session_operations(str(session))["batch_size"], 0)
        self.assertEqual(campaign.session_status(str(session))["active_claims"]["row-0001"]["token"], token)
        result = campaign.record_schedule_session(str(session), key="row-0001", result="failed", claim_token=token)
        self.assertEqual(result["operation"]["status"], "failed")
        self.assertIsNone(operation_state.inspect_claim(resource))

    @unittest.skipUnless(shutil.which("node"), "Node is only needed for the offline JXA contract test")
    def test_jxa_snapshot_guard_blocks_bcc_drift(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let sent = 0, bcc = [], content = () => 'body'; content.attachments = () => [];
const draft = {id:()=>123,sender:()=> 'sender@example.com',subject:()=> 'subject',content,
  toRecipients:()=>[{address:()=> 'person@example.com'}],ccRecipients:()=>[],bccRecipients:()=>bcc,
  visible:()=>true,send:()=>{sent++;return true;}};
const context = {Application:()=>({outgoingMessages:{byId:()=>draft}})};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'),context);
const snap=context.draftSnapshot(draft);
bcc=[{address:()=> 'extra@example.com'}];
assert.throws(()=>context.guardDraft(draft, snap));
assert.equal(sent,0);bcc=[];
context.guardDraft(draft, snap);
assert.equal(sent,0);
'''
        subprocess.run([shutil.which("node"), "-e", script, str(mailctl.BACKEND)], check=True, capture_output=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
