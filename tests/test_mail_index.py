from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "apple-mail-cli" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import mail_index  # noqa: E402


class MailIndexTests(unittest.TestCase):
    def create_index_fixture(self, path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE messages (
                    ROWID INTEGER PRIMARY KEY, message_id INTEGER, global_message_id INTEGER, sender INTEGER,
                    subject INTEGER, summary INTEGER, date_sent INTEGER, date_received INTEGER, mailbox INTEGER,
                    read INTEGER, flagged INTEGER, deleted INTEGER, size INTEGER, conversation_id INTEGER
                );
                CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
                CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
                CREATE TABLE summaries (ROWID INTEGER PRIMARY KEY, summary TEXT);
                CREATE TABLE message_global_data (ROWID INTEGER PRIMARY KEY, message_id_header TEXT, send_later_date INTEGER);
                CREATE TABLE recipients (message INTEGER, address INTEGER, type INTEGER);
                CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT DEFAULT '');
                CREATE TABLE attachments (message INTEGER, attachment_id TEXT, name TEXT);
                CREATE TABLE sender_addresses (address INTEGER PRIMARY KEY, sender INTEGER);
                INSERT INTO mailboxes VALUES (1, 'imap://test/INBOX');
                INSERT INTO subjects VALUES (1, 'Alpha proposal'), (2, 'Re: Alpha proposal');
                INSERT INTO summaries VALUES (1, 'First summary'), (2, 'Second summary'), (3, 'Third summary');
                INSERT INTO addresses(ROWID, address) VALUES
                    (1, 'sender@example.com'), (2, 'recipient@example.com');
                INSERT INTO sender_addresses VALUES (1, 1);
                INSERT INTO message_global_data VALUES
                    (1, '<one@example.test>', NULL),
                    (2, '<two@example.test>', NULL),
                    (3, '<three@example.test>', NULL);
                INSERT INTO messages VALUES
                    (1, 101, 1, 1, 1, 1, 10, 10, 1, 0, 0, 0, 100, 42),
                    (2, 102, 2, 1, 2, 2, 20, 20, 1, 1, 1, 0, 200, 42),
                    (3, 103, 3, 1, 2, 3, 30, 30, 1, 1, 0, 0, 300, 42);
                INSERT INTO recipients VALUES (1, 2, 0), (2, 2, 0), (3, 2, 0);
                INSERT INTO attachments VALUES (2, 'attachment-1', 'proposal.pdf');
                """
            )

    def test_connection_is_enforced_read_only_by_uri_and_pragma(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Envelope Index"
            with sqlite3.connect(path) as writable:
                writable.execute("CREATE TABLE evidence (value TEXT)")
                writable.execute("INSERT INTO evidence VALUES ('unchanged')")

            with mail_index.connect_read_only(path) as readonly:
                self.assertEqual(readonly.execute("SELECT value FROM evidence").fetchone()[0], "unchanged")
                self.assertEqual(readonly.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute("UPDATE evidence SET value = 'changed'")

            with sqlite3.connect(path) as check:
                self.assertEqual(check.execute("SELECT value FROM evidence").fetchone()[0], "unchanged")

    def test_message_id_deduplication_prefers_material_sent_state(self) -> None:
        rows = [
            {"message_id": "one", "state": "inbox"},
            {"message_id": "one", "state": "scheduled"},
            {"message_id": "one", "state": "sent"},
            {"message_id": "two", "state": "scheduled"},
        ]
        unique = mail_index.unique_messages(rows)
        self.assertEqual({item["message_id"]: item["state"] for item in unique}, {"one": "sent", "two": "scheduled"})
        self.assertEqual(
            mail_index.counts(rows),
            {
                "index_rows": 4,
                "unique_message_ids": 2,
                "states": {"scheduled": 1, "sent": 1},
                "replica_rows": 2,
            },
        )

    def test_same_state_replica_prefers_complete_evidence_deterministically(self) -> None:
        incomplete = {
            "message_id": "one",
            "row_id": 20,
            "state": "scheduled",
            "to": [],
            "cc": [],
            "bcc": [],
            "sender_addresses": [],
            "attachments": [],
            "attachment_count": 0,
            "scheduled_at": None,
        }
        complete = {
            **incomplete,
            "row_id": 10,
            "to": ["recipient@example.com"],
            "sender_addresses": ["sender@example.com"],
            "attachments": ["proposal.pdf"],
            "attachment_count": 1,
            "scheduled_at": "2026-09-05T09:00:00+08:00",
        }

        self.assertEqual(mail_index.unique_messages([incomplete, complete]), [complete])
        self.assertEqual(mail_index.unique_messages([complete, incomplete]), [complete])

    def test_missing_global_and_internal_ids_fall_back_to_unique_row_id(self) -> None:
        base = {
            "message_id": None,
            "internal_message_hash": None,
            "subject": "",
            "mailbox_url": "/INBOX",
            "send_later_epoch": None,
            "date_sent_epoch": None,
            "date_received_epoch": None,
            "is_read": 0,
            "is_flagged": 0,
            "is_deleted": 0,
            "size_bytes": 1,
            "to_addresses": "",
            "cc_addresses": "",
            "bcc_addresses": "",
            "sender_addresses": "",
            "attachment_names": "",
            "attachment_count": 0,
        }
        first = mail_index.normalize_row({**base, "row_id": 10})
        second = mail_index.normalize_row({**base, "row_id": 11})
        self.assertEqual(first["message_id"], "row:10")
        self.assertEqual(second["message_id"], "row:11")
        self.assertEqual(len(mail_index.unique_messages([first, second])), 2)

    def test_index_search_paginates_and_binds_cursor_to_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Envelope Index"
            self.create_index_fixture(path)
            first = mail_index.search_messages(database=str(path), query="alpha", limit=1)
            second = mail_index.search_messages(
                database=str(path), query="alpha", limit=1, cursor=first["next_cursor"]
            )
            with self.assertRaises(mail_index.MailIndexError):
                mail_index.search_messages(database=str(path), query="different", cursor=first["next_cursor"])

        self.assertEqual(first["returned"], 1)
        self.assertTrue(first["has_more"])
        self.assertNotEqual(first["messages"][0]["message_id"], second["messages"][0]["message_id"])
        self.assertFalse(first["body_searched"])

    def test_thread_search_is_chronological_and_attachment_filterable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Envelope Index"
            self.create_index_fixture(path)
            thread = mail_index.thread_messages(42, database=str(path))
            attachment = mail_index.search_messages(
                database=str(path), has_attachment=True, attachment="proposal.pdf"
            )
            schema = mail_index.schema_report(str(path))

        self.assertEqual([item["row_id"] for item in thread["messages"]], [1, 2, 3])
        self.assertEqual([item["row_id"] for item in attachment["messages"]], [2])
        self.assertTrue(schema["compatible"])


if __name__ == "__main__":
    unittest.main()
