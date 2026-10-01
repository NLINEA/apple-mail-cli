from __future__ import annotations

import argparse
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "apple-mail-cli" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import mailctl  # noqa: E402


BUSINESS_UNIT_ID = "10000000-0000-4000-8000-000000000001"
WORK_ITEM_ID = "20000000-0000-4000-8000-000000000001"
PROPOSAL_ID = "30000000-0000-4000-8000-000000000001"
WORKER_ID = "40000000-0000-4000-8000-000000000001"
REQUEST_ID = "50000000-0000-4000-8000-000000000001"


class MailCtlTests(unittest.TestCase):
    def outgoing_args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "to": ["Person <person@example.com>"],
            "cc": [],
            "bcc": [],
            "sender": "Sender <sender@example.com>",
            "subject": "A useful subject",
            "body": "A useful body.",
            "body_file": None,
            "attachment": [],
            "confirm_send": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_capabilities_exposes_machine_contract_and_authority_boundaries(self) -> None:
        result = mailctl.capabilities()
        self.assertEqual(result["schema_version"], "apple-mail-cli/v1")
        self.assertEqual(result["version"], mailctl.VERSION)
        boundaries = result["data"]["hard_boundaries"]
        self.assertFalse(boundaries["mail_index_write"])
        self.assertFalse(boundaries["ubd_formal_state_change"])
        self.assertTrue(boundaries["direct_send_requires_confirm_send"])
        self.assertTrue(boundaries["send_later_emlx_fallback_read_only"])
        self.assertTrue(boundaries["send_later_evidence_cache_private_local_only"])
        self.assertFalse(boundaries["send_later_evidence_cache_contains_message_bodies"])

    def test_outgoing_preflight_normalizes_addresses_and_rejects_drift(self) -> None:
        payload, evidence = mailctl.outgoing_preflight(self.outgoing_args(), "compose")
        self.assertEqual(payload["to"], ["person@example.com"])
        self.assertEqual(payload["sender"], "sender@example.com")
        self.assertEqual(evidence, [])

        invalid_cases = [
            self.outgoing_args(cc=["person@example.com"]),
            self.outgoing_args(subject="Hello\nBcc: leak@example.com"),
            self.outgoing_args(to=["not-an-address"]),
            self.outgoing_args(body="   "),
        ]
        for args in invalid_cases:
            with self.subTest(args=args), self.assertRaises(mailctl.MailCtlError):
                mailctl.outgoing_preflight(args, "compose")

    def test_mailbox_mutations_require_explicit_cli_confirmation(self) -> None:
        parser = mailctl.build_parser()
        for arguments in [
            ["mark", "1", "--read"],
            ["flag", "1", "--on"],
            ["move", "1", "--to", "Archive"],
            ["trash", "1"],
        ]:
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(arguments)

        parsed = parser.parse_args(["mark", "1", "--read", "--confirm-change"])
        self.assertTrue(parsed.confirm_change)
        parsed = parser.parse_args(["trash", "1", "--confirm-trash"])
        self.assertTrue(parsed.confirm_trash)

    def test_private_event_artifact_is_0600_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "event.json"
            written = mailctl.write_private_json(str(path), {"safe": True})
            self.assertEqual(written, str(path.resolve()))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"safe": True})
            with self.assertRaises(mailctl.MailCtlError):
                mailctl.write_private_json(str(path), {"safe": False})

    def test_classification_proposal_cli_path_remains_redacted_and_pending(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict, str | None]] = []

            def capabilities(self) -> dict:
                return {
                    "schemaVersion": "ubd-agent-api/v1",
                    "data": {
                        "tools": [{"name": name} for name in mailctl.ubd_connector.PROPOSAL_REQUIRED_TOOLS],
                        "businessUnits": [{"id": BUSINESS_UNIT_ID}],
                        "policy": {
                            "directDatabaseAccess": False,
                            "send": False,
                            "publish": False,
                            "payments": False,
                            "delete": False,
                            "humanReviewRequiredForProposals": True,
                        },
                    },
                }

            def tool(self, name: str, payload: dict, *, idempotency_key: str | None = None) -> dict:
                self.calls.append((name, payload, idempotency_key))
                if name == "work.get":
                    return {
                        "schemaVersion": "ubd-agent-api/v1",
                        "tool": name,
                        "data": {
                            "item": {
                                "id": WORK_ITEM_ID,
                                "businessUnit": {"id": BUSINESS_UNIT_ID},
                                "revision": 3,
                                "detail": "Human context.",
                            }
                        },
                        "safety": {"readOnly": True, "applied": False, "requiresHumanReview": False},
                    }
                return {
                    "schemaVersion": "ubd-agent-api/v1",
                    "requestId": REQUEST_ID,
                    "tool": name,
                    "data": {
                        "proposal": {
                            "id": PROPOSAL_ID,
                            "workItemId": WORK_ITEM_ID,
                            "basedOnRevision": 3,
                            "status": "pending",
                            "worker": {"id": WORKER_ID},
                            "applied": False,
                        }
                    },
                    "safety": {"readOnly": False, "applied": False, "requiresHumanReview": True},
                }

        args = mailctl.build_parser().parse_args(
            [
                "ubd",
                "propose-classification",
                "123",
                "--business-unit-id",
                BUSINESS_UNIT_ID,
                "--work-item-id",
                WORK_ITEM_ID,
                "--expected-revision",
                "3",
                "--idempotency-key",
                REQUEST_ID,
                "--confirm-proposal",
            ]
        )
        client = FakeClient()
        message = {
            "message_id": "<private@example.com>",
            "sender": "person@example.com",
            "subject": "Re: confidential",
            "content": "A private response.",
            "date_received": "2026-08-18T01:00:00Z",
            "attachments": [],
        }
        with mock.patch.object(mailctl, "ubd_api_client", return_value=client), mock.patch.object(
            mailctl, "run_backend", return_value={"ok": True, "data": message}
        ):
            result = mailctl.execute_ubd(args)

        self.assertTrue(result["safety"]["proposal_stored"])
        self.assertTrue(result["safety"]["human_review_required"])
        proposal_body = client.calls[-1][1]
        encoded = json.dumps(proposal_body)
        for private in ["private@example.com", "person@example.com", "confidential", "A private response"]:
            self.assertNotIn(private, encoded)
        self.assertEqual(client.calls[-1][2], REQUEST_ID)

    def test_bilingual_classification_covers_opt_out_out_of_office_and_human_intent(self) -> None:
        unsubscribe = mailctl.classify_message(
            {"subject": "Re: 合作", "sender": "person@example.com", "content": "請停止寄送，謝謝。", "attachments": []}
        )
        out_of_office = mailctl.classify_message(
            {
                "subject": "自動回覆",
                "sender": "person@example.com",
                "content": "我正在休假中，請聯絡 colleague@example.com。",
                "attachments": [],
            }
        )
        positive = mailctl.classify_message(
            {"subject": "回覆: 合作", "sender": "person@example.com", "content": "有興趣，可以傾。", "attachments": []}
        )

        self.assertEqual(unsubscribe["kind"], "unsubscribe_request")
        self.assertEqual(out_of_office["kind"], "out_of_office")
        self.assertEqual(out_of_office["discovered_contacts"], ["colleague@example.com"])
        self.assertEqual(positive["kind"], "human_positive")

    def test_new_search_rebalance_session_and_dashboard_commands_parse(self) -> None:
        parser = mailctl.build_parser()
        search = parser.parse_args(["search", "--query", "proposal", "--has-attachment", "--limit", "50"])
        rebalance = parser.parse_args(
            [
                "campaign",
                "rebalance",
                "campaign.json",
                "--output",
                "new.json",
                "--start-at",
                "2026-08-19",
                "--finish-by",
                "2026-08-21",
            ]
        )
        session = parser.parse_args(
            ["campaign", "session-record", "session.json", "--key", "row-1", "--result", "applied"]
        )
        dashboard = parser.parse_args(["ubd", "dashboard-event", "campaign.json", "--business-unit-id", BUSINESS_UNIT_ID])

        self.assertTrue(search.has_attachment)
        self.assertEqual(rebalance.campaign_command, "rebalance")
        self.assertEqual(session.result, "applied")
        self.assertEqual(dashboard.ubd_command, "dashboard-event")

    def test_structured_errors_include_stable_code_hint_and_retryability(self) -> None:
        value = mailctl.structured_error(
            mailctl.MailCtlError("Timed out", code="mail_timeout", hint="Audit before retrying.", retryable=True),
            "list",
        )
        self.assertEqual(value["error"]["code"], "mail_timeout")
        self.assertTrue(value["error"]["retryable"])
        self.assertEqual(value["error"]["hint"], "Audit before retrying.")

    def test_triage_batch_never_returns_message_body(self) -> None:
        args = mailctl.build_parser().parse_args(["triage", "--limit", "1"])
        responses = [
            {
                "ok": True,
                "data": {
                    "messages": [{"id": 7}],
                    "mailbox": "INBOX",
                    "account_filter": None,
                    "scanned": 1,
                    "truncated": False,
                },
            },
            {
                "ok": True,
                "data": {
                    "id": 7,
                    "message_id": "<private@example.test>",
                    "sender": "Person <person@example.com>",
                    "subject": "Re: proposal",
                    "content": "A private body with a question?",
                    "date_received": "2026-08-18T01:00:00Z",
                    "account": "Private account",
                    "mailbox": "INBOX",
                    "attachments": [],
                },
            },
        ]
        with mock.patch.object(mailctl, "run_backend", side_effect=responses):
            result = mailctl.triage_mailbox(args)

        self.assertNotIn("content", result["items"][0])
        self.assertEqual(result["items"][0]["classification"]["kind"], "human_question")


if __name__ == "__main__":
    unittest.main()
