from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "apple-mail-cli" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ubd_connector  # noqa: E402


BUSINESS_UNIT_ID = "10000000-0000-4000-8000-000000000001"
WORK_ITEM_ID = "20000000-0000-4000-8000-000000000001"
PROPOSAL_ID = "30000000-0000-4000-8000-000000000001"
WORKER_ID = "40000000-0000-4000-8000-000000000001"


class UbdConnectorTests(unittest.TestCase):
    def campaign_audit(self) -> dict:
        return {
            "campaign_id": "kurio-partnerships-2026-08",
            "complete": False,
            "safe_to_resume_missing": True,
            "summary": {
                "active": 12,
                "statuses": {"missing": 2, "scheduled": 7, "sent": 3},
                "issues": {},
                "mail_index_rows_scanned": 30,
                "unique_message_ids_scanned": 26,
                "replica_rows_ignored": 4,
            },
            "results": [
                {
                    "to": ["private@example.com"],
                    "subject": "Private subject",
                    "matches": [{"message_id": "private-message-id"}],
                }
            ],
        }

    def test_campaign_event_exports_only_aggregate_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "campaign.json"
            manifest.write_text('{"schema_version":1,"private":"person@example.com"}', encoding="utf-8")
            event = ubd_connector.campaign_audit_event(
                str(manifest),
                self.campaign_audit(),
                business_unit_id=BUSINESS_UNIT_ID,
                work_item_id=WORK_ITEM_ID,
                observed_at="2026-08-18T01:00:00Z",
            )

        encoded = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("private@example.com", encoded)
        self.assertNotIn("Private subject", encoded)
        self.assertNotIn("private-message-id", encoded)
        self.assertEqual(event["event"]["payload"]["status_counts"]["scheduled"], 7)
        self.assertFalse(event["authority"]["formal_state_changed"])
        self.assertTrue(event["authority"]["human_review_required"])

    def test_campaign_event_accepts_attachment_unverified_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "campaign.json"
            manifest.write_text('{"schema_version":1}', encoding="utf-8")
            audit = self.campaign_audit()
            audit["summary"]["issues"] = {"attachment_unverified": 2}
            audit["safe_to_resume_missing"] = False
            event = ubd_connector.campaign_audit_event(
                str(manifest),
                audit,
                business_unit_id=BUSINESS_UNIT_ID,
                work_item_id=WORK_ITEM_ID,
                observed_at="2026-08-18T01:00:00Z",
            )

        self.assertEqual(event["event"]["payload"]["issue_counts"]["attachment_unverified"], 2)

    def test_dashboard_event_keeps_only_quota_and_triage_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "campaign.json"
            manifest.write_text('{"schema_version":1,"private":"person@example.com"}', encoding="utf-8")
            dashboard = {
                "campaign_id": "kurio-partnerships-2026-08",
                "campaign": {
                    "complete": False,
                    "safe_to_resume_missing": True,
                    "statuses": {"missing": 1, "scheduled": 2},
                    "issues": {"wrong_recipients": 1},
                },
                "account_volume_by_day": {
                    "2026-08-18": {
                        "sent": 67,
                        "scheduled": 322,
                        "total": 389,
                        "daily_maximum": 1000,
                        "remaining": 611,
                        "over_limit": False,
                    }
                },
                "triage": {
                    "matched": 2,
                    "classifications": {"human_positive": 1, "bounce_permanent": 1},
                    "source": "/private/triage.json",
                },
            }
            event = ubd_connector.campaign_dashboard_event(
                str(manifest),
                dashboard,
                business_unit_id=BUSINESS_UNIT_ID,
                work_item_id=WORK_ITEM_ID,
                observed_at="2026-08-18T01:00:00Z",
            )

        encoded = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("person@example.com", encoded)
        self.assertNotIn("/private/triage.json", encoded)
        self.assertEqual(event["event"]["payload"]["daily_volume"][0]["total_count"], 389)
        self.assertEqual(event["event"]["payload"]["triage_counts"]["human_positive"], 1)
        self.assertIn("over_limit_days=0", ubd_connector.event_summary(event))

    def test_event_identity_is_stable_across_observation_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "campaign.json"
            manifest.write_text('{"schema_version":1}', encoding="utf-8")
            first = ubd_connector.campaign_audit_event(
                str(manifest),
                self.campaign_audit(),
                business_unit_id=BUSINESS_UNIT_ID,
                work_item_id=WORK_ITEM_ID,
                observed_at="2026-08-18T01:00:00Z",
            )
            second = ubd_connector.campaign_audit_event(
                str(manifest),
                self.campaign_audit(),
                business_unit_id=BUSINESS_UNIT_ID,
                work_item_id=WORK_ITEM_ID,
                observed_at="2026-08-18T02:00:00Z",
            )

        self.assertEqual(first["event"]["id"], second["event"]["id"])
        self.assertNotEqual(first["event"]["observed_at"], second["event"]["observed_at"])

    def test_classification_event_hashes_message_identity_and_drops_content(self) -> None:
        event = ubd_connector.classification_event(
            {
                "message_id": "<secret-message@example.com>",
                "sender": "sender@example.com",
                "subject": "Re: confidential",
                "content": "Please call me.",
                "date_received": "2026-08-18T01:00:00Z",
            },
            {
                "kind": "bounce_permanent",
                "confidence": 0.99,
                "failed_recipients": ["bad@example.com"],
                "smtp_codes": ["5.1.1"],
            },
            business_unit_id=BUSINESS_UNIT_ID,
            work_item_id=WORK_ITEM_ID,
            observed_at="2026-08-18T01:05:00Z",
        )

        encoded = json.dumps(event, ensure_ascii=False)
        for private_value in [
            "secret-message@example.com",
            "sender@example.com",
            "bad@example.com",
            "confidential",
            "Please call me",
        ]:
            self.assertNotIn(private_value, encoded)
        payload = event["event"]["payload"]
        self.assertEqual(payload["classification"], "bounce_permanent")
        self.assertEqual(payload["failed_recipient_count"], 1)
        self.assertEqual(payload["suggested_action"], "suppress_and_find_contact")

    def test_classification_event_rejects_non_finite_confidence(self) -> None:
        with self.assertRaises(ubd_connector.UbdConnectorError):
            ubd_connector.classification_event(
                {"message_id": "stable-id"},
                {"kind": "unclassified", "confidence": float("nan")},
                business_unit_id=BUSINESS_UNIT_ID,
            )

    def test_event_blocks_accidental_email_leak(self) -> None:
        with self.assertRaises(ubd_connector.UbdConnectorError):
            ubd_connector.build_event(
                "mail.test",
                business_unit_id=BUSINESS_UNIT_ID,
                work_item_id=None,
                payload={"note": "leak@example.com"},
            )

    def test_origin_requires_https_except_localhost(self) -> None:
        self.assertEqual(ubd_connector.validate_origin("https://os.example.invalid/"), "https://os.example.invalid")
        self.assertEqual(ubd_connector.validate_origin("http://127.0.0.1:3000"), "http://127.0.0.1:3000")
        for invalid in [
            "http://os.example.invalid",
            "https://user:pass@os.example.invalid",
            "https://os.example.invalid/api",
            "https://os.example.invalid?token=secret",
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ubd_connector.UbdConnectorError):
                ubd_connector.validate_origin(invalid)

    def test_api_token_rejects_header_injection_and_excessive_length(self) -> None:
        for token in ["token\nInjected: value", "x" * 4_097]:
            with self.subTest(token_length=len(token)), self.assertRaises(ubd_connector.UbdConnectorError):
                ubd_connector.AgentApiClient("https://os.example.invalid", token)

    def capabilities(self) -> dict:
        return {
            "schemaVersion": "ubd-agent-api/v1",
            "data": {
                "tools": [
                    {"name": "system.capabilities"},
                    {"name": "work.list"},
                    {"name": "work.get"},
                    {"name": "work.propose_update"},
                ],
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

    def test_capabilities_must_preserve_minimum_agent_boundary(self) -> None:
        data = ubd_connector.verify_capabilities(
            self.capabilities(),
            required_tools=ubd_connector.PROPOSAL_REQUIRED_TOOLS,
            business_unit_id=BUSINESS_UNIT_ID,
        )
        self.assertEqual(len(data["businessUnits"]), 1)

        too_broad = self.capabilities()
        too_broad["data"]["policy"]["send"] = True
        with self.assertRaises(ubd_connector.UbdConnectorError):
            ubd_connector.verify_capabilities(
                too_broad,
                required_tools=["system.capabilities"],
            )

    def test_proposal_replaces_only_its_owned_detail_section(self) -> None:
        event = ubd_connector.build_event(
            "mail.message.classified",
            business_unit_id=BUSINESS_UNIT_ID,
            work_item_id=WORK_ITEM_ID,
            payload={
                "message_reference_sha256": "a" * 64,
                "classification": "likely_human_reply",
                "confidence": 0.75,
                "suggested_action": "review_and_follow_up",
                "failed_recipient_count": 0,
                "smtp_codes": [],
                "received_at": "2026-08-18T01:00:00Z",
            },
            observed_at="2026-08-18T01:05:00Z",
        )
        work = {
            "id": WORK_ITEM_ID,
            "businessUnit": {"id": BUSINESS_UNIT_ID},
            "revision": 3,
            "detail": "Keep this human-authored context.\n\n[UBD Mail Connector]\nOld result\n[/UBD Mail Connector]",
        }
        payload = ubd_connector.proposal_payload(event, work, expected_revision=3)
        detail = payload["patch"]["detail"]
        self.assertIn("Keep this human-authored context.", detail)
        self.assertNotIn("Old result", detail)
        self.assertEqual(detail.count("[UBD Mail Connector]"), 1)
        self.assertIn("likely_human_reply", detail)

    def test_proposal_refuses_stale_or_oversized_work(self) -> None:
        event = ubd_connector.build_event(
            "mail.message.classified",
            business_unit_id=BUSINESS_UNIT_ID,
            work_item_id=WORK_ITEM_ID,
            payload={
                "message_reference_sha256": "b" * 64,
                "classification": "auto_reply",
                "confidence": 0.85,
                "suggested_action": "wait_unless_action_requested",
                "failed_recipient_count": 0,
                "smtp_codes": [],
                "received_at": None,
            },
        )
        stale = {
            "id": WORK_ITEM_ID,
            "businessUnit": {"id": BUSINESS_UNIT_ID},
            "revision": 4,
            "detail": "Existing",
        }
        with self.assertRaises(ubd_connector.UbdConnectorError):
            ubd_connector.proposal_payload(event, stale, expected_revision=3)

        current = {**stale, "revision": 3, "detail": "x" * 1_990}
        with self.assertRaises(ubd_connector.UbdConnectorError):
            ubd_connector.proposal_payload(event, current, expected_revision=3)

    def test_proposal_refuses_ambiguous_connector_markers(self) -> None:
        with self.assertRaises(ubd_connector.UbdConnectorError):
            ubd_connector.merge_work_detail(
                "[UBD Mail Connector]\nOne\n[/UBD Mail Connector]\n[UBD Mail Connector]\nTwo\n[/UBD Mail Connector]",
                "Fresh result",
            )

    def test_proposal_receipt_must_remain_pending_human_review(self) -> None:
        response = {
            "schemaVersion": "ubd-agent-api/v1",
            "requestId": PROPOSAL_ID,
            "tool": "work.propose_update",
            "data": {
                "proposal": {
                    "id": PROPOSAL_ID,
                    "workItemId": WORK_ITEM_ID,
                    "basedOnRevision": 3,
                    "status": "pending",
                    "applied": False,
                    "worker": {"id": WORKER_ID},
                }
            },
            "safety": {
                "readOnly": False,
                "applied": False,
                "requiresHumanReview": True,
            },
        }
        checked = ubd_connector.verify_proposal_response(
            response,
            expected_work_item_id=WORK_ITEM_ID,
            expected_revision=3,
            expected_request_id=PROPOSAL_ID,
        )
        self.assertEqual(checked["data"]["proposal"]["id"], PROPOSAL_ID)

        unsafe = {**response, "safety": {**response["safety"], "applied": True}}
        with self.assertRaises(ubd_connector.UbdConnectorError):
            ubd_connector.verify_proposal_response(unsafe)


if __name__ == "__main__":
    unittest.main()
