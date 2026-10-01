from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "apple-mail-cli" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ubd_connector  # noqa: E402


BUSINESS_UNIT_ID = "10000000-0000-4000-8000-000000000001"
WORK_ITEM_ID = "20000000-0000-4000-8000-000000000001"
PROPOSAL_ID = "30000000-0000-4000-8000-000000000001"
WORKER_ID = "40000000-0000-4000-8000-000000000001"
REQUEST_ID = "50000000-0000-4000-8000-000000000001"


class FakeAgentApiHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    redirect_capabilities = False
    redirected_request_seen = False

    def log_message(self, format: str, *args: object) -> None:
        return

    def reply(self, value: dict[str, Any], status: int = 200, **headers: str) -> None:
        encoded = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for key, item in headers.items():
            self.send_header(key, item)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/redirect-target":
            type(self).redirected_request_seen = True
            self.reply({"unexpected": True})
            return
        if type(self).redirect_capabilities:
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.end_headers()
            return
        type(self).requests.append({"method": "GET", "path": self.path, "authorization": self.headers.get("Authorization")})
        self.reply(
            {
                "schemaVersion": "ubd-agent-api/v1",
                "data": {
                    "tools": [{"name": name} for name in ubd_connector.PROPOSAL_REQUIRED_TOOLS],
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
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        request = {
            "method": "POST",
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "idempotency_key": self.headers.get("Idempotency-Key"),
            "body": body,
        }
        type(self).requests.append(request)
        if self.path.endswith("/work.get"):
            self.reply(
                {
                    "schemaVersion": "ubd-agent-api/v1",
                    "tool": "work.get",
                    "data": {
                        "item": {
                            "id": WORK_ITEM_ID,
                            "businessUnit": {"id": BUSINESS_UNIT_ID},
                            "revision": 3,
                            "detail": "Human-authored context.",
                        }
                    },
                    "safety": {"readOnly": True, "applied": False, "requiresHumanReview": False},
                }
            )
            return
        if self.path.endswith("/work.propose_update"):
            self.reply(
                {
                    "schemaVersion": "ubd-agent-api/v1",
                    "requestId": self.headers.get("Idempotency-Key"),
                    "tool": "work.propose_update",
                    "data": {
                        "proposal": {
                            "id": PROPOSAL_ID,
                            "workItemId": WORK_ITEM_ID,
                            "basedOnRevision": 3,
                            "status": "pending",
                            "worker": {"id": WORKER_ID, "name": "Hermes"},
                            "applied": False,
                        }
                    },
                    "safety": {"readOnly": False, "applied": False, "requiresHumanReview": True},
                }
            )
            return
        self.reply({"error": {"code": "not_found", "message": "Not found"}}, status=404)


class UbdHttpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeAgentApiHandler.requests = []
        FakeAgentApiHandler.redirect_capabilities = False
        FakeAgentApiHandler.redirected_request_seen = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgentApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_real_http_flow_preserves_redaction_scope_and_pending_proposal(self) -> None:
        client = ubd_connector.AgentApiClient(self.origin, "local-test-token")
        capabilities = client.capabilities()
        ubd_connector.verify_capabilities(
            capabilities,
            required_tools=ubd_connector.PROPOSAL_REQUIRED_TOOLS,
            business_unit_id=BUSINESS_UNIT_ID,
        )
        work_response = ubd_connector.verify_tool_response(
            client.tool("work.get", {"businessUnitId": BUSINESS_UNIT_ID, "workItemId": WORK_ITEM_ID}),
            "work.get",
        )
        event = ubd_connector.classification_event(
            {"message_id": "<private@example.com>", "date_received": "2026-08-18T01:00:00Z"},
            {
                "kind": "likely_human_reply",
                "confidence": 0.8,
                "failed_recipients": [],
                "smtp_codes": [],
            },
            business_unit_id=BUSINESS_UNIT_ID,
            work_item_id=WORK_ITEM_ID,
        )
        proposal = ubd_connector.proposal_payload(event, work_response["data"]["item"], expected_revision=3)
        receipt = client.tool("work.propose_update", proposal, idempotency_key=REQUEST_ID)
        ubd_connector.verify_proposal_response(
            receipt,
            expected_work_item_id=WORK_ITEM_ID,
            expected_revision=3,
            expected_request_id=REQUEST_ID,
        )

        self.assertEqual([item["authorization"] for item in FakeAgentApiHandler.requests], ["Bearer local-test-token"] * 3)
        proposal_request = FakeAgentApiHandler.requests[-1]
        self.assertEqual(proposal_request["idempotency_key"], REQUEST_ID)
        encoded = json.dumps(proposal_request["body"])
        self.assertNotIn("private@example.com", encoded)
        self.assertIn("likely_human_reply", encoded)

    def test_bearer_token_is_never_forwarded_through_redirect(self) -> None:
        FakeAgentApiHandler.redirect_capabilities = True
        client = ubd_connector.AgentApiClient(self.origin, "must-not-be-forwarded")
        with self.assertRaises(ubd_connector.UbdConnectorError):
            client.capabilities()
        self.assertFalse(FakeAgentApiHandler.redirected_request_seen)


if __name__ == "__main__":
    unittest.main()
