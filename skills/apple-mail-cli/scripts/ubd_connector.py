"""Safe, minimal bridge from Apple Mail audit results to UBD OS Agent API v1."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


CONNECTOR_SCHEMA_VERSION = "ubd-mail-connector/v1"
AGENT_API_SCHEMA_VERSION = "ubd-agent-api/v1"
MAX_RESPONSE_BYTES = 1_048_576
WORK_DETAIL_LIMIT = 2_000
WORK_REASON_LIMIT = 1_000
MARKER_START = "[UBD Mail Connector]"
MARKER_END = "[/UBD Mail Connector]"
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
CAMPAIGN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
SMTP_CODE_PATTERN = re.compile(r"^[245](?:\d{2}|\.\d\.\d)$")
CLASSIFICATIONS = {
    "bounce_transient_dns",
    "bounce_permanent",
    "bounce_unknown",
    "dmarc_report",
    "auto_reply_with_contacts",
    "auto_reply",
    "out_of_office",
    "unsubscribe_request",
    "complaint",
    "human_positive",
    "human_negative",
    "human_question",
    "likely_human_reply",
    "unclassified",
}
CAMPAIGN_STATUSES = {"missing", "scheduled", "sent", "wrong_time", "duplicate"}
CAMPAIGN_ISSUES = {
    "wrong_time",
    "wrong_sender",
    "sender_unverified",
    "attachment_unverified",
    "wrong_attachment",
    "wrong_recipients",
    "mailbox_unverified",
    "replica_conflict",
}
PROPOSAL_REQUIRED_TOOLS = ["system.capabilities", "work.list", "work.get", "work.propose_update"]


class UbdConnectorError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_uuid(value: str | None, label: str, *, required: bool = True) -> str | None:
    if not value:
        if required:
            raise UbdConnectorError(f"{label} is required and must be a UUID.")
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise UbdConnectorError(f"{label} must be a UUID.") from error
    return str(parsed)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bounded_count(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise UbdConnectorError(f"{label} must be a non-negative integer.")
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError) as error:
        raise UbdConnectorError(f"{label} must be a non-negative integer.") from error
    if parsed < 0 or parsed > 1_000_000_000:
        raise UbdConnectorError(f"{label} is outside the connector count limit.")
    return parsed


def validate_origin(value: str) -> str:
    raw = value.strip()
    parsed = urlparse(raw)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UbdConnectorError("UBD Agent API origin cannot contain credentials, query parameters, or fragments.")
    if parsed.path not in {"", "/"}:
        raise UbdConnectorError("UBD Agent API origin must not contain a path.")
    if not parsed.hostname:
        raise UbdConnectorError("UBD Agent API origin is invalid.")
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise UbdConnectorError("UBD Agent API requires HTTPS; HTTP is allowed only for local development.")
    return f"{parsed.scheme}://{parsed.netloc}"


def event_id(event_type: str, scope: dict[str, Any], payload: dict[str, Any]) -> str:
    identity = canonical_json({"event_type": event_type, "scope": scope, "payload": payload})
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{CONNECTOR_SCHEMA_VERSION}:{identity}"))


def build_event(
    event_type: str,
    *,
    business_unit_id: str,
    work_item_id: str | None,
    payload: dict[str, Any],
    observed_at: str | None = None,
) -> dict[str, Any]:
    scope = {
        "business_unit_id": checked_uuid(business_unit_id, "business_unit_id"),
        "work_item_id": checked_uuid(work_item_id, "work_item_id", required=False),
    }
    event = {
        "schema_version": CONNECTOR_SCHEMA_VERSION,
        "event": {
            "id": event_id(event_type, scope, payload),
            "type": event_type,
            "observed_at": observed_at or utc_now(),
            "source": {
                "provider": "apple_mail",
                "adapter": "local_read_only",
            },
            "scope": scope,
            "payload": payload,
        },
        "privacy": {
            "contains_message_body": False,
            "contains_email_address": False,
            "contains_subject": False,
            "contains_attachment_content": False,
        },
        "authority": {
            "source_of_truth": "ubd_os",
            "formal_state_changed": False,
            "human_review_required": True,
        },
    }
    assert_redacted_event(event)
    return event


def assert_redacted_event(event: dict[str, Any]) -> None:
    encoded = canonical_json(event)
    if EMAIL_PATTERN.search(encoded):
        raise UbdConnectorError("The UBD event contains an email address and was blocked.")
    forbidden_keys = {
        "address",
        "addresses",
        "attachment",
        "body",
        "subject",
        "sender",
        "to",
        "cc",
        "bcc",
        "mailbox",
        "account",
        "attachments",
        "content",
        "headers",
        "html",
        "raw",
        "recipient",
        "recipients",
        "reply_to",
        "results",
        "matches",
    }

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in forbidden_keys:
                    raise UbdConnectorError(f"The UBD event contains forbidden field '{key}'.")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(event)


def classification_event(
    message: dict[str, Any],
    classification: dict[str, Any],
    *,
    business_unit_id: str,
    work_item_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    raw_message_id = str(message.get("message_id") or message.get("id") or "").strip()
    if not raw_message_id:
        raise UbdConnectorError("Apple Mail did not provide a stable message reference.")
    kind = str(classification.get("kind") or "unclassified")
    if kind not in CLASSIFICATIONS:
        raise UbdConnectorError("Classification kind is not part of the connector contract.")
    confidence = float(classification.get("confidence") or 0)
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        raise UbdConnectorError("Classification confidence must be between 0 and 1.")
    action_codes = {
        "bounce_transient_dns": "hold_and_review",
        "bounce_permanent": "suppress_and_find_contact",
        "bounce_unknown": "review_delivery_failure",
        "dmarc_report": "no_reply_review_authentication",
        "auto_reply_with_contacts": "review_alternate_contact",
        "auto_reply": "wait_unless_action_requested",
        "out_of_office": "wait_for_return_or_use_alternate_contact",
        "unsubscribe_request": "suppress_and_record_opt_out",
        "complaint": "stop_outreach_and_review_complaint",
        "human_positive": "prioritize_sales_follow_up",
        "human_negative": "record_decline_and_stop_follow_up",
        "human_question": "answer_question_or_send_requested_material",
        "likely_human_reply": "review_and_follow_up",
        "unclassified": "manual_review",
    }
    smtp_codes = sorted({str(value) for value in classification.get("smtp_codes") or []})[:20]
    if any(not SMTP_CODE_PATTERN.fullmatch(value) for value in smtp_codes):
        raise UbdConnectorError("Classification contains an invalid SMTP status code.")
    payload = {
        "message_reference_sha256": sha256_text(raw_message_id.lower()),
        "classification": kind,
        "confidence": round(confidence, 4),
        "suggested_action": action_codes.get(kind, "manual_review"),
        "failed_recipient_count": len(set(classification.get("failed_recipients") or [])),
        "discovered_contact_count": len(set(classification.get("discovered_contacts") or [])),
        "smtp_codes": smtp_codes,
        "received_at": message.get("date_received"),
    }
    return build_event(
        "mail.message.classified",
        business_unit_id=business_unit_id,
        work_item_id=work_item_id,
        payload=payload,
        observed_at=observed_at,
    )


def campaign_dashboard_event(
    manifest_path: str,
    dashboard: dict[str, Any],
    *,
    business_unit_id: str,
    work_item_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise UbdConnectorError(f"Campaign manifest not found: {path}")
    campaign_id = str(dashboard.get("campaign_id") or "")
    if not CAMPAIGN_ID_PATTERN.fullmatch(campaign_id):
        raise UbdConnectorError("Campaign dashboard contains an invalid campaign_id.")
    campaign = dashboard.get("campaign")
    if not isinstance(campaign, dict):
        raise UbdConnectorError("Campaign dashboard summary is missing.")
    statuses = campaign.get("statuses") if isinstance(campaign.get("statuses"), dict) else {}
    issues = campaign.get("issues") if isinstance(campaign.get("issues"), dict) else {}
    if set(statuses) - CAMPAIGN_STATUSES or set(issues) - CAMPAIGN_ISSUES:
        raise UbdConnectorError("Campaign dashboard contains an unsupported status or issue code.")
    status_counts = {str(key): bounded_count(value, f"status_counts.{key}") for key, value in sorted(statuses.items())}
    issue_counts = {str(key): bounded_count(value, f"issue_counts.{key}") for key, value in sorted(issues.items())}
    volume = dashboard.get("account_volume_by_day") if isinstance(dashboard.get("account_volume_by_day"), dict) else {}
    daily_volume = []
    for day, counts in sorted(volume.items()):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)) or not isinstance(counts, dict):
            raise UbdConnectorError("Campaign dashboard contains an invalid daily volume row.")
        daily_volume.append(
            {
                "day": str(day),
                "sent_count": bounded_count(counts.get("sent"), f"daily_volume.{day}.sent"),
                "scheduled_count": bounded_count(counts.get("scheduled"), f"daily_volume.{day}.scheduled"),
                "total_count": bounded_count(counts.get("total"), f"daily_volume.{day}.total"),
                "over_limit": bool(counts.get("over_limit")),
            }
        )
    triage = dashboard.get("triage") if isinstance(dashboard.get("triage"), dict) else {}
    triage_counts = triage.get("classifications") if isinstance(triage.get("classifications"), dict) else {}
    if set(triage_counts) - CLASSIFICATIONS:
        raise UbdConnectorError("Campaign dashboard contains an unsupported triage classification.")
    payload = {
        "campaign_id": campaign_id,
        "manifest_sha256": sha256_file(path),
        "complete": bool(campaign.get("complete")),
        "safe_to_resume_missing": bool(campaign.get("safe_to_resume_missing")),
        "status_counts": status_counts,
        "issue_counts": issue_counts,
        "daily_volume": daily_volume,
        "triage_matched_count": bounded_count(triage.get("matched"), "triage.matched"),
        "triage_counts": {
            str(key): bounded_count(value, f"triage_counts.{key}") for key, value in sorted(triage_counts.items())
        },
    }
    return build_event(
        "mail.campaign.dashboard",
        business_unit_id=business_unit_id,
        work_item_id=work_item_id,
        payload=payload,
        observed_at=observed_at,
    )


def campaign_audit_event(
    manifest_path: str,
    audit: dict[str, Any],
    *,
    business_unit_id: str,
    work_item_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise UbdConnectorError(f"Campaign manifest not found: {path}")
    summary = audit.get("summary")
    if not isinstance(summary, dict):
        raise UbdConnectorError("Campaign audit summary is missing.")
    statuses = summary.get("statuses") if isinstance(summary.get("statuses"), dict) else {}
    issues = summary.get("issues") if isinstance(summary.get("issues"), dict) else {}
    campaign_id = str(audit.get("campaign_id") or "")
    if not CAMPAIGN_ID_PATTERN.fullmatch(campaign_id):
        raise UbdConnectorError("Campaign audit contains an invalid campaign_id.")
    status_counts = {
        str(key): bounded_count(value, f"status_counts.{key}") for key, value in sorted(statuses.items())
    }
    issue_counts = {
        str(key): bounded_count(value, f"issue_counts.{key}") for key, value in sorted(issues.items())
    }
    if set(status_counts) - CAMPAIGN_STATUSES or set(issue_counts) - CAMPAIGN_ISSUES:
        raise UbdConnectorError("Campaign audit contains an unsupported status or issue code.")
    numeric_values = [
        bounded_count(summary.get("active"), "summary.active"),
        bounded_count(summary.get("mail_index_rows_scanned"), "summary.mail_index_rows_scanned"),
        bounded_count(summary.get("unique_message_ids_scanned"), "summary.unique_message_ids_scanned"),
        bounded_count(summary.get("replica_rows_ignored"), "summary.replica_rows_ignored"),
        *status_counts.values(),
        *issue_counts.values(),
    ]
    if sum(status_counts.values()) != numeric_values[0]:
        raise UbdConnectorError("Campaign audit status counts do not match its active count.")
    payload = {
        "campaign_id": campaign_id,
        "manifest_sha256": sha256_file(path),
        "complete": bool(audit.get("complete")),
        "safe_to_resume_missing": bool(audit.get("safe_to_resume_missing")),
        "active_count": numeric_values[0],
        "status_counts": status_counts,
        "issue_counts": issue_counts,
        "mail_index_rows_scanned": numeric_values[1],
        "unique_message_ids_scanned": numeric_values[2],
        "replica_rows_ignored": numeric_values[3],
    }
    return build_event(
        "mail.campaign.reconciled",
        business_unit_id=business_unit_id,
        work_item_id=work_item_id,
        payload=payload,
        observed_at=observed_at,
    )


def event_summary(event: dict[str, Any]) -> str:
    data = event["event"]
    payload = data["payload"]
    if data["type"] == "mail.campaign.reconciled":
        statuses = ", ".join(f"{key}={value}" for key, value in payload["status_counts"].items()) or "none"
        issues = ", ".join(f"{key}={value}" for key, value in payload["issue_counts"].items()) or "none"
        return (
            f"Mail campaign reconciliation: {payload['campaign_id']}\n"
            f"Status: {statuses}; issues: {issues}; complete={str(payload['complete']).lower()}; "
            f"safe_to_resume_missing={str(payload['safe_to_resume_missing']).lower()}.\n"
            f"Event: {data['id']} · observed {data['observed_at']}"
        )
    if data["type"] == "mail.message.classified":
        return (
            f"Mail classification: {payload['classification']} "
            f"({payload['confidence']:.2f}); next={payload['suggested_action']}.\n"
            f"Event: {data['id']} · observed {data['observed_at']}"
        )
    if data["type"] == "mail.campaign.dashboard":
        statuses = ", ".join(f"{key}={value}" for key, value in payload["status_counts"].items()) or "none"
        issues = ", ".join(f"{key}={value}" for key, value in payload["issue_counts"].items()) or "none"
        over_limit_days = sum(1 for item in payload["daily_volume"] if item["over_limit"])
        return (
            f"Mail campaign dashboard: {payload['campaign_id']}\n"
            f"Status: {statuses}; issues: {issues}; over_limit_days={over_limit_days}; "
            f"triage_matched={payload['triage_matched_count']}.\n"
            f"Event: {data['id']} · observed {data['observed_at']}"
        )
    raise UbdConnectorError(f"Unsupported UBD Mail event type: {data['type']}")


def merge_work_detail(existing: str | None, block: str) -> str:
    base = (existing or "").strip()
    start_count = base.count(MARKER_START)
    end_count = base.count(MARKER_END)
    if start_count != end_count or start_count > 1:
        raise UbdConnectorError("Existing work detail contains ambiguous UBD Mail Connector markers.")
    if start_count == 1:
        start = base.find(MARKER_START)
        end = base.find(MARKER_END)
        if end < start:
            raise UbdConnectorError("Existing work detail contains an incomplete UBD Mail Connector marker.")
        end += len(MARKER_END)
        base = (base[:start] + base[end:]).strip()
    section = f"{MARKER_START}\n{block.strip()}\n{MARKER_END}"
    merged = f"{base}\n\n{section}" if base else section
    if len(merged) > WORK_DETAIL_LIMIT:
        raise UbdConnectorError(
            "The UBD work detail is too long to append the reconciliation safely; shorten it in UBD OS first."
        )
    return merged


def proposal_payload(event: dict[str, Any], work: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
    if event.get("schema_version") != CONNECTOR_SCHEMA_VERSION:
        raise UbdConnectorError("The UBD Mail event schema is unsupported.")
    assert_redacted_event(event)
    authority = event.get("authority")
    if not isinstance(authority, dict) or authority.get("formal_state_changed") is not False or authority.get("human_review_required") is not True:
        raise UbdConnectorError("The UBD Mail event does not preserve the proposal-only authority boundary.")
    event_data = event.get("event")
    if not isinstance(event_data, dict) or event_data.get("type") not in {
        "mail.campaign.reconciled",
        "mail.message.classified",
        "mail.campaign.dashboard",
    }:
        raise UbdConnectorError("The UBD Mail event type cannot be attached to work.")
    if expected_revision < 1:
        raise UbdConnectorError("expected_revision must be positive.")
    scope = event_data.get("scope")
    if not isinstance(scope, dict):
        raise UbdConnectorError("The UBD Mail event scope is missing.")
    if work.get("id") != scope.get("work_item_id"):
        raise UbdConnectorError("The UBD event and work item do not match.")
    if work.get("businessUnit", {}).get("id") != scope.get("business_unit_id"):
        raise UbdConnectorError("The UBD event and work item business unit do not match.")
    if work.get("revision") != expected_revision:
        raise UbdConnectorError("The UBD work item changed after the requested revision.")
    summary = event_summary(event)
    reason = f"Apple Mail read-only reconciliation event {event['event']['id']} is ready for human review."
    if len(reason) > WORK_REASON_LIMIT:
        raise UbdConnectorError("The UBD proposal reason is too long.")
    return {
        "businessUnitId": scope["business_unit_id"],
        "workItemId": scope["work_item_id"],
        "expectedRevision": expected_revision,
        "patch": {"detail": merge_work_detail(work.get("detail"), summary)},
        "reason": reason,
    }


def verify_capabilities(
    response: dict[str, Any],
    *,
    required_tools: Iterable[str],
    business_unit_id: str | None = None,
) -> dict[str, Any]:
    if response.get("schemaVersion") != AGENT_API_SCHEMA_VERSION or not isinstance(response.get("data"), dict):
        raise UbdConnectorError("UBD Agent API capabilities response failed its contract.")
    data = response["data"]
    tools = {item.get("name") for item in data.get("tools", []) if isinstance(item, dict)}
    missing = sorted(set(required_tools) - tools)
    if missing:
        raise UbdConnectorError(f"UBD Agent API token is missing required tools: {', '.join(missing)}")
    policy = data.get("policy")
    if not isinstance(policy, dict) or any(policy.get(key) is not False for key in ["directDatabaseAccess", "send", "publish", "payments", "delete"]):
        raise UbdConnectorError("UBD Agent API policy is broader than the Mail connector allows.")
    if policy.get("humanReviewRequiredForProposals") is not True:
        raise UbdConnectorError("UBD Agent API does not confirm human review for proposals.")
    if business_unit_id:
        allowed = {item.get("id") for item in data.get("businessUnits", []) if isinstance(item, dict)}
        if business_unit_id not in allowed:
            raise UbdConnectorError("UBD Agent API token cannot access the requested business unit.")
    return data


def verify_tool_response(response: dict[str, Any], tool: str) -> dict[str, Any]:
    if response.get("schemaVersion") != AGENT_API_SCHEMA_VERSION or response.get("tool") != tool:
        raise UbdConnectorError("UBD Agent API tool response failed its contract.")
    if not isinstance(response.get("data"), dict) or not isinstance(response.get("safety"), dict):
        raise UbdConnectorError("UBD Agent API tool response is incomplete.")
    return response


def verify_proposal_response(
    response: dict[str, Any],
    *,
    expected_work_item_id: str | None = None,
    expected_revision: int | None = None,
    expected_request_id: str | None = None,
) -> dict[str, Any]:
    checked = verify_tool_response(response, "work.propose_update")
    safety = checked["safety"]
    if safety.get("applied") is not False or safety.get("requiresHumanReview") is not True:
        raise UbdConnectorError("UBD OS did not preserve the proposal-only human-review boundary.")
    proposal = checked["data"].get("proposal")
    if not isinstance(proposal, dict):
        raise UbdConnectorError("UBD OS did not return a stored proposal receipt.")
    checked_uuid(str(proposal.get("id") or ""), "proposal.id")
    proposal_work_item_id = checked_uuid(str(proposal.get("workItemId") or ""), "proposal.workItemId")
    if proposal.get("status") != "pending" or proposal.get("applied") is not False:
        raise UbdConnectorError("UBD OS proposal receipt is not pending human review.")
    if not isinstance(proposal.get("basedOnRevision"), int) or proposal["basedOnRevision"] < 1:
        raise UbdConnectorError("UBD OS proposal receipt contains an invalid base revision.")
    if expected_work_item_id and proposal_work_item_id != checked_uuid(expected_work_item_id, "expected_work_item_id"):
        raise UbdConnectorError("UBD OS proposal receipt belongs to another work item.")
    if expected_revision is not None and proposal["basedOnRevision"] != expected_revision:
        raise UbdConnectorError("UBD OS proposal receipt is based on another work revision.")
    if expected_request_id and checked.get("requestId") != checked_uuid(expected_request_id, "expected_request_id"):
        raise UbdConnectorError("UBD OS proposal receipt does not match the idempotency key.")
    return checked


class AgentApiClient:
    def __init__(self, origin: str, token: str, *, timeout: int = 30) -> None:
        self.origin = validate_origin(origin)
        self.token = token.strip()
        if not self.token:
            raise UbdConnectorError("UBD Agent API token is unavailable.")
        if len(self.token) > 4_096 or any(ord(character) < 32 or ord(character) == 127 for character in self.token):
            raise UbdConnectorError("UBD Agent API token contains unsafe characters or exceeds the local limit.")
        if timeout < 1 or timeout > 60:
            raise UbdConnectorError("UBD Agent API timeout must be between 1 and 60 seconds.")
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "apple-mail-cli-ubd-connector/1",
        }
        body = None
        if payload is not None:
            body = canonical_json(payload).encode("utf-8")
            if len(body) > 65_536:
                raise UbdConnectorError("UBD Agent API request exceeds the 64 KiB limit.")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = str(checked_uuid(idempotency_key, "idempotency_key"))
        request = Request(f"{self.origin}{path}", data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared_length = int(content_length)
                    except ValueError as error:
                        raise UbdConnectorError("UBD Agent API returned an invalid Content-Length header.") from error
                    if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                        raise UbdConnectorError("UBD Agent API response exceeds the local safety limit.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise UbdConnectorError("UBD Agent API response exceeds the local safety limit.")
        except HTTPError as error:
            raw = error.read(MAX_RESPONSE_BYTES)
            try:
                parsed = json.loads(raw.decode("utf-8"))
                api_error = parsed.get("error", {})
                code = api_error.get("code", f"http_{error.code}")
                message = api_error.get("message", "UBD Agent API rejected the request.")
                request_id = parsed.get("requestId")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                code = f"http_{error.code}"
                message = "UBD Agent API rejected the request."
                request_id = None
            suffix = f" Request ID: {request_id}" if request_id else ""
            raise UbdConnectorError(f"{code}: {message}{suffix}") from None
        except (URLError, TimeoutError):
            raise UbdConnectorError("UBD Agent API connection failed; no mutation result was assumed.") from None
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UbdConnectorError("UBD Agent API returned invalid JSON.") from error
        if not isinstance(result, dict):
            raise UbdConnectorError("UBD Agent API returned an invalid response object.")
        return result

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/api/agent/v1/capabilities")

    def tool(self, name: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        return self._request(
            "POST",
            f"/api/agent/v1/tools/{name}",
            payload=payload,
            idempotency_key=key,
        )
