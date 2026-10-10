"""0.6 uses private fixtures only; no Mail mutations, sending, or UBD calls."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import test_campaign
import test_evolution
import campaign
import mailctl
import mail_index
import mail_workflow
import operation_state
import send_guard


class V060Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mailctl-060-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patch = mock.patch.dict(os.environ, {"MAILCTL_STATE_DIR": str(self.root / "state")})
        patch.start()
        self.addCleanup(patch.stop)
        guard = mock.patch.object(send_guard, "state_root", return_value=self.root / "send-attempts")
        guard.start()
        self.addCleanup(guard.stop)

    def session(self, name="one", body="first"):
        root = self.root / name
        root.mkdir()
        manifest = test_campaign.CampaignTests().write_manifest(root, body=body)
        value = json.loads(manifest.read_text())
        value["campaign_id"] = name
        manifest.write_text(json.dumps(value))
        render = campaign.render_campaign(str(manifest), str(root / "render"))
        session = root / "session.json"
        audit = self.missing()
        with mock.patch.object(campaign, "audit_campaign", return_value=audit):
            campaign.start_schedule_session(str(manifest), str(session), action="schedule", queue_path=render["queue_path"])
        return session

    def missing(self):
        result = test_campaign.CampaignTests().missing_audit()
        result["results"][0]["issues"] = []
        return result

    def row(self, **changes):
        row = {"row_id": 1, "message_id": "<sent@example.com>", "account_key": "account-A", "conversation_id": 20,
               "subject": "A complete subject", "to": ["recipient@example.com"], "cc": [], "bcc": [],
               "sender_addresses": ["sender@example.com"], "attachments": [], "attachment_count": 0,
               "state": "sent", "date_sent": "2026-08-02T00:00:00+00:00", "date_received": "2026-08-02T00:00:00+00:00",
               "scheduled_at": None, "deleted": False}
        row.update(changes)
        return row

    def test_cross_campaign_and_changed_body_share_identity_claim(self):
        first, second = self.session("first"), self.session("second", body="different body")
        with mock.patch.object(campaign, "audit_campaign", side_effect=lambda *a, **kw: self.missing()):
            a = campaign.next_session_operations(str(first), owner="one")
            b = campaign.next_session_operations(str(second), owner="two")
        self.assertEqual(a["batch_size"], 1)
        self.assertEqual(b["batch_size"], 0)
        self.assertEqual(b["blocked_claims"][0]["owners"], ["one"])
        self.assertGreater(len(a["batch"][0]["claim_resources"]), 1)

    def test_cross_process_overlapping_multi_claim_has_one_winner(self):
        source = "import operation_state,uuid,sys; print(operation_state.claim_many([sys.argv[1], 'shared-mail'], 'worker', str(uuid.uuid4()), sys.argv[1]))"
        env = {**os.environ, "PYTHONPATH": str(mailctl.SCRIPT_DIR)}
        workers = [subprocess.Popen([sys.executable, "-c", source, str(i)], env=env, stdout=subprocess.PIPE, text=True) for i in range(4)]
        self.assertEqual([worker.communicate(timeout=10)[0].strip() for worker in workers].count("True"), 1)

    def test_released_claim_cannot_issue_work_from_stale_missing_audit(self):
        first, second = self.session("first"), self.session("second")
        with mock.patch.object(campaign, "audit_campaign", side_effect=lambda *a, **kw: self.missing()):
            work = campaign.next_session_operations(str(first))["batch"][0]
        campaign.record_schedule_session(str(first), key=work["key"], result="applied", claim_token=work["claim_token"])
        scheduled = self.missing()
        scheduled["results"][0].update(status="scheduled", distinct_message_ids=["<scheduled@example.com>"])
        scheduled["summary"]["statuses"] = {"scheduled": 1}
        real_claim = operation_state.claim_many
        published = []

        def finish_first_then_claim(*args, **kwargs):
            with mock.patch.object(campaign, "audit_campaign", return_value=scheduled):
                campaign.reconcile_schedule_session(str(first))
            published.append(True)
            return real_claim(*args, **kwargs)

        def observe(*args, **kwargs):
            return scheduled if published else self.missing()

        with mock.patch.object(campaign, "audit_campaign", side_effect=observe), mock.patch.object(
            operation_state, "claim_many", side_effect=finish_first_then_claim
        ):
            with self.assertRaisesRegex(campaign.CampaignError, "after ownership"):
                campaign.next_session_operations(str(second))
        self.assertEqual(campaign.session_status(str(first))["statuses"], {"verified": 1})
        self.assertEqual(campaign.session_status(str(second))["statuses"], {"pending": 1})
        self.assertEqual(campaign.session_status(str(second))["active_claims"], {})
        self.assertEqual(list((self.root / "state" / "claims").glob("*.json")), [])
        with mock.patch.object(campaign, "audit_campaign", return_value=scheduled):
            self.assertEqual(campaign.next_session_operations(str(second))["batch_size"], 0)

    def test_post_claim_audit_is_fresh_and_all_claims_are_held(self):
        session = self.session()
        observations = []

        def observe(*args, **kwargs):
            claim = operation_state.inspect_claim("one:row-0001")
            observations.append((kwargs.get("fresh", False), claim is not None))
            return self.missing()

        with mock.patch.object(campaign, "audit_campaign", side_effect=observe):
            work = campaign.next_session_operations(str(session))
        self.assertEqual(observations, [(False, False), (True, True)])
        self.assertEqual(work["batch_size"], 1)
        self.assertEqual(campaign.session_status(str(session))["statuses"], {"claimed": 1})

    def test_post_claim_audit_failure_releases_only_unissued_claims(self):
        session = self.session()
        self.assertTrue(operation_state.claim("unrelated", "other", "other-token", "other-session"))
        with mock.patch.object(campaign, "audit_campaign", side_effect=[self.missing(), OSError("fixture audit failure")]):
            with self.assertRaisesRegex(OSError, "fixture audit failure"):
                campaign.next_session_operations(str(session))
        self.assertEqual(campaign.session_status(str(session))["statuses"], {"pending": 1})
        self.assertEqual(campaign.session_status(str(session))["active_claims"], {})
        self.assertEqual(operation_state.inspect_claim("unrelated")["token"], "other-token")
        self.assertEqual(len(list((self.root / "state" / "claims").glob("*.json"))), 1)

    def test_post_claim_unsafe_or_missing_evidence_blocks_handoff(self):
        for index, change in enumerate((
            {"status": "duplicate"}, {"issues": ["wrong_sender"]},
            {"issues": ["attachment_unverified"]}, {"issues": ["wrong_recipients"]},
            {"status": "sent", "distinct_message_ids": ["<sent@example.com>"]}, None, "duplicate_rows",
        )):
            with self.subTest(change=change):
                session = self.session("unsafe-" + str(index))
                latest = self.missing()
                if change is None:
                    latest["results"] = []
                elif change == "duplicate_rows":
                    latest["results"].append(dict(latest["results"][0]))
                else:
                    latest["results"][0].update(change)
                with mock.patch.object(campaign, "audit_campaign", side_effect=[self.missing(), latest]):
                    with self.assertRaisesRegex(campaign.CampaignError, "after ownership"):
                        campaign.next_session_operations(str(session))
                self.assertEqual(campaign.session_status(str(session))["active_claims"], {})
                self.assertEqual(campaign.session_status(str(session))["statuses"], {"pending": 1})

    def test_post_claim_reschedule_and_cancel_reject_changed_message_id(self):
        for action in ("reschedule", "cancel"):
            with self.subTest(action=action):
                session = self.session(action)
                value = json.loads(session.read_text())
                value["action"] = action
                value["operations"][0]["message_ids"] = ["<old@example.com>"]
                session.write_text(json.dumps(value))
                before = self.missing()
                before["results"][0].update(status="wrong_time", issues=["wrong_time"], distinct_message_ids=["<old@example.com>"])
                latest = self.missing()
                latest["results"][0].update(status="wrong_time", issues=["wrong_time"], distinct_message_ids=["<replacement@example.com>"])
                with mock.patch.object(campaign, "audit_campaign", side_effect=[before, latest]):
                    with self.assertRaisesRegex(campaign.CampaignError, "after ownership"):
                        campaign.next_session_operations(str(session))
                self.assertEqual(campaign.session_status(str(session))["active_claims"], {})
                self.assertEqual(campaign.session_status(str(session))["operations"][0]["message_ids"], ["<old@example.com>"])

    def test_post_claim_unchanged_reschedule_and_cancel_keep_pinned_target(self):
        for action in ("reschedule", "cancel"):
            with self.subTest(action=action):
                session = self.session("unchanged-" + action)
                value = json.loads(session.read_text())
                value["action"] = action
                value["operations"][0]["message_ids"] = ["<old@example.com>"]
                session.write_text(json.dumps(value))
                audit = self.missing()
                audit["results"][0].update(status="wrong_time", issues=["wrong_time"], distinct_message_ids=["<old@example.com>"])
                with mock.patch.object(campaign, "audit_campaign", return_value=audit):
                    work = campaign.next_session_operations(str(session))["batch"][0]
                self.assertEqual(work["message_ids"], ["<old@example.com>"])
                self.assertIn("mail-message:<old@example.com>", work["claim_resources"])
                campaign.record_schedule_session(str(session), key=work["key"], result="skipped", claim_token=work["claim_token"])

    def test_post_claim_batch_failure_preserves_previously_issued_work(self):
        for prior_work in (False, True):
            with self.subTest(prior_work=prior_work):
                root = self.root / ("batch-" + str(prior_work))
                root.mkdir()
                manifest = test_campaign.CampaignTests().write_manifest(root)
                value = json.loads(manifest.read_text())
                value["campaign_id"] = root.name
                template = value["messages"][0]
                value["messages"] = [{**template, "key": "row-" + str(index),
                    "subject": root.name + " subject " + str(index),
                    "schedule_at": "2026-08-19T09:" + str(index * 10).zfill(2) + ":00+08:00"}
                    for index in range(3)]
                manifest.write_text(json.dumps(value))
                queue = campaign.render_campaign(str(manifest), str(root / "render"))["queue_path"]
                audit = self.missing()
                audit["results"] = [{"key": row["key"], "status": "missing", "issues": []} for row in value["messages"]]
                session = root / "session.json"
                with mock.patch.object(campaign, "audit_campaign", return_value=audit):
                    campaign.start_schedule_session(str(manifest), str(session), action="schedule", queue_path=queue)
                    issued = campaign.next_session_operations(str(session), limit=1)["batch"][0] if prior_work else None
                held = []

                def fail_under_claims(*args, **kwargs):
                    held.extend(operation_state.inspect_claim(root.name + ":" + row["key"])
                                for row in value["messages"])
                    raise OSError("batch audit failure")

                with mock.patch.object(campaign, "audit_campaign", side_effect=[audit, None]) as observer:
                    observer.side_effect = lambda *a, **kw: fail_under_claims() if kw.get("fresh") else audit
                    with self.assertRaisesRegex(OSError, "batch audit failure"):
                        campaign.next_session_operations(str(session))
                self.assertEqual(len(held), 3)
                self.assertTrue(all(held))
                status = campaign.session_status(str(session))
                self.assertEqual(set(status["active_claims"]), {"row-0"} if prior_work else set())
                self.assertEqual(status["statuses"], {"claimed": 1, "pending": 2} if prior_work else {"pending": 3})
                for claim in held:
                    for resource in claim["resources"]:
                        current = operation_state.inspect_claim(resource)
                        if issued and claim["token"] == issued["claim_token"]:
                            self.assertEqual(current["token"], issued["claim_token"])
                        else:
                            self.assertIsNone(current)

    def test_multi_claim_release_requires_token_and_releases_whole_group(self):
        self.assertTrue(operation_state.claim_many(["campaign", "intent", "message"], "owner", "token", "session"))
        operation_state.release("campaign", "incorrect")
        self.assertFalse(operation_state.claim("intent", "other", "new", "elsewhere"))
        operation_state.release("campaign", "token")
        self.assertTrue(operation_state.claim("intent", "other", "new", "elsewhere"))
        self.assertIsNone(operation_state.inspect_claim("message"))

    def test_partial_claim_publication_keeps_recovery_anchor(self):
        original = operation_state.write
        calls = []
        def fail(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("interrupted")
            return original(path, data)
        with mock.patch.object(operation_state, "write", side_effect=fail):
            with self.assertRaises(OSError):
                operation_state.claim_many(["campaign", "intent"], "owner", "token", "session")
        self.assertEqual(operation_state.inspect_claim("campaign")["token"], "token")
        self.assertFalse(operation_state.claim_many(["campaign", "intent"], "owner", "new", "session"))

    def test_legacy_single_claim_still_blocks_group(self):
        path = operation_state.root() / "claims" / (operation_state.digest("campaign") + ".json")
        operation_state.write(path, {"owner": "old", "token": "old", "session": "legacy"})
        self.assertFalse(operation_state.claim_many(["campaign", "intent"], "new", "new", "new"))
        self.assertIsNone(operation_state.inspect_claim("intent"))

    def test_pinned_mail_identity_change_stops_session(self):
        session = self.session()
        value = json.loads(session.read_text())
        value["operations"][0]["message_ids"] = ["<old@example.com>"]
        session.write_text(json.dumps(value))
        audit = self.missing()
        audit["results"][0].update(status="scheduled", distinct_message_ids=["<new@example.com>"])
        with mock.patch.object(campaign, "audit_campaign", return_value=audit):
            with self.assertRaisesRegex(campaign.CampaignError, "blocks"):
                campaign.next_session_operations(str(session))
        self.assertEqual(campaign.session_status(str(session))["operations"][0]["status"], "verification_failed")

    def send_receipt(self, state="accepted"):
        key = str(uuid.uuid4())
        receipt = operation_state.begin(key, "send", {"test": True})
        receipt["created_at"] = "2026-08-01T00:00:00+00:00"
        operation_state.write(operation_state.receipt_path(key), receipt)
        operation_state.annotate(key, "send_intent", {"sender": "sender@example.com", "to": ["recipient@example.com"],
            "cc": [], "bcc": [], "subject": "A complete subject", "attachments": [], "body_sha256": "private-digest"})
        operation_state.transition(key, state)
        return key

    def reconcile(self, key, rows, has_more=False):
        with mock.patch.object(mail_index, "search_messages", return_value={"messages": rows, "has_more": has_more}):
            return mail_workflow.receipt_reconcile(key, "<sent@example.com>", "account-A")

    def test_sent_observation_is_not_delivery_body_or_operation_proof(self):
        key = self.send_receipt()
        result = self.reconcile(key, [self.row()])
        self.assertTrue(result["data"]["sent_observed"])
        for flag in ("verified", "body_verified", "attachment_bytes_verified", "operation_correlation_verified", "delivery_verified", "retry_authorized"):
            self.assertFalse(result["data"][flag])
        self.assertEqual(operation_state.get(key)["state"], "accepted")

    def test_reconciling_unknown_does_not_unlock_retry(self):
        key = self.send_receipt("unknown")
        self.reconcile(key, [self.row()])
        self.assertEqual(operation_state.get(key)["state"], "unknown")

    def test_old_sent_mail_does_not_prove_new_send(self):
        result = self.reconcile(self.send_receipt(), [self.row(date_sent="2026-07-01T00:00:00+00:00")])
        self.assertFalse(result["ok"])
        self.assertIn("sent_time_outside_operation", result["data"]["issues"])

    def test_sent_wrong_bcc_sender_account_or_missing_attachment_block(self):
        for change in ({"bcc": ["hidden@example.com"]}, {"sender_addresses": []}, {"account_key": "B"}, {"attachments": ["unexpected.pdf"], "attachment_count": 1}):
            with self.subTest(change=change):
                self.assertFalse(self.reconcile(self.send_receipt(), [self.row(**change)])["data"]["sent_observed"])

    def test_all_sent_replicas_are_checked(self):
        result = self.reconcile(self.send_receipt(), [self.row(), self.row(row_id=2, bcc=["hidden@example.com"])])
        self.assertFalse(result["ok"])

    def test_sent_ambiguous_dates_are_not_silently_deduplicated(self):
        result = self.reconcile(self.send_receipt(), [self.row(), self.row(row_id=2, date_sent="2026-08-03T00:00:00+00:00")])
        self.assertIn("conflicting_sent_dates", result["data"]["issues"])

    def test_incomplete_sent_lookup_does_not_save_observation(self):
        key = self.send_receipt()
        with self.assertRaises(ValueError):
            self.reconcile(key, [self.row()], has_more=True)
        self.assertNotIn("sent_observation", operation_state.get(key))

    def test_missing_sent_does_not_report_send_failure_or_authorize_retry(self):
        result = self.reconcile(self.send_receipt(), [])
        self.assertEqual(result["data"]["issues"], ["sent_not_observed"])
        self.assertFalse(result["data"]["retry_authorized"])

    def test_old_receipt_or_draft_id_cannot_be_reconciled(self):
        key = str(uuid.uuid4())
        operation_state.begin(key, "send", {})
        with self.assertRaisesRegex(ValueError, "no saved"):
            mail_workflow.receipt_reconcile(key, "<sent@example.com>", "account-A")
        with self.assertRaisesRegex(ValueError, "RFC"):
            mail_workflow.receipt_reconcile(self.send_receipt(), "123", "account-A")

    def test_saved_send_intent_uses_exact_preflight_and_no_body_text(self):
        helper = test_evolution.EvolutionTests()
        args = helper.send_args()
        def backend(payload, timeout):
            return {"ok": True, "data": {"submission_accepted": True, "delivery_confirmed": False, "intent_digest": payload["confirm_send"]}}
        with mock.patch.object(mailctl, "run_backend", side_effect=backend):
            mailctl.execute(args)
        intent = operation_state.get(args.operation_id)["send_intent"]
        self.assertEqual(intent["to"], ["person@example.com"])
        self.assertNotIn("body", intent)
        self.assertIn("body_sha256", intent)

    def test_public_preview_never_opens_mail_or_creates_receipts(self):
        args = test_evolution.EvolutionTests().send_args()
        args.confirm_send = None
        with mock.patch.object(mailctl, "run_backend") as backend, mock.patch.object(operation_state, "begin") as begin:
            result = mailctl.execute(args)
        self.assertTrue(result["data"]["preview"])
        backend.assert_not_called()
        begin.assert_not_called()

    def test_public_confirmation_drift_fails_before_receipt_or_backend(self):
        args = test_evolution.EvolutionTests().send_args()
        args.bcc = ["changed@example.com"]
        with mock.patch.object(mailctl, "run_backend") as backend, mock.patch.object(operation_state, "begin") as begin:
            with self.assertRaises(mailctl.MailCtlError) as failure:
                mailctl.execute(args)
        self.assertEqual(failure.exception.code, "send_intent_mismatch")
        backend.assert_not_called()
        begin.assert_not_called()

    def test_new_operation_uuid_cannot_bypass_existing_send_intent(self):
        args = test_evolution.EvolutionTests().send_args()
        def accepted(payload, timeout):
            return {"ok": True, "data": {"submission_accepted": True, "delivery_confirmed": False, "intent_digest": payload["confirm_send"]}}
        with mock.patch.object(mailctl, "run_backend", side_effect=accepted) as backend:
            mailctl.execute(args)
            args.operation_id = str(uuid.uuid4())
            with self.assertRaises(send_guard.SendGuardError):
                mailctl.execute(args)
        self.assertEqual(backend.call_count, 1)

    def triage(self, summaries, backend, size=20):
        args = mailctl.build_parser().parse_args(["triage", "--limit", "100", "--batch-size", str(size)])
        def call(payload, timeout):
            if payload["command"] == "list":
                return {"ok": True, "data": {"messages": summaries, "truncated": False}}
            return {"ok": True, "data": backend(payload)}
        with mock.patch.object(mailctl, "run_backend", side_effect=call) as runner:
            result = mailctl.triage_mailbox(args)
        return result, runner.call_count

    def message(self, identifier):
        return {"id": identifier, "message_id": f"<m{identifier}@example.com>", "subject": "Re: Test", "sender": "person@example.com",
                "content": "Interested in discussing this.", "headers": "", "attachments": []}

    def test_45_messages_need_only_three_read_batch_calls(self):
        result, calls = self.triage([{"id": i} for i in range(1, 46)], lambda payload: {
            "items": [self.message(i) for i in payload["message_ids"]], "errors": [], "not_attempted_ids": []})
        self.assertEqual(calls, 4)  # One listing plus three chunks.
        self.assertEqual(result["returned"], 45)
        self.assertTrue(result["complete"])
        self.assertNotIn("Interested", json.dumps(result))

    def test_batch_partial_failure_preserves_other_messages(self):
        result, _ = self.triage([{"id": i} for i in (1, 2, 3)], lambda p: {
            "items": [self.message(1), self.message(3)], "errors": [{"id": 2, "code": "message_read_failed"}]})
        self.assertEqual([row["id"] for row in result["items"]], [1, 3])
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], 1)

    def test_fatal_batch_stops_future_chunks_and_preserves_completed_reads(self):
        result, calls = self.triage([{"id": i} for i in (1, 2, 3, 4)], lambda p: {
            "items": [self.message(1)], "errors": [{"id": 2, "code": "mail_permission_denied"}],
            "not_attempted_ids": [3], "stopped_early": True}, size=3)
        self.assertEqual(calls, 2)
        self.assertEqual(result["not_attempted_ids"], [3, 4])
        self.assertEqual(result["returned"], 1)
        self.assertTrue(result["stopped_early"])

    def test_batch_contract_cannot_silently_drop_or_duplicate_ids(self):
        for data in ({"items": [self.message(1)], "errors": []},
                     {"items": [self.message(1), self.message(1)], "errors": []},
                     {"items": [], "errors": [], "not_attempted_ids": [1, 2]}):
            result, _ = self.triage([{"id": 1}, {"id": 2}], lambda p: data)
            self.assertFalse(result["ok"])
            self.assertEqual(result["returned"], 0)
            self.assertTrue(all(row["code"] == "backend_contract_error" for row in result["errors"]))

    def test_explicit_batch_ids_are_validated_before_mail_access(self):
        args = mailctl.build_parser().parse_args(["read-batch", "--id", "1", "--id", "1"])
        with mock.patch.object(mailctl, "run_backend") as backend:
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
        backend.assert_not_called()

    def test_reported_apple_event_timeout_is_fatal_even_with_zero_process_exit(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"command": "read", "ok": False, "error": {"message": "AppleEvent timed out."}}), "")
        with mock.patch.object(mailctl.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(mailctl.subprocess, "run", return_value=response):
            with self.assertRaises(mailctl.MailCtlError) as failure:
                mailctl.run_backend({"command": "read", "message_id": 1}, 30)
        self.assertEqual(failure.exception.code, "mail_automation_timeout")

    def test_whole_batch_timeout_preserves_earlier_chunks(self):
        def backend(payload):
            if payload["message_ids"] == [1, 2]:
                return {"items": [self.message(1), self.message(2)], "errors": []}
            raise mailctl.MailCtlError("timed out", code="mail_automation_timeout")
        result, calls = self.triage([{"id": i} for i in range(1, 7)], backend, size=2)
        self.assertEqual(calls, 3)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["not_attempted_ids"], [5, 6])

    @unittest.skipUnless(shutil.which("node"), "Node is required for the backend fixture")
    def test_jxa_batch_fixture_uses_one_location_and_isolates_read_failure(self):
        script = r'''
const fs = require('fs'), vm = require('vm');
const scope = {}; vm.createContext(scope); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), scope);
let resolves = 0;
const Mail = {inbox: () => { resolves++; return {messages: {byId: id => ({
  id: () => id, content: () => {if (id === 2) throw Error('message gone'); return 'hello';}
})}}}};
const result = scope.readBatchCommand(Mail, {message_ids:[1,2,3], max_chars:1000});
process.stdout.write(JSON.stringify({resolves,result}));
'''
        run = subprocess.run([shutil.which("node"), "-e", script, str(mailctl.BACKEND)], check=True, text=True, capture_output=True)
        result = json.loads(run.stdout)
        self.assertEqual(result["resolves"], 1)
        self.assertEqual([row["id"] for row in result["result"]["items"]], [1, 3])
        self.assertEqual(result["result"]["errors"][0]["id"], 2)

    def followups(self, rows, **changes):
        manifest = test_campaign.CampaignTests().write_manifest(self.root, cc=[], bcc=[])
        with mock.patch.object(mail_index, "list_messages", return_value=(self.root / "Envelope Index", rows)):
            return mail_workflow.followups(str(manifest), "account-A", as_of="2026-08-10T00:00:00+00:00", **changes)

    def test_due_worklist_is_always_review_only(self):
        result = self.followups([self.row()])
        self.assertEqual(result["items"][0]["status"], "follow_up_review_due")
        self.assertFalse(result["items"][0]["send_authorized"])
        self.assertTrue(result["human_review_only"])
        self.assertFalse(result["automatic_followup"])

    def test_waiting_before_due_threshold(self):
        self.assertEqual(self.followups([self.row()], after_days=10)["items"][0]["status"], "waiting")

    def test_reply_bounce_or_other_contact_activity_requires_review(self):
        for changes in ({"sender_addresses": ["recipient@example.com"]},
                        {"sender_addresses": ["mailer-daemon@example.com"]},
                        {"sender_addresses": ["recipient@example.com"], "conversation_id": 99}):
            inbound = self.row(row_id=2, message_id="<reply@example.com>", state="inbox", subject="Re: subject",
                               to=["sender@example.com"], date_received="2026-08-03T00:00:00+00:00", **changes)
            item = self.followups([self.row(), inbound])["items"][0]
            self.assertEqual(item["status"], "related_activity_review")
            self.assertEqual(len(item["related_activity"]), 1)

    def test_account_scope_prevents_cross_account_thread_collision(self):
        other = self.row(row_id=2, message_id="<reply@example.com>", state="inbox", account_key="account-B",
                         sender_addresses=["recipient@example.com"], date_received="2026-08-03T00:00:00+00:00")
        item = self.followups([self.row(), other])["items"][0]
        self.assertEqual(item["status"], "follow_up_review_due")
        self.assertEqual(item["related_activity"], [])

    def test_duplicate_sent_or_uncertain_identity_never_becomes_due(self):
        for rows in ([self.row(), self.row(row_id=2, message_id="<second@example.com>")],
                     [self.row(conversation_id=0)], [self.row(message_id="hash:123")],
                     [self.row(date_sent=None)], [self.row(state="scheduled")]):
            self.assertNotEqual(self.followups(rows)["items"][0]["status"], "follow_up_review_due")

    def test_followup_index_bound_does_not_claim_complete_worklist(self):
        with self.assertRaisesRegex(ValueError, "bound"):
            self.followups([self.row()] * 100000)

    def test_followup_output_is_private_and_never_overwritten(self):
        manifest = test_campaign.CampaignTests().write_manifest(self.root, cc=[], bcc=[])
        output = self.root / "worklist.json"
        args = mailctl.build_parser().parse_args(["campaign", "followups", str(manifest), "--account-key", "account-A", "--output", str(output)])
        with mock.patch.object(mail_index, "list_messages", return_value=(self.root / "Envelope Index", [self.row()])):
            mailctl.execute(args)
            before = output.read_bytes()
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.execute(args)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
