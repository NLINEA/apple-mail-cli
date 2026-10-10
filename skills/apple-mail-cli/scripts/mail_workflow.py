"""Read-only post-send observations and human-review follow-up worklists.

Metadata is not MIME integrity, transport delivery, or authorization to follow up.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import campaign
import mail_index
import operation_state

RFC_ID = re.compile(r"<[^<>\s]+@[^<>\s]+>")


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def addresses(row, field):
    return sorted(str(value).strip().lower() for value in row.get(field, []))


def receipt_reconcile(operation_id, message_id, account_key, *, database=None):
    receipt = operation_state.get(operation_id)
    intent = receipt.get("send_intent")
    if receipt.get("command") != "send" or not intent:
        raise ValueError("Receipt has no saved send intent; inspect older operations manually.")
    if receipt["state"] not in {"submitting", "accepted", "unknown"}:
        raise ValueError("This receipt is not a dispatched or uncertain send.")
    if not account_key or not RFC_ID.fullmatch(message_id):
        raise ValueError("Use an exact account_key and RFC Message-ID, not a numeric draft ID.")
    found = mail_index.search_messages(database=database, states={"sent"}, account=account_key,
                                      message_id=message_id, limit=1000)
    if found["has_more"]:
        raise ValueError("Sent evidence exceeds the bound; no reconciliation was saved.")
    rows = [row for row in found["messages"] if not row.get("deleted")]
    issues = []
    if not rows:
        issues.append("sent_not_observed")
    started = timestamp(receipt["created_at"])
    observed_at = datetime.now(timezone.utc)
    for row in rows:
        if row.get("state") != "sent" or row.get("account_key") != account_key or row.get("message_id") != message_id:
            issues.append("identity_mismatch")
        for field in ("to", "cc", "bcc"):
            if addresses(row, field) != sorted(intent[field]):
                issues.append("wrong_" + field)
        if addresses(row, "sender_addresses") != [intent["sender"]]:
            issues.append("sender_mismatch_or_unavailable")
        if row.get("subject") != intent["subject"]:
            issues.append("wrong_subject")
        sent_at = timestamp(row.get("date_sent"))
        if not started or not sent_at or sent_at < started.replace(microsecond=0) or sent_at > observed_at:
            issues.append("sent_time_outside_operation")
        if (Counter(row.get("attachments") or []) != Counter(item["name"] for item in intent["attachments"])
                or row.get("attachment_count") != len(intent["attachments"])):
            issues.append("attachment_metadata_mismatch_or_unavailable")
    # Examine all replicas, not just the richest row. Conflicting dates also
    # remain ambiguous even when both dates fall inside the operation window.
    if len({row.get("date_sent") for row in rows}) > 1:
        issues.append("conflicting_sent_dates")
    observation = {"observed_at": observed_at.isoformat(), "message_id": message_id, "account_key": account_key,
                   "sent_observed": bool(rows) and not issues, "issues": sorted(set(issues)),
                   "evidence": "exact_account_and_message_id_sent_index_metadata",
                   "row_ids": [row["row_id"] for row in rows], "verified": False,
                   "body_verified": False, "attachment_bytes_verified": False,
                   "operation_correlation_verified": False, "delivery_verified": False,
                   "requires_review": True, "retry_authorized": False}
    operation_state.annotate(operation_id, "sent_observation", observation)
    return {"ok": not issues, "operation_id": operation_id, "dispatch_state": receipt["state"], "data": observation}


def followups(manifest_path, account_key, *, database=None, after_days=7, as_of=None):
    if not account_key or not 1 <= after_days <= 365:
        raise ValueError("An exact account_key and --after-days between 1 and 365 are required.")
    now = timestamp(as_of) if as_of else datetime.now(timezone.utc)
    if now is None:
        raise ValueError("--as-of must include a timezone offset.")
    report = campaign.validate_manifest(manifest_path, enforce_schedule_policy=False)
    campaign.require_valid(report)
    index_path, all_rows = mail_index.list_messages(database=database, limit=100000)
    if len(all_rows) >= 100000:
        raise ValueError("Follow-up evidence reached the 100,000-row bound; no complete worklist can be claimed.")
    rows = [row for row in all_rows if row.get("account_key") == account_key]
    by_subject, by_thread, by_sender = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        by_subject[row.get("subject")].append(row)
        if row.get("conversation_id"):
            by_thread[row["conversation_id"]].append(row)
        for address in addresses(row, "sender_addresses"):
            by_sender[address].append(row)
    items = []
    for source in report["active_messages"]:
        matching = [row for row in by_subject[source["subject"]]
                    if row.get("state") in {"sent", "scheduled"} and not row.get("deleted")
                    and addresses(row, "sender_addresses") == [source["sender"].lower()]
                    and all(addresses(row, field) == addresses(source, field) for field in ("to", "cc", "bcc"))]
        unique = mail_index.unique_messages(matching)
        status, reasons, related, due_at = "not_sent_observed", [], [], None
        if len(unique) > 1:
            status = "ambiguous_mail_review"
        elif unique:
            sent = unique[0]
            sent_at = timestamp(sent.get("date_sent"))
            sent_replicas = [row for row in matching if row["message_id"] == sent["message_id"] and row["state"] == "sent"]
            if sent.get("replica_conflicts") or len({row.get("date_sent") for row in sent_replicas}) > 1:
                status = "ambiguous_mail_review"
            elif sent["state"] == "scheduled":
                status = "scheduled_no_followup"
            elif (not sent_at or not sent.get("conversation_id") or sent_at > now
                  or not RFC_ID.fullmatch(str(sent["message_id"]))):
                status = "insufficient_evidence_review"
            else:
                due_at = sent_at + timedelta(days=after_days)
                contacts = {value for field in ("to", "cc", "bcc") for value in addresses(source, field)}
                possible = {row["row_id"]: row for row in by_thread[sent["conversation_id"]]}
                for contact in contacts:
                    possible.update({row["row_id"]: row for row in by_sender[contact]})
                for row in possible.values():
                    if row["message_id"] == sent["message_id"]:
                        continue
                    date = timestamp(row.get("date_received")) or timestamp(row.get("date_sent"))
                    if date is None or sent_at <= date <= now:
                        related.append({"row_id": row["row_id"], "message_id": row["message_id"], "state": row["state"],
                                        "same_conversation": row.get("conversation_id") == sent["conversation_id"]})
                if related:
                    status = "related_activity_review"
                    reasons.append("Review possible replies, bounces, opt-outs and later sends before considering contact.")
                else:
                    status = "follow_up_review_due" if now >= due_at else "waiting"
                    reasons.append("No later activity observed in this local index scope; this is not proof of no reply.")
        if status in {"not_sent_observed", "ambiguous_mail_review", "insufficient_evidence_review"}:
            reasons.append("Resolve missing or ambiguous Sent identity before planning any follow-up.")
        items.append({"key": source["key"], "company": source.get("company"), "to": source["to"],
                      "subject": source["subject"], "status": status,
                      "sent_message_ids": [row["message_id"] for row in unique if row["state"] == "sent"],
                      "review_due_at": due_at.isoformat() if due_at else None,
                      "related_activity": sorted(related, key=lambda row: row["row_id"]), "reasons": reasons,
                      "requires_human_review": True, "send_authorized": False})
    return {"ok": True, "schema_version": "apple-mail-cli-followups/v1", "campaign_id": report["campaign_id"],
            "observed_at": operation_state.now(), "as_of": now.isoformat(), "account_key": account_key,
            "after_days": after_days, "mail_index": str(index_path), "manifest_sha256": campaign.sha256(Path(report["manifest_path"])),
            "complete": True, "scope": "local_index_metadata_only", "mail_mutated": False,
            "skipped_manifest_rows": len(report["skipped_messages"]),
            "as_of_semantics": "current_index_evaluated_at_date_not_historical_snapshot",
            "automatic_followup": False, "human_review_only": True,
            "required_checks": ["Review full conversation and other accounts", "Check opt-outs, complaints and bounces",
                                "Verify original body and attachments", "Obtain fresh approval for final recipients and message"],
            "summary": dict(sorted(Counter(row["status"] for row in items).items())), "items": items}
