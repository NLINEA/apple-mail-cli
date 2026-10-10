"""Synthetic regression cases; never operates Mail or connects to any service."""
import argparse
import base64
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import mail_index
import mailctl
import test_mail_index


class MetadataQualityTests(unittest.TestCase):
    def fixture(self, path):
        test_mail_index.MailIndexTests.create_index_fixture(self, path)

    def test_unicode_casefold_and_literal_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Envelope Index"
            self.fixture(path)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE subjects SET subject = ? WHERE ROWID = 1", ("香港 STRAẞE ÉCOLE 100% _offer",))
                db.execute("UPDATE subjects SET subject = ? WHERE ROWID = 2", ("100 other offer",))
                db.execute("UPDATE addresses SET address = ? WHERE ROWID = 1", ("Équipe@example.test",))
                db.execute("UPDATE attachments SET name = ?", ("BÜRO_100%.pdf",))
            for term in ("香港", "strasse", "école", "e\u0301cole", "100%", "_offer"):
                with self.subTest(term=term):
                    rows = mail_index.search_messages(database=str(path), subject=term)["messages"]
                    self.assertEqual([r["row_id"] for r in rows], [1])
            self.assertEqual(mail_index.search_messages(database=str(path), sender="éQUIPE")["returned"], 3)
            self.assertEqual(mail_index.search_messages(database=str(path), attachment="büro_100%")["returned"], 1)
            _, rows = mail_index.list_messages(database=str(path), subject="100%")
            self.assertEqual([r["row_id"] for r in rows], [1])

    def test_cursor_is_bound_to_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            first_path = Path(temporary) / "one"
            second_path = Path(temporary) / "two"
            self.fixture(first_path)
            self.fixture(second_path)
            cursor = mail_index.search_messages(database=str(first_path), limit=1)["next_cursor"]
            with self.assertRaises(mail_index.MailIndexError):
                mail_index.search_messages(database=str(second_path), cursor=cursor)

    def test_cursor_rejects_boolean_oversized_and_garbage_offsets(self):
        filters = {"query": "fixture"}
        for offset in (True, 2**100, -1, 1.5):
            value = mail_index.encode_cursor(offset, filters)
            with self.subTest(offset=offset), self.assertRaises(mail_index.MailIndexError):
                mail_index.decode_cursor(value, filters)
        for value in ("!garbage!", "x" * 3000):
            with self.subTest(value_length=len(value)), self.assertRaises(mail_index.MailIndexError):
                mail_index.decode_cursor(value, filters)

    def test_schema_detects_all_query_dependencies(self):
        for table in ("subjects", "summaries", "sender_addresses"):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "Envelope Index"
                self.fixture(path)
                with sqlite3.connect(path) as db:
                    db.execute("DROP TABLE " + table)
                self.assertFalse(mail_index.schema_report(str(path))["compatible"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Envelope Index"
            self.fixture(path)
            with sqlite3.connect(path) as db:
                db.execute("ALTER TABLE messages RENAME COLUMN size TO absent_size")
            self.assertIn("size", mail_index.schema_report(str(path))["missing"]["messages"])

    def test_connection_closes_after_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE sample (value TEXT)")
            with mail_index.connect_read_only(path) as db:
                self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")

    def test_schema_accepts_implicit_rowid(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Envelope Index"
            self.fixture(path)
            with sqlite3.connect(path) as db:
                db.execute("DROP TABLE subjects")
                db.execute("CREATE TABLE subjects (subject TEXT)")
            self.assertTrue(mail_index.schema_report(str(path))["compatible"])

    def test_expensive_query_is_interrupted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE sample (value TEXT)")
            with mock.patch("time.monotonic", side_effect=[0, 1000]), mail_index.connect_read_only(path) as db:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n").fetchone()


class BackendContractTests(unittest.TestCase):
    def response(self, payload, output, returncode=0, stderr=""):
        with mock.patch.object(mailctl.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(mailctl.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode, output, stderr)) as run:
            result = mailctl.run_backend(payload, 3)
        return result, run

    def test_payload_uses_utf8_stdin_and_never_argv(self):
        payload = {"command": "compose", "body": "合成 private fixture", "subject": "合成 subject"}
        result, run = self.response(payload, json.dumps({"ok": True, "command": "compose", "data": {"sent": False}}))
        argv = run.call_args[0][0]
        self.assertEqual(len(argv), 4)
        self.assertEqual(json.loads(run.call_args[1]["input"]), payload)
        self.assertEqual(run.call_args[1]["encoding"], "utf-8")
        self.assertNotIn(payload["body"], " ".join(argv))
        self.assertFalse(result["data"]["sent"])

    def test_malformed_backend_response_is_structured_failure(self):
        values = [None, [], True, {"ok": "true", "command": "ping", "data": {}},
                  {"ok": True, "command": "wrong", "data": {}},
                  {"ok": True, "command": "ping"},
                  {"ok": False, "command": "ping", "error": []}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(mailctl.MailCtlError) as caught:
                self.response({"command": "ping"}, json.dumps(value))
            self.assertEqual(caught.exception.code, "backend_contract_error")

    def test_timeout_and_permission_are_structured_and_never_retried(self):
        with mock.patch.object(mailctl.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(mailctl.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 3)) as run:
            with self.assertRaises(mailctl.MailCtlError) as caught:
                mailctl.run_backend({"command": "send"}, 3)
            self.assertEqual(caught.exception.code, "mail_automation_timeout")
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(run.call_count, 1)
        with self.assertRaises(mailctl.MailCtlError) as caught:
            self.response({"command": "ping"}, "", returncode=1, stderr="Not authorized (-1743)")
        self.assertEqual(caught.exception.code, "mail_permission_denied")

    def test_doctor_fails_when_index_access_is_missing(self):
        with mock.patch.object(mailctl.platform, "system", return_value="Darwin"), mock.patch.object(mailctl.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(Path, "exists", return_value=True), mock.patch.object(Path, "is_dir", return_value=False), mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(mailctl, "run_backend", return_value={"data": {"version": mailctl.VERSION}}), mock.patch.object(mailctl.mail_index, "find_index", side_effect=mail_index.MailIndexError("synthetic permission denied")), mock.patch.object(Path, "read_text", return_value=json.dumps({"version": mailctl.VERSION})):
            result = mailctl.doctor(3)
        self.assertFalse(result["ok"])
        self.assertFalse(result["data"]["mail_index_read_only"])

    def test_send_requires_sender_before_backend(self):
        boundaries = mailctl.capabilities()["data"]["hard_boundaries"]
        self.assertTrue(boundaries["direct_send_requires_explicit_sender"])
        self.assertTrue(boundaries["supplied_sender_requires_unique_enabled_account"])
        args = argparse.Namespace(to=["recipient@example.test"], cc=[], bcc=[], subject="Synthetic", body="合成", body_file=None, attachment=[], sender=None, confirm_send=True)
        with self.assertRaises(mailctl.MailCtlError) as caught:
            mailctl.outgoing_preflight(args, "send")
        self.assertEqual(caught.exception.code, "sender_required")

    def test_unexpected_failure_keeps_json_contract_without_private_detail(self):
        output = StringIO()
        with mock.patch.object(mailctl.sys, "argv", ["mailctl", "capabilities"]), mock.patch.object(mailctl, "execute", side_effect=RuntimeError("synthetic private detail")), redirect_stdout(output):
            self.assertEqual(mailctl.main(), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error"]["code"], "unexpected_error")
        self.assertNotIn("synthetic private detail", result["error"]["message"])

    def test_validation_failure_does_not_expose_private_detail(self):
        result = mailctl.structured_error(ValueError("synthetic private detail"), "send")
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertNotIn("synthetic private detail", result["error"]["message"])
