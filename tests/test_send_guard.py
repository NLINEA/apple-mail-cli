"""Synthetic send intent and durable attempt tests; never calls Mail."""
import argparse
from contextlib import redirect_stderr
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/apple-mail-cli/scripts"))
import mailctl
import send_guard


class GuardedSendTests(unittest.TestCase):
    def test_body_readback_accepts_only_exact_or_complete_empty_paragraph_projection(self):
        for body in ("香港 café\n\nEnd\n\n", "One\n\n\nTwo\n", "\nStart\n\n", "Keep \n \nspacing", "Literal\u2028text\n\n"):
            rendered = "\n".join("\u2028" if line == "" and index < len(body.split("\n")) - 1 else line
                                 for index, line in enumerate(body.split("\n")))
            with self.subTest(body=body):
                self.assertEqual(mailctl.body_readback_comparison(body, body), "exact")
                self.assertIn(mailctl.body_readback_comparison(rendered, body), ("exact", "mail_empty_paragraphs"))
                for changed in (body + "signature", body + "\u2028", body.replace("\n", "\r\n"), "\ufffc" + body):
                    if changed != body:
                        self.assertEqual(mailctl.body_readback_comparison(changed, body), "mismatch")
        self.assertEqual(mailctl.body_readback_comparison("One\u2028\n\n", "One\n\n"), "mismatch")
        self.assertEqual(mailctl.body_readback_comparison("One\n\u2028\n\nEnd", "One\n\n\nEnd"), "mismatch")
        self.assertEqual(mailctl.body_readback_comparison("Literaltext\n\n", "Literal\u2028text\n\n"), "mismatch")

    def test_native_null_attachments_remain_unknown_after_body_compatibility(self):
        expected = dict(sender="sender@example.test", to=["recipient@example.test"], cc=[], bcc=[], subject="Test", body="One\n\nEnd\n\n")
        current = {**expected, "content": "One\n\u2028\nEnd\n\u2028\n", "attachments": None}
        result = mailctl.draft_verification(current, expected, [])
        self.assertFalse(result["verified"])
        self.assertEqual(result["mismatch_fields"], ["attachments_unverified"])
        self.assertEqual(result["body_comparison"], "mail_empty_paragraphs")
        self.assertFalse(result["body_exact_match"])
        self.assertFalse(result["attachment_sha256_checked"])

    def test_native_draft_diagnostics_survive_unknown_send_without_enabling_retry(self):
        diagnostics = {"stage": "draft_verification", "draft_id": 99, "send_invoked": False,
                       "mismatch_fields": ["attachments_unverified"], "body_comparison": "mail_empty_paragraphs"}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": False, "command": "send",
            "error": {"message": "Draft verification failed; it remains unsent.", "diagnostics": diagnostics}}), stderr="")
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(send_guard, "state_root", return_value=Path(temporary) / "state"), mock.patch.object(mailctl.shutil, "which", return_value="synthetic-osascript"), mock.patch.object(mailctl.subprocess, "run", return_value=completed) as backend:
            args = self.args()
            args.confirm_send = self.preview(args)["intent_digest"]
            with self.assertRaises(mailctl.MailCtlError) as caught:
                mailctl.guarded_send(args)
            error = mailctl.structured_error(caught.exception, "send")["error"]
            self.assertEqual(error["code"], "send_outcome_unknown")
            self.assertEqual(error["diagnostics"], diagnostics)
            self.assertFalse(error["retryable"])
            with self.assertRaises(send_guard.SendGuardError):
                mailctl.guarded_send(args)
            self.assertEqual(backend.call_count, 1)

    def test_diagnostics_are_content_free_strict_and_cannot_claim_send_success(self):
        good = {"stage": "draft_verification", "draft_id": 99, "send_invoked": False,
                "mismatch_fields": ["attachments_unverified"], "body_comparison": "exact"}
        self.assertEqual(mailctl.draft_failure_diagnostics({**good, "private_body": "do not expose"}), good)
        for change in ({"send_invoked": True}, {"send_invoked": 0}, {"draft_id": True}, {"draft_id": "99"},
                       {"body_comparison": []}, {"body_comparison": {}}, {"mismatch_fields": [None]},
                       {"mismatch_fields": ["private address"]}, {"mismatch_fields": []}):
            with self.subTest(change=change):
                self.assertIsNone(mailctl.draft_failure_diagnostics({**good, **change}))

    def test_receipt_keeps_draft_failure_observation_but_stays_unknown(self):
        import operation_state
        diagnostics = {"stage": "draft_verification", "draft_id": 99, "send_invoked": False,
                       "mismatch_fields": ["attachments_unverified"], "body_comparison": "mail_empty_paragraphs"}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"MAILCTL_STATE_DIR": temporary}), mock.patch.object(send_guard, "state_root", return_value=Path(temporary) / "attempts"):
            args = self.args(operation_id=str(uuid.uuid4()))
            args.confirm_send = self.preview(args)["intent_digest"]
            error = mailctl.MailCtlError("Draft failed", diagnostics=diagnostics)
            with mock.patch.object(mailctl, "run_backend", side_effect=error) as backend:
                with self.assertRaises(mailctl.MailCtlError):
                    mailctl.execute(args)
                receipt = operation_state.get(args.operation_id)
                self.assertEqual(receipt["state"], "unknown")
                self.assertEqual(receipt["result"]["error"]["diagnostics"], diagnostics)
                with self.assertRaises(mailctl.MailCtlError):
                    mailctl.execute(args)
                self.assertEqual(backend.call_count, 1)

    def args(self, **changes):
        values = dict(command="send", to=["Recipient <recipient@example.test>"], cc=["copy@example.test"],
                      bcc=["private-copy@example.test"], sender="sender@example.test", subject="香港 café",
                      body="Final body\n\n", body_file=None, attachment=[], confirm_send=None, timeout=3)
        values.update(changes)
        return argparse.Namespace(**values)

    def preview(self, args):
        return mailctl.guarded_send(args)["data"]

    def accepted(self, payload, timeout):
        return {"ok": True, "command": "send", "data": {"submission_accepted": True,
                "delivery_confirmed": False, "intent_digest": payload["confirm_send"], "outcome": "accepted_by_mail"}}

    def test_preview_has_exact_intent_without_backend_or_state_writes(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(send_guard, "state_root", return_value=Path(temporary) / "state"), mock.patch.object(mailctl, "run_backend") as backend:
            data = self.preview(self.args())
            self.assertEqual(data["intent"]["to"], ["recipient@example.test"])
            self.assertEqual(data["intent"]["body"], "Final body\n\n")
            self.assertEqual(data["intent"]["subject"], "香港 café")
            self.assertFalse(data["submission_accepted"])
            self.assertFalse(data["delivery_confirmed"])
            self.assertTrue(data["requires_human_confirmation"])
            backend.assert_not_called()
            self.assertFalse((Path(temporary) / "state").exists())

    def test_every_envelope_field_and_body_drift_blocks_before_backend(self):
        args = self.args()
        digest = self.preview(args)["intent_digest"]
        for change in ({"sender": "other@example.test"}, {"to": ["other@example.test"]},
                       {"cc": []}, {"bcc": []}, {"subject": "Changed"}, {"body": "Changed\n\n"}):
            with self.subTest(change=list(change)), mock.patch.object(mailctl, "run_backend") as backend, mock.patch.object(send_guard, "reserve_attempt") as reserve:
                with self.assertRaises(mailctl.MailCtlError) as caught:
                    mailctl.guarded_send(self.args(confirm_send=digest, **change))
                self.assertEqual(caught.exception.code, "send_intent_mismatch")
                backend.assert_not_called()
                reserve.assert_not_called()

    def test_attachment_name_size_and_same_length_byte_drift_change_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "approved.pdf"
            source.write_bytes(b"approved")
            args = self.args(attachment=[str(source)])
            original = self.preview(args)["intent_digest"]
            for name, data in (("approved.pdf", b"altered!"), ("approved.pdf", b"longer bytes"), ("renamed.pdf", b"approved")):
                with self.subTest(name=name, size=len(data)):
                    target = root / name
                    target.write_bytes(data)
                    with mock.patch.object(mailctl, "run_backend") as backend, self.assertRaises(mailctl.MailCtlError):
                        mailctl.guarded_send(self.args(attachment=[str(target)], confirm_send=original))
                    backend.assert_not_called()

    def test_boolean_confirmation_and_bare_legacy_flag_cannot_send(self):
        with mock.patch.object(mailctl, "run_backend") as backend:
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.guarded_send(self.args(confirm_send=True))
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                mailctl.build_parser().parse_args(["send", "--to", "recipient@example.test", "--sender", "sender@example.test", "--subject", "Synthetic", "--body", "body", "--confirm-send"])
            backend.assert_not_called()

    def test_accepted_intent_uses_private_read_only_snapshot_and_blocks_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "approved.pdf"
            source.write_bytes(b"approved")
            args = self.args(attachment=[str(source)])
            args.confirm_send = self.preview(args)["intent_digest"]
            def native(payload, timeout):
                source.write_bytes(b"later changed original")
                snapshot = Path(payload["attachments"][0])
                self.assertEqual(snapshot.read_bytes(), b"approved")
                self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o400)
                self.assertNotEqual(str(snapshot), str(source))
                return self.accepted(payload, timeout)
            with mock.patch.object(send_guard, "state_root", return_value=root / "state"), mock.patch.object(mailctl, "run_backend", side_effect=native) as backend:
                result = mailctl.guarded_send(args)
                self.assertFalse(result["data"]["delivery_confirmed"])
                record_path = root / "state" / args.confirm_send / "attempt.json"
                record = json.loads(record_path.read_text())
                self.assertEqual(record["state"], "accepted_by_mail")
                self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(record_path.parent.stat().st_mode), 0o700)
                for text in ("recipient@example.test", "Final body", "香港"):
                    self.assertNotIn(text, record_path.read_text())
                source.write_bytes(b"approved")
                with self.assertRaises(send_guard.SendGuardError) as caught:
                    mailctl.guarded_send(args)
                self.assertEqual(caught.exception.code, "send_attempt_exists")
                self.assertEqual(backend.call_count, 1)

    def test_uncertain_timeout_backend_error_and_old_contract_are_not_retryable(self):
        for outcome in (mailctl.MailCtlError("synthetic timeout", code="mail_automation_timeout", retryable=True),
                        mailctl.MailCtlError("synthetic backend error"),
                        {"ok": True, "command": "send", "data": {"sent": True, "delivery_confirmed": False}}):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "state"
                args = self.args()
                args.confirm_send = self.preview(args)["intent_digest"]
                native = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(return_value=outcome)
                with mock.patch.object(send_guard, "state_root", return_value=root), mock.patch.object(mailctl, "run_backend", native):
                    with self.assertRaises(mailctl.MailCtlError) as caught:
                        mailctl.guarded_send(args)
                    error = mailctl.structured_error(caught.exception, "send")["error"]
                    self.assertEqual(error["code"], "send_outcome_unknown")
                    self.assertFalse(error["retryable"])
                    self.assertEqual(json.loads((root / args.confirm_send / "attempt.json").read_text())["state"], "outcome_unknown")
                    with self.assertRaises(send_guard.SendGuardError):
                        mailctl.guarded_send(args)
                    self.assertEqual(native.call_count, 1)

    def test_process_interruption_keeps_in_flight_reservation(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(send_guard, "state_root", return_value=Path(temporary) / "state"):
            args = self.args()
            args.confirm_send = self.preview(args)["intent_digest"]
            with mock.patch.object(mailctl, "run_backend", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    mailctl.guarded_send(args)
            path = send_guard.state_root() / args.confirm_send / "attempt.json"
            self.assertEqual(json.loads(path.read_text())["state"], "in_flight")
            with mock.patch.object(mailctl, "run_backend") as backend, self.assertRaises(send_guard.SendGuardError):
                mailctl.guarded_send(args)
            backend.assert_not_called()

    def test_failure_to_record_final_acceptance_becomes_unknown_and_blocks(self):
        original = send_guard.SendAttempt.finish
        def finish(attempt, state):
            if state == "accepted_by_mail":
                raise OSError("synthetic disk failure")
            return original(attempt, state)
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(send_guard, "state_root", return_value=Path(temporary) / "state"), mock.patch.object(send_guard.SendAttempt, "finish", finish), mock.patch.object(mailctl, "run_backend", side_effect=self.accepted) as backend:
            args = self.args()
            args.confirm_send = self.preview(args)["intent_digest"]
            with self.assertRaises(mailctl.MailCtlError) as caught:
                mailctl.guarded_send(args)
            self.assertEqual(caught.exception.code, "send_outcome_unknown")
            with self.assertRaises(send_guard.SendGuardError):
                mailctl.guarded_send(args)
            self.assertEqual(backend.call_count, 1)

    def test_source_changed_during_copy_or_symlink_swap_never_submits(self):
        for replacement in ("bytes", "symlink"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "approved.pdf"
                source.write_bytes(b"approved")
                payload, evidence = mailctl.outgoing_preflight(self.args(attachment=[str(source)]), "send")
                digest = send_guard.intent_digest(send_guard.canonical_intent(payload, evidence))
                if replacement == "bytes":
                    source.write_bytes(b"altered!")
                else:
                    other = root / "other.pdf"
                    other.write_bytes(b"approved")
                    source.unlink()
                    source.symlink_to(other)
                with mock.patch.object(send_guard, "state_root", return_value=root / "state"), self.assertRaises((send_guard.SendGuardError, OSError)):
                    send_guard.reserve_attempt(digest, evidence)
                self.assertEqual(json.loads((root / "state" / digest / "attempt.json").read_text())["state"], "not_submitted")

    def test_atomic_reservation_allows_only_one_concurrent_attempt(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(send_guard, "state_root", return_value=Path(temporary) / "state"):
            digest = self.preview(self.args())["intent_digest"]
            def reserve(_):
                try:
                    send_guard.reserve_attempt(digest, [])
                    return "reserved"
                except send_guard.SendGuardError as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(reserve, [1, 2]))
            self.assertCountEqual(outcomes, ["reserved", "send_attempt_exists"])

    def test_unsafe_state_permissions_and_symlink_root_block(self):
        for kind in ("permissions", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / "state"
                if kind == "permissions":
                    state.mkdir(mode=0o755)
                else:
                    target = root / "elsewhere"
                    target.mkdir(mode=0o700)
                    state.symlink_to(target)
                with mock.patch.object(send_guard, "state_root", return_value=state), self.assertRaises(send_guard.SendGuardError) as caught:
                    send_guard.reserve_attempt("a" * 64, [])
                self.assertEqual(caught.exception.code, "send_state_unsafe")

    def test_attachment_limits_and_invalid_unicode_fail_offline(self):
        payload, _ = mailctl.outgoing_preflight(self.args(), "send")
        descriptor = {"name": "synthetic.pdf", "size_bytes": send_guard.MAX_ATTACHMENT_BYTES + 1, "sha256": "0" * 64}
        with self.assertRaises(send_guard.SendGuardError):
            send_guard.canonical_intent(payload, [descriptor])
        descriptor["size_bytes"] = 1
        with self.assertRaises(send_guard.SendGuardError):
            send_guard.canonical_intent(payload, [descriptor] * 21)
        payload["body"] = "\ud800"
        with self.assertRaises(send_guard.SendGuardError):
            send_guard.intent_digest(send_guard.canonical_intent(payload, []))

    def test_attachment_bound_is_checked_before_hashing_oversize_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic.pdf"
            source.write_bytes(b"x")
            with mock.patch.object(send_guard, "MAX_ATTACHMENT_BYTES", 0), mock.patch.object(mailctl, "file_digest") as checksum:
                with self.assertRaises(mailctl.MailCtlError) as caught:
                    self.preview(self.args(attachment=[str(source)]))
                self.assertEqual(caught.exception.code, "send_attachment_limit")
                checksum.assert_not_called()
