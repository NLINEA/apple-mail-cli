#!/usr/bin/env python3
"""JSON-first CLI for Apple Mail and outreach campaign operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import campaign
import mail_index
import mail_store
import ubd_connector
import send_guard


VERSION = "0.4.3"
CLI_SCHEMA_VERSION = "apple-mail-cli/v1"
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND = SCRIPT_DIR / "mail_backend.js"
MAIL_APP = Path("/System/Applications/Mail.app")
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
HEADER_CONTROL_PATTERN = re.compile(r"[\u0000-\u001f\u007f]")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_BODY_CHARS = 5_000_000


class MailCtlError(RuntimeError):
    def __init__(self, message: str, *, code: str = "mailctl_error", hint: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.retryable = retryable


def emit(payload: dict[str, Any], *, compact: bool = False) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=None if compact else 2, sort_keys=False))


def run_backend(payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    osascript = shutil.which("osascript")
    if not osascript:
        raise MailCtlError(
            "osascript is unavailable; this CLI requires macOS.",
            code="platform_unavailable",
            hint="Run the Apple Mail CLI on macOS.",
        )
    if not BACKEND.is_file():
        raise MailCtlError(
            f"Apple Mail backend is missing: {BACKEND}",
            code="backend_missing",
            hint="Reinstall the plugin before operating Mail.",
        )
    try:
        completed = subprocess.run(
            [osascript, "-l", "JavaScript", str(BACKEND)],
            input=json.dumps(payload, ensure_ascii=False),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise MailCtlError(
            f"Mail automation timed out after {timeout} seconds.",
            code="mail_automation_timeout",
            hint="Audit actual Mail state before deciding whether to retry.",
            retryable=payload.get("command") != "send",
        ) from error
    except UnicodeError as error:
        raise MailCtlError("Apple Mail returned invalid UTF-8.", code="backend_contract_error") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Apple Mail automation failed."
        if "Not authorized" in detail or "-1743" in detail:
            detail += " Allow Codex or Terminal to control Mail in System Settings > Privacy & Security > Automation."
            raise MailCtlError(
                detail,
                code="mail_permission_denied",
                hint="Grant Automation permission explicitly or stop using the Mail mutation workflow.",
            )
        raise MailCtlError(detail, code="mail_automation_failed", hint="Inspect Mail and run mailctl doctor before retrying.")
    output = completed.stdout.strip()
    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        raise MailCtlError(
            "Apple Mail returned an invalid response.",
            code="backend_contract_error",
            hint="Reinstall the plugin or inspect the backend version reported by mailctl doctor.",
        ) from error
    valid = (
        isinstance(result, dict)
        and type(result.get("ok")) is bool
        and result.get("command") == payload.get("command")
    )
    if valid:
        if result["ok"]:
            valid = isinstance(result.get("data"), (dict, list))
        else:
            valid = isinstance(result.get("error"), dict) and isinstance(result["error"].get("message"), str)
    if not valid:
        raise MailCtlError(
            "Apple Mail returned an invalid response contract.",
            code="backend_contract_error",
            hint="Check that the CLI and backend come from the same source revision.",
        )
    if not result["ok"]:
        message = result["error"]["message"]
        if "not authorized" in message.lower() or "-1743" in message:
            message += " Allow Codex or Terminal to control Mail in System Settings > Privacy & Security > Automation."
            raise MailCtlError(message, code="mail_permission_denied")
        raise MailCtlError(message, code="mail_command_failed", hint="Audit actual Mail state before retrying.")
    return result


def file_digest(path: Path, expected_size: int | None = None) -> str:
    digest = hashlib.sha256()
    if expected_size is None:
        handle = path.open("rb")
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        handle = os.fdopen(descriptor, "rb")
    total = 0
    with handle:
        if expected_size is not None and not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MailCtlError("Direct-send attachment must be a regular file.", code="send_attachment_changed")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            if expected_size is not None and total > expected_size:
                raise MailCtlError("Attachment changed during preflight.", code="send_attachment_changed")
            digest.update(chunk)
    if expected_size is not None and total != expected_size:
        raise MailCtlError("Attachment changed during preflight.", code="send_attachment_changed")
    return digest.hexdigest()


def attachment_preflight(paths: list[str], *, direct_send: bool = False) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    total = 0
    if direct_send and len(paths) > send_guard.MAX_ATTACHMENTS:
        raise MailCtlError("Direct send permits at most 20 attachments.", code="send_attachment_limit")
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise MailCtlError(f"Attachment not found: {path}")
        size = path.stat().st_size
        total += size
        if direct_send and total > send_guard.MAX_ATTACHMENT_BYTES:
            raise MailCtlError("Direct send attachments exceed 25 MiB.", code="send_attachment_limit")
        output.append({"path": str(path), "name": path.name, "size_bytes": size, "sha256": file_digest(path, expected_size=size) if direct_send else file_digest(path)})
    return output


def body_value(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        path = Path(args.body_file).expanduser().resolve()
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise MailCtlError(f"Cannot read body file {path}: {error}") from error
    return args.body


def validated_body(args: argparse.Namespace, label: str = "body") -> str:
    value = body_value(args)
    if not value.strip():
        raise MailCtlError(f"{label} cannot be empty.")
    if len(value) > MAX_BODY_CHARS:
        raise MailCtlError(f"{label} exceeds the {MAX_BODY_CHARS:,}-character local safety limit.")
    return value


def normalize_address(value: str, label: str) -> str:
    raw = str(value or "").strip()
    if not raw or HEADER_CONTROL_PATTERN.search(raw):
        raise MailCtlError(f"{label} contains an invalid email address.")
    _, parsed = parseaddr(raw)
    normalized = parsed.strip().lower()
    if not EMAIL_ADDRESS_PATTERN.fullmatch(normalized):
        raise MailCtlError(f"{label} contains an invalid email address: {raw}")
    return normalized


def normalize_addresses(values: list[str], label: str) -> list[str]:
    return [normalize_address(value, f"{label}[{index}]") for index, value in enumerate(values)]


def outgoing_preflight(args: argparse.Namespace, command: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recipients = {
        "to": normalize_addresses(args.to, "to"),
        "cc": normalize_addresses(args.cc, "cc"),
        "bcc": normalize_addresses(args.bcc, "bcc"),
    }
    seen: dict[str, str] = {}
    for field, addresses in recipients.items():
        for address in addresses:
            if address in seen:
                raise MailCtlError(f"Recipient {address} appears more than once across {seen[address]} and {field}.")
            seen[address] = field
    if not recipients["to"]:
        raise MailCtlError("At least one To recipient is required.")
    subject = str(args.subject or "").strip()
    if not subject:
        raise MailCtlError("Subject cannot be empty.")
    if len(subject) > 998 or HEADER_CONTROL_PATTERN.search(subject):
        raise MailCtlError("Subject contains control characters or exceeds 998 characters.")
    sender = normalize_address(args.sender, "sender") if args.sender else None
    if command == "send" and sender is None:
        raise MailCtlError("Direct send requires an explicit sender identity.", code="sender_required", hint="Select the exact configured identity and pass --sender after reviewing the message.")
    body = validated_body(args)
    evidence = attachment_preflight(args.attachment, direct_send=command == "send")
    payload = {
        "command": command,
        **recipients,
        "sender": sender,
        "subject": subject,
        "body": body,
        "attachments": [item["path"] for item in evidence],
        "confirm_send": bool(getattr(args, "confirm_send", False)),
    }
    return payload, evidence


def add_location(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mailbox", default="INBOX", help="Standard alias or slash-delimited custom mailbox path.")
    parser.add_argument("--account", help="Mail account name when the mailbox is ambiguous.")


def add_body(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--body", help="Plain-text message body.")
    group.add_argument("--body-file", help="UTF-8 file containing the plain-text body.")


def add_outgoing(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", action="append", required=True, help="Recipient; repeat for multiple addresses.")
    parser.add_argument("--cc", action="append", default=[], help="CC recipient; repeat as needed.")
    parser.add_argument("--bcc", action="append", default=[], help="BCC recipient; repeat as needed.")
    parser.add_argument("--sender", help="Configured Mail sender identity.")
    parser.add_argument("--subject", default="", help="Message subject.")
    parser.add_argument("--attachment", action="append", default=[], help="Absolute attachment path; repeat as needed.")
    add_body(parser)


def add_ubd_scope(parser: argparse.ArgumentParser, *, work_required: bool = False) -> None:
    parser.add_argument("--business-unit-id", required=True, help="Exact UBD OS business-unit UUID.")
    parser.add_argument("--work-item-id", required=work_required, help="Exact UBD OS work-item UUID.")


def add_ubd_connection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--origin",
        help="UBD OS origin; defaults to UBD_AGENT_API_ORIGIN. HTTPS is required outside localhost.",
    )
    parser.add_argument(
        "--token-env",
        default="UBD_AGENT_TOKEN",
        help="Environment variable containing the one-time-visible Agent API token.",
    )
    parser.add_argument("--api-timeout", type=int, default=30, help="UBD Agent API timeout in seconds (1-60).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailctl", description="Operate and audit local Apple Mail with structured JSON.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    parser.add_argument("--timeout", type=int, default=90, help="Apple automation timeout in seconds.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capabilities", help="Describe the stable JSON contract and safety classes without accessing Mail.")
    sub.add_parser("doctor", help="Check prerequisites without reading message content.")
    sub.add_parser("accounts", help="List configured Mail accounts.")
    sub.add_parser("selection", help="List messages currently selected in Mail.")

    mailboxes = sub.add_parser("mailboxes", help="List mailbox paths and unread counts.")
    mailboxes.add_argument("--account")
    mailboxes.add_argument("--depth", type=int, default=12)

    listing = sub.add_parser("list", help="List or search messages in a bounded Mail scan.")
    add_location(listing)
    listing.add_argument("--limit", type=int, default=25)
    listing.add_argument("--scan-limit", type=int, default=500)
    listing.add_argument("--query")
    listing.add_argument("--search-body", action="store_true")
    listing.add_argument("--unread", action="store_true")
    listing.add_argument("--flagged", action="store_true")
    listing.add_argument("--since", help="ISO date/time lower bound.")
    listing.add_argument("--before", help="ISO date/time exclusive upper bound.")

    search = sub.add_parser("search", help="Search the complete read-only Mail metadata index with pagination.")
    search.add_argument("--database", help="Explicit Envelope Index path.")
    search.add_argument("--state", action="append", choices=["sent", "scheduled", "draft", "trash", "inbox", "junk", "outbox"])
    search.add_argument("--query", help="Search subject, summary, addresses, and attachment names; message bodies are excluded.")
    search.add_argument("--sender")
    search.add_argument("--recipient")
    search.add_argument("--subject")
    search.add_argument("--attachment")
    attachment_group = search.add_mutually_exclusive_group()
    attachment_group.add_argument("--has-attachment", action="store_true")
    attachment_group.add_argument("--no-attachment", action="store_true")
    read_group = search.add_mutually_exclusive_group()
    read_group.add_argument("--unread", action="store_true")
    read_group.add_argument("--read", action="store_true")
    flagged_group = search.add_mutually_exclusive_group()
    flagged_group.add_argument("--flagged", action="store_true")
    flagged_group.add_argument("--unflagged", action="store_true")
    search.add_argument("--since", help="ISO date/time lower bound with UTC offset.")
    search.add_argument("--before", help="ISO date/time exclusive upper bound with UTC offset.")
    search.add_argument("--limit", type=int, default=100)
    search.add_argument("--cursor", help="Opaque next_cursor returned by a previous identical search.")

    thread = sub.add_parser("thread", help="List one complete Mail conversation from the read-only index.")
    thread.add_argument("conversation_id", type=int)
    thread.add_argument("--database")
    thread.add_argument("--limit", type=int, default=500)
    thread.add_argument("--cursor")

    for name, help_text in [("read", "Read one message and metadata."), ("classify", "Classify a reply, bounce, auto-reply, or DMARC report.")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("message_id", type=int)
        add_location(command)
        command.add_argument("--max-chars", type=int, default=30000)

    triage = sub.add_parser("triage", help="Classify a bounded mailbox batch without returning message bodies.")
    add_location(triage)
    triage.add_argument("--limit", type=int, default=30)
    triage.add_argument("--scan-limit", type=int, default=500)
    triage.add_argument("--since", help="ISO date/time lower bound.")
    triage.add_argument("--unread", action="store_true")
    triage.add_argument("--max-chars", type=int, default=30000)
    triage.add_argument("--output", help="Optional new private JSON artifact for dashboard reconciliation.")

    mark = sub.add_parser("mark", help="Mark a message read or unread.")
    mark.add_argument("message_id", type=int)
    add_location(mark)
    mark_group = mark.add_mutually_exclusive_group(required=True)
    mark_group.add_argument("--read", action="store_true")
    mark_group.add_argument("--unread", action="store_true")
    mark.add_argument("--confirm-change", action="store_true", required=True)

    flag = sub.add_parser("flag", help="Set or clear a message flag.")
    flag.add_argument("message_id", type=int)
    add_location(flag)
    flag_group = flag.add_mutually_exclusive_group(required=True)
    flag_group.add_argument("--on", action="store_true")
    flag_group.add_argument("--off", action="store_true")
    flag.add_argument("--confirm-change", action="store_true", required=True)

    move = sub.add_parser("move", help="Move a message to a custom mailbox.")
    move.add_argument("message_id", type=int)
    add_location(move)
    move.add_argument("--to", dest="to_mailbox", required=True)
    move.add_argument("--to-account")
    move.add_argument("--confirm-change", action="store_true", required=True)

    trash = sub.add_parser("trash", help="Move a message to Trash using Mail's delete behavior.")
    trash.add_argument("message_id", type=int)
    add_location(trash)
    trash.add_argument("--confirm-trash", action="store_true", required=True)

    compose = sub.add_parser("compose", help="Open a visible unsent draft.")
    add_outgoing(compose)

    send = sub.add_parser("send", help="Preview intent offline; send only with its exact approved digest.")
    add_outgoing(send)
    send.add_argument("--confirm-send", metavar="SHA256", help="Exact digest from the reviewed send preview; each intent may be attempted once locally.")

    reply = sub.add_parser("reply", help="Open a visible unsent reply draft.")
    reply.add_argument("message_id", type=int)
    add_location(reply)
    add_body(reply)
    reply.add_argument("--all", dest="reply_all", action="store_true")

    index = sub.add_parser("index", help="Read-only Apple Mail index audit commands.")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    for name in ["list", "counts"]:
        index_parser = index_sub.add_parser(name)
        index_parser.add_argument("--database", help="Explicit Envelope Index path.")
        index_parser.add_argument("--state", action="append", choices=["sent", "scheduled", "draft", "trash", "inbox"])
        index_parser.add_argument("--recipient")
        index_parser.add_argument("--subject")
        index_parser.add_argument("--limit", type=int, default=5000 if name == "counts" else 200)

    campaign_parser = sub.add_parser("campaign", help="Validate, render, plan, or audit an outreach campaign.")
    campaign_sub = campaign_parser.add_subparsers(dest="campaign_command", required=True)
    validate = campaign_sub.add_parser("validate")
    validate.add_argument("manifest")
    plan = campaign_sub.add_parser("plan")
    plan.add_argument("manifest")
    render = campaign_sub.add_parser("render")
    render.add_argument("manifest")
    render.add_argument("--output", required=True)
    render.add_argument("--force", action="store_true")
    audit = campaign_sub.add_parser("audit")
    audit.add_argument("manifest")
    audit.add_argument("--database")
    audit.add_argument("--csv")
    next_batch = campaign_sub.add_parser("next", help="Return the next safe missing batch from a rendered queue.")
    next_batch.add_argument("manifest")
    next_batch.add_argument("--queue", required=True, help="Rendered queue.json path.")
    next_batch.add_argument("--database")
    next_batch.add_argument("--limit", type=int, default=20)
    for command_name in ["reschedule-plan", "cancel-plan"]:
        change = campaign_sub.add_parser(command_name, help="Build a read-only visible-UI change plan.")
        change.add_argument("manifest")
        change.add_argument("--database")
    dashboard = campaign_sub.add_parser("dashboard", help="Combine campaign state, account volume, quota, and optional triage counts.")
    dashboard.add_argument("manifest")
    dashboard.add_argument("--database")
    dashboard.add_argument("--triage", help="Private triage JSON artifact created by mailctl triage.")
    rebalance = campaign_sub.add_parser("rebalance", help="Create a minimal-movement, quota-aware replacement schedule manifest.")
    rebalance.add_argument("manifest")
    rebalance.add_argument("--output", required=True)
    rebalance.add_argument("--start-at", required=True, help="ISO date/time or local YYYY-MM-DD.")
    rebalance.add_argument("--finish-by", required=True, help="ISO date/time or local YYYY-MM-DD.")
    rebalance.add_argument("--database")
    rebalance.add_argument("--daily-maximum", type=int)
    rebalance.add_argument("--minimum-gap-minutes", type=int)
    rebalance.add_argument("--offline", action="store_true", help="Ignore existing Sent and Send Later volume; use only for isolated planning.")
    rebalance.add_argument("--force", action="store_true", help="Replace an existing output manifest.")
    session_start = campaign_sub.add_parser("session-start", help="Create a private resumable visible-UI scheduling session.")
    session_start.add_argument("manifest")
    session_start.add_argument("--output", required=True)
    session_start.add_argument("--action", required=True, choices=["schedule", "reschedule", "cancel"])
    session_start.add_argument("--queue")
    session_start.add_argument("--database")
    session_next = campaign_sub.add_parser("session-next", help="Reconcile and return the next exact visible-UI operations.")
    session_next.add_argument("session")
    session_next.add_argument("--database")
    session_next.add_argument("--limit", type=int, default=20)
    session_record = campaign_sub.add_parser("session-record", help="Record one visible-UI attempt without claiming verification.")
    session_record.add_argument("session")
    session_record.add_argument("--key", required=True)
    session_record.add_argument("--result", required=True, choices=["applied", "failed", "skipped"])
    session_record.add_argument("--note")
    session_status = campaign_sub.add_parser("session-status", help="Summarize a resumable scheduling session.")
    session_status.add_argument("session")
    session_reconcile = campaign_sub.add_parser("session-reconcile", help="Verify session outcomes against Sent and Send Later.")
    session_reconcile.add_argument("session")
    session_reconcile.add_argument("--database")

    ubd = sub.add_parser("ubd", help="Create redacted UBD OS events or use its proposal-only Agent API.")
    ubd_sub = ubd.add_subparsers(dest="ubd_command", required=True)

    ubd_capabilities = ubd_sub.add_parser("capabilities", help="Read and verify UBD Agent API capabilities.")
    add_ubd_connection(ubd_capabilities)

    ubd_work_list = ubd_sub.add_parser("work-list", help="Read bounded UBD OS work through Agent API.")
    add_ubd_connection(ubd_work_list)
    ubd_work_list.add_argument("--business-unit-id", required=True)
    ubd_work_list.add_argument(
        "--status",
        choices=["open", "in_progress", "waiting", "completed", "cancelled"],
    )
    ubd_work_list.add_argument("--updated-after", help="ISO timestamp with UTC offset.")
    ubd_work_list.add_argument("--limit", type=int, default=50)

    ubd_work_get = ubd_sub.add_parser("work-get", help="Read one exact UBD OS work item through Agent API.")
    add_ubd_connection(ubd_work_get)
    add_ubd_scope(ubd_work_get, work_required=True)

    ubd_campaign_event = ubd_sub.add_parser(
        "campaign-event",
        help="Build a redacted, read-only UBD event from a local campaign audit.",
    )
    ubd_campaign_event.add_argument("manifest")
    ubd_campaign_event.add_argument("--database")
    add_ubd_scope(ubd_campaign_event)
    ubd_campaign_event.add_argument("--output", help="Optional new 0600 JSON artifact path; existing files are refused.")

    ubd_dashboard_event = ubd_sub.add_parser(
        "dashboard-event",
        help="Build a redacted campaign dashboard event with quota and triage aggregates.",
    )
    ubd_dashboard_event.add_argument("manifest")
    ubd_dashboard_event.add_argument("--database")
    ubd_dashboard_event.add_argument("--triage")
    add_ubd_scope(ubd_dashboard_event)
    ubd_dashboard_event.add_argument("--output")

    ubd_classify_event = ubd_sub.add_parser(
        "classify-event",
        help="Classify one message locally and emit only a redacted UBD event.",
    )
    ubd_classify_event.add_argument("message_id", type=int)
    add_location(ubd_classify_event)
    ubd_classify_event.add_argument("--max-chars", type=int, default=30000)
    add_ubd_scope(ubd_classify_event)
    ubd_classify_event.add_argument("--output", help="Optional new 0600 JSON artifact path; existing files are refused.")

    ubd_proposal = ubd_sub.add_parser(
        "propose-campaign-audit",
        help="Attach a redacted campaign audit to one UBD work item as a human-review proposal.",
    )
    ubd_proposal.add_argument("manifest")
    ubd_proposal.add_argument("--database")
    add_ubd_connection(ubd_proposal)
    add_ubd_scope(ubd_proposal, work_required=True)
    ubd_proposal.add_argument("--expected-revision", type=int, required=True)
    ubd_proposal.add_argument("--idempotency-key", required=True, help="UUID reused only for the exact same proposal.")
    ubd_proposal.add_argument("--confirm-proposal", action="store_true", required=True)

    ubd_classification_proposal = ubd_sub.add_parser(
        "propose-classification",
        help="Attach one redacted message classification to a UBD work item as a human-review proposal.",
    )
    ubd_classification_proposal.add_argument("message_id", type=int)
    add_location(ubd_classification_proposal)
    ubd_classification_proposal.add_argument("--max-chars", type=int, default=30000)
    add_ubd_connection(ubd_classification_proposal)
    add_ubd_scope(ubd_classification_proposal, work_required=True)
    ubd_classification_proposal.add_argument("--expected-revision", type=int, required=True)
    ubd_classification_proposal.add_argument(
        "--idempotency-key", required=True, help="UUID reused only for the exact same proposal."
    )
    ubd_classification_proposal.add_argument("--confirm-proposal", action="store_true", required=True)

    ubd_dashboard_proposal = ubd_sub.add_parser(
        "propose-dashboard",
        help="Attach a redacted campaign dashboard to one UBD work item as a human-review proposal.",
    )
    ubd_dashboard_proposal.add_argument("manifest")
    ubd_dashboard_proposal.add_argument("--database")
    ubd_dashboard_proposal.add_argument("--triage")
    add_ubd_connection(ubd_dashboard_proposal)
    add_ubd_scope(ubd_dashboard_proposal, work_required=True)
    ubd_dashboard_proposal.add_argument("--expected-revision", type=int, required=True)
    ubd_dashboard_proposal.add_argument("--idempotency-key", required=True)
    ubd_dashboard_proposal.add_argument("--confirm-proposal", action="store_true", required=True)
    return parser


def capabilities() -> dict[str, Any]:
    return {
        "ok": True,
        "command": "capabilities",
        "schema_version": CLI_SCHEMA_VERSION,
        "version": VERSION,
        "data": {
            "platform": "macos",
            "output": "json_stdout",
            "exit_codes": {"success": 0, "error_or_incomplete": 1, "usage": 2},
            "command_classes": {
                "offline_read_only": [
                    "capabilities",
                    "campaign validate",
                    "campaign plan",
                ],
                "local_artifact_write": [
                    "campaign render",
                    "campaign audit --csv",
                    "campaign audit evidence cache",
                    "campaign rebalance",
                    "campaign session-*",
                    "triage --output",
                    "ubd *-event --output",
                ],
                "mail_read_only": [
                    "doctor",
                    "accounts",
                    "selection",
                    "mailboxes",
                    "list",
                    "search",
                    "thread",
                    "read",
                    "classify",
                    "triage",
                    "index list",
                    "index counts",
                    "campaign audit",
                    "campaign dashboard",
                    "campaign next",
                    "campaign reschedule-plan",
                    "campaign cancel-plan",
                    "ubd campaign-event",
                    "ubd dashboard-event",
                    "ubd classify-event",
                ],
                "mail_draft_mutation": ["compose", "reply"],
                "mail_confirmed_mutation": ["mark", "flag", "move", "trash", "send"],
                "ubd_read_only": ["ubd capabilities", "ubd work-list", "ubd work-get"],
                "ubd_proposal_only": ["ubd propose-campaign-audit", "ubd propose-classification", "ubd propose-dashboard"],
            },
            "hard_boundaries": {
                "mail_index_write": False,
                "ubd_direct_database_access": False,
                "ubd_formal_state_change": False,
                "ubd_send_publish_payment_delete": False,
                "direct_send_requires_confirm_send": True,
                "direct_send_requires_explicit_sender": True,
                "direct_send_defaults_to_offline_preview": True,
                "direct_send_requires_intent_digest": True,
                "direct_send_has_durable_local_attempt_guard": True,
                "direct_send_error_is_retryable": False,
                "direct_send_intent_schema": send_guard.INTENT_SCHEMA,
                "supplied_sender_requires_unique_enabled_account": True,
                "scheduled_attachment_match_requires_emlx_byte_evidence": True,
                "unknown_campaign_mailbox_blocks_continuation": True,
                "required_body_access_errors_propagate": True,
                "mailbox_changes_require_confirmation_flag": True,
                "ubd_proposals_require_confirmation_and_human_review": True,
                "send_later_uses_visible_ui": True,
                "schedule_sessions_never_claim_ui_success_without_index_verification": True,
                "send_later_emlx_fallback_read_only": True,
                "send_later_evidence_cache_private_local_only": True,
                "send_later_evidence_cache_contains_message_bodies": False,
            },
        },
    }


def doctor(timeout: int) -> dict[str, Any]:
    plugin_root = SCRIPT_DIR.parents[2]
    plugin_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    checks: dict[str, Any] = {
        "platform": platform.system(),
        "macos": platform.system() == "Darwin",
        "osascript": shutil.which("osascript"),
        "mail_app": str(MAIL_APP),
        "mail_app_exists": MAIL_APP.exists(),
        "backend": str(BACKEND),
        "backend_exists": BACKEND.is_file(),
        "cli_version": VERSION,
        "plugin_root": str(plugin_root),
    }
    ready = bool(checks["macos"] and checks["osascript"] and checks["mail_app_exists"] and checks["backend_exists"])
    if ready:
        try:
            checks["backend_ping"] = run_backend({"command": "ping"}, timeout)["data"]
            if checks["backend_ping"].get("version") != VERSION:
                checks["backend_version_mismatch"] = True
                ready = False
        except MailCtlError as error:
            checks["backend_error"] = str(error)
            ready = False
    try:
        index_path = mail_index.find_index()
        with mail_index.connect_read_only(index_path) as connection:
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        checks["mail_index"] = str(index_path)
        checks["mail_index_read_only"] = True
        checks["mail_index_schema"] = mail_index.schema_report(str(index_path))
        version_root = index_path.parent.parent
        evidence_cache = mail_store.default_cache_path(version_root)
        try:
            store_files = mail_store.send_later_files(version_root)
            checks["send_later_evidence"] = {
                "emlx_files": len(store_files),
                "cache_enabled": evidence_cache is not None,
                "cache_exists": bool(evidence_cache and evidence_cache.is_file()),
            }
        except RuntimeError as error:
            checks["send_later_evidence"] = {
                "emlx_files": None,
                "cache_enabled": evidence_cache is not None,
                "cache_exists": bool(evidence_cache and evidence_cache.is_file()),
                "error": str(error),
            }
            ready = False
        if not checks["mail_index_schema"]["compatible"]:
            ready = False
    except mail_index.MailIndexError as error:
        checks["mail_index_read_only"] = False
        checks["mail_index_error"] = str(error)
        ready = False
    if plugin_manifest.is_file():
        try:
            manifest_version = json.loads(plugin_manifest.read_text(encoding="utf-8")).get("version")
            checks["plugin_version"] = manifest_version
            checks["plugin_base_version_matches"] = str(manifest_version or "").split("+", 1)[0] == VERSION
            if not checks["plugin_base_version_matches"]:
                ready = False
        except (OSError, json.JSONDecodeError) as error:
            checks["plugin_manifest_error"] = str(error)
            ready = False
    cache_root = Path.home() / ".codex" / "plugins" / "cache" / "personal" / "apple-mail-cli"
    cache_copies = []
    if cache_root.is_dir():
        source_digest = file_digest(Path(__file__).resolve())
        for candidate in sorted(cache_root.glob("*/skills/apple-mail-cli/scripts/mailctl.py")):
            cache_copies.append(
                {
                    "version": candidate.parents[3].name,
                    "path": str(candidate),
                    "matches_current": file_digest(candidate) == source_digest,
                }
            )
    checks["installed_cache_copies"] = cache_copies
    checks["installed_cache_matches_current"] = any(item["matches_current"] for item in cache_copies)
    if cache_copies and not checks["installed_cache_matches_current"]:
        checks["installed_cache_version_mismatch"] = True
        ready = False
    return {
        "ok": ready,
        "command": "doctor",
        "data": checks,
        "note": "The first Mail command may request Automation permission; visible scheduling also requires Accessibility permission.",
    }


def classify_message(message: dict[str, Any]) -> dict[str, Any]:
    subject = str(message.get("subject") or "")
    sender = str(message.get("sender") or "")
    content = str(message.get("content") or "")
    combined = f"{subject}\n{sender}\n{content}"
    lowered = combined.lower()
    evidence: list[str] = []
    failed_recipients: list[str] = []
    smtp_codes = sorted(set(re.findall(r"\b[245](?:\d{2}|\.\d\.\d)\b", combined)))
    for pattern in [r"Original-Recipient:\s*rfc822;\s*([^\s]+)", r"Final-Recipient:\s*rfc822;\s*([^\s]+)"]:
        failed_recipients.extend(re.findall(pattern, combined, flags=re.IGNORECASE))
    failed_recipients.extend(re.findall(r"<([^<>\s]+@[^<>\s]+)>:\s*(?:host|unable|mailbox)", combined, flags=re.IGNORECASE))
    failed_recipients = sorted({value.rstrip(".;,").lower() for value in failed_recipients})

    bounce_terms = [
        "undelivered mail returned to sender",
        "delivery status notification (failure)",
        "mail delivery failed",
        "delivery failure",
        "could not be delivered",
        "action: failed",
        "郵件傳送失敗",
        "邮件传送失败",
        "無法投遞",
        "无法投递",
    ]
    dmarc = "dmarc" in lowered and ("aggregate report" in lowered or any(item["name"].lower().endswith((".xml", ".zip", ".gz")) for item in message.get("attachments", [])))
    if any(term in lowered for term in bounce_terms):
        if "unable to look up host" in lowered or "name or service not known" in lowered or "5.4.4" in lowered:
            kind, confidence, action = "bounce_transient_dns", 0.98, "Record the temporary DNS failure and do not retry automatically."
            evidence.append("DNS lookup or 5.4.4 delivery failure")
        elif any(code.startswith("5") for code in smtp_codes) or "invalid recipient" in lowered or "recipient address rejected" in lowered:
            kind, confidence, action = "bounce_permanent", 0.99, "Mark the address do-not-retry and find a better contact."
            evidence.append("Permanent 5xx or invalid-recipient signal")
        else:
            kind, confidence, action = "bounce_unknown", 0.9, "Record the failure and review the diagnostic before retrying."
            evidence.append("Delivery failure notification")
    elif dmarc:
        kind, confidence, action = "dmarc_report", 0.99, "Inspect authentication results if requested; no reply is normally needed."
        evidence.append("DMARC aggregate-report signal")
    elif any(
        term in lowered
        for term in [
            "unsubscribe",
            "remove me from",
            "stop emailing",
            "do not email",
            "退訂",
            "退订",
            "停止寄送",
            "不要再寄",
        ]
    ):
        kind, confidence, action = "unsubscribe_request", 0.99, "Suppress the address and record the opt-out before any future campaign."
        evidence.append("Explicit unsubscribe or stop-contact wording")
    elif any(
        term in lowered
        for term in [
            "report spam",
            "this is spam",
            "harassment",
            "投訴",
            "投诉",
            "垃圾郵件",
            "垃圾邮件",
        ]
    ):
        kind, confidence, action = "complaint", 0.97, "Stop outreach, preserve evidence, and review the complaint."
        evidence.append("Complaint or spam-report wording")
    elif any(
        term in lowered
        for term in [
            "out of office",
            "out of the office",
            "on leave",
            "away from the office",
            "不在辦公室",
            "不在办公室",
            "休假中",
            "外出中",
        ]
    ):
        kind, confidence, action = "out_of_office", 0.94, "Record the return date or alternate contact and wait before following up."
        evidence.append("Out-of-office wording")
    elif any(term in lowered for term in ["automatic reply", "auto-reply", "automated response", "this is an automated", "thank you for contacting", "自動回覆", "自动回复", "自動回信", "自动回信"]):
        discovered = sorted(set(address.lower() for address in EMAIL_PATTERN.findall(content)))
        if discovered:
            kind, confidence, action = "auto_reply_with_contacts", 0.9, "Review any suggested partnership or PR contact and update tracking before resending."
            evidence.append("Automated-response wording plus email address")
        else:
            kind, confidence, action = "auto_reply", 0.85, "Record the acknowledgement and wait unless it requests another action."
            evidence.append("Automated-response wording")
    elif subject.lower().startswith(("re:", "fw:", "fwd:", "回覆:", "回复:")):
        positive_terms = ["interested", "sounds good", "let's discuss", "lets discuss", "有興趣", "有兴趣", "可以傾", "可以聊"]
        negative_terms = ["not interested", "no thanks", "not a fit", "沒有興趣", "没有兴趣", "暫不考慮", "暂不考虑"]
        question_terms = ["?", "？", "could you", "can you", "請問", "请问", "想了解"]
        if any(term in lowered for term in negative_terms):
            kind, confidence, action = "human_negative", 0.9, "Record the decline and stop or adjust follow-up."
            evidence.append("Negative human-reply wording")
        elif any(term in lowered for term in positive_terms):
            kind, confidence, action = "human_positive", 0.9, "Prioritize a contextual reply and create the next sales action."
            evidence.append("Positive human-reply wording")
        elif any(term in lowered for term in question_terms):
            kind, confidence, action = "human_question", 0.84, "Read the thread and prepare an answer or requested material."
            evidence.append("Question in a likely human reply")
        else:
            kind, confidence, action = "likely_human_reply", 0.75, "Read in context and decide whether a reply or tracking update is needed."
            evidence.append("Reply or forward subject without automated signals")
    else:
        kind, confidence, action = "unclassified", 0.4, "Review manually."
    discovered_contacts = sorted(set(address.lower() for address in EMAIL_PATTERN.findall(content)))
    return {
        "kind": kind,
        "confidence": confidence,
        "evidence": evidence,
        "failed_recipients": failed_recipients,
        "smtp_codes": smtp_codes,
        "discovered_contacts": discovered_contacts[:20],
        "suggested_action": action,
    }


def triage_mailbox(args: argparse.Namespace) -> dict[str, Any]:
    listing = run_backend(
        {
            "command": "list",
            "mailbox": args.mailbox,
            "account": args.account,
            "limit": max(1, min(args.limit, 100)),
            "scan_limit": max(1, min(args.scan_limit, 10000)),
            "unread_only": args.unread,
            "since": args.since,
        },
        args.timeout,
    )["data"]
    items = []
    counts: dict[str, int] = {}
    for summary in listing.get("messages", []):
        message = run_backend(
            {
                "command": "read",
                "message_id": summary["id"],
                "mailbox": args.mailbox,
                "account": args.account,
                "max_chars": args.max_chars,
            },
            args.timeout,
        )["data"]
        classification = classify_message(message)
        kind = classification["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        _, sender_address = parseaddr(str(message.get("sender") or ""))
        items.append(
            {
                "id": message.get("id"),
                "message_id": message.get("message_id"),
                "sender": message.get("sender"),
                "sender_address": sender_address.strip().lower(),
                "subject": message.get("subject"),
                "date_received": message.get("date_received"),
                "account": message.get("account"),
                "mailbox": message.get("mailbox"),
                "classification": classification,
            }
        )
    result = {
        "ok": True,
        "command": "triage",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "mailbox": listing.get("mailbox"),
        "account_filter": listing.get("account_filter"),
        "scanned": listing.get("scanned"),
        "returned": len(items),
        "truncated": listing.get("truncated"),
        "classification_counts": dict(sorted(counts.items())),
        "items": items,
    }
    if args.output:
        result["output"] = write_private_json(args.output, result)
    return result


def public_campaign_report(report: dict[str, Any]) -> dict[str, Any]:
    output = {key: value for key, value in report.items() if key not in {"active_messages", "skipped_messages"}}
    for source_key, target_key in [("active_messages", "active_messages"), ("skipped_messages", "skipped_messages")]:
        output[target_key] = []
        for item in report[source_key]:
            visible = {key: value for key, value in item.items() if key != "body"}
            visible["body_chars"] = len(item.get("body", ""))
            output[target_key].append(visible)
    return output


def outgoing_payload(args: argparse.Namespace, command: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return outgoing_preflight(args, command)


def guarded_send(args: argparse.Namespace) -> dict[str, Any]:
    payload, evidence = outgoing_preflight(args, "send")
    intent = send_guard.canonical_intent(payload, evidence)
    digest = send_guard.intent_digest(intent)
    confirmation = getattr(args, "confirm_send", None)
    if confirmation is None:
        return {"ok": True, "command": "send", "data": {
            "preview": True, "intent": intent, "intent_digest": digest,
            "submission_accepted": False, "delivery_confirmed": False,
            "requires_human_confirmation": True,
        }}
    if type(confirmation) is not str or confirmation != digest:
        raise MailCtlError("Confirmation does not match the current outbound intent. Review a fresh preview before confirming.", code="send_intent_mismatch")
    attempt = send_guard.reserve_attempt(digest, evidence)
    payload["confirm_send"] = digest
    payload["attachment_evidence"] = intent["attachments"]
    payload["attachments"] = attempt.snapshots
    try:
        result = run_backend(payload, args.timeout)
        data = result.get("data")
        if not isinstance(data, dict) or data.get("submission_accepted") is not True or data.get("delivery_confirmed") is not False or data.get("intent_digest") != digest:
            raise MailCtlError("Send backend did not confirm the current guarded submission contract.", code="backend_contract_error")
        attempt.finish("accepted_by_mail")
    except Exception as error:
        try:
            attempt.finish("outcome_unknown")
        except Exception:
            pass
        raise MailCtlError("Send outcome is unknown and this intent remains blocked. Inspect Mail and the private attempt record; do not retry automatically.", code="send_outcome_unknown", hint="A timeout, backend error, or missing durable final record cannot establish whether Mail accepted the message.", retryable=False) from error
    result["attempt"] = {"intent_digest": digest, "state": "accepted_by_mail", "retryable": False}
    return result


def write_private_json(path_value: str | None, value: dict[str, Any]) -> str | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise MailCtlError(f"Refusing to overwrite existing UBD event artifact: {path}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return str(path)


def ubd_api_client(args: argparse.Namespace) -> ubd_connector.AgentApiClient:
    origin = args.origin or os.environ.get("UBD_AGENT_API_ORIGIN", "")
    if not origin:
        raise MailCtlError("Set UBD_AGENT_API_ORIGIN or pass --origin; no production origin is assumed.")
    if not ENV_NAME_PATTERN.fullmatch(args.token_env):
        raise MailCtlError("--token-env must be a valid environment-variable name.")
    token = os.environ.get(args.token_env, "")
    if not token:
        raise MailCtlError(f"UBD Agent API token is unavailable in environment variable {args.token_env}.")
    return ubd_connector.AgentApiClient(origin, token, timeout=args.api_timeout)


def checked_read_response(response: dict[str, Any], tool: str) -> dict[str, Any]:
    checked = ubd_connector.verify_tool_response(response, tool)
    safety = checked["safety"]
    if safety.get("readOnly") is not True or safety.get("applied") is not False:
        raise MailCtlError(f"UBD Agent API did not preserve the read-only boundary for {tool}.")
    return checked


def work_for_proposal(
    client: ubd_connector.AgentApiClient,
    capabilities_response: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    if not args.confirm_proposal:
        raise MailCtlError("A UBD proposal requires --confirm-proposal.")
    if args.expected_revision < 1:
        raise MailCtlError("--expected-revision must be positive.")
    ubd_connector.checked_uuid(args.idempotency_key, "idempotency_key")
    ubd_connector.verify_capabilities(
        capabilities_response,
        required_tools=ubd_connector.PROPOSAL_REQUIRED_TOOLS,
        business_unit_id=args.business_unit_id,
    )
    work_response = checked_read_response(
        client.tool(
            "work.get",
            {"businessUnitId": args.business_unit_id, "workItemId": args.work_item_id},
        ),
        "work.get",
    )
    work = work_response["data"].get("item")
    if not isinstance(work, dict):
        raise MailCtlError("UBD OS did not return the requested work item.")
    return work


def store_event_proposal(
    client: ubd_connector.AgentApiClient,
    event: dict[str, Any],
    work: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    proposal = ubd_connector.proposal_payload(event, work, expected_revision=args.expected_revision)
    response = client.tool(
        "work.propose_update",
        proposal,
        idempotency_key=args.idempotency_key,
    )
    return ubd_connector.verify_proposal_response(
        response,
        expected_work_item_id=args.work_item_id,
        expected_revision=args.expected_revision,
        expected_request_id=args.idempotency_key,
    )


def execute_ubd(args: argparse.Namespace) -> dict[str, Any]:
    if args.ubd_command == "campaign-event":
        audit = campaign.audit_campaign(args.manifest, database=args.database)
        event = ubd_connector.campaign_audit_event(
            args.manifest,
            audit,
            business_unit_id=args.business_unit_id,
            work_item_id=args.work_item_id,
        )
        artifact = write_private_json(args.output, event)
        return {
            "ok": True,
            "command": "ubd campaign-event",
            "data": {"event": event, "artifact": artifact},
            "safety": {"mail_mutated": False, "ubd_formal_state_changed": False},
        }

    if args.ubd_command == "dashboard-event":
        dashboard = campaign.campaign_dashboard(args.manifest, database=args.database, triage_path=args.triage)
        event = ubd_connector.campaign_dashboard_event(
            args.manifest,
            dashboard,
            business_unit_id=args.business_unit_id,
            work_item_id=args.work_item_id,
        )
        artifact = write_private_json(args.output, event)
        return {
            "ok": True,
            "command": "ubd dashboard-event",
            "data": {"event": event, "artifact": artifact},
            "safety": {"mail_mutated": False, "ubd_formal_state_changed": False},
        }

    if args.ubd_command == "classify-event":
        result = run_backend(
            {
                "command": "read",
                "message_id": args.message_id,
                "mailbox": args.mailbox,
                "account": args.account,
                "max_chars": args.max_chars,
            },
            args.timeout,
        )
        message = result["data"]
        event = ubd_connector.classification_event(
            message,
            classify_message(message),
            business_unit_id=args.business_unit_id,
            work_item_id=args.work_item_id,
        )
        artifact = write_private_json(args.output, event)
        return {
            "ok": True,
            "command": "ubd classify-event",
            "data": {"event": event, "artifact": artifact},
            "safety": {"mail_mutated": False, "ubd_formal_state_changed": False},
        }

    client = ubd_api_client(args)
    capabilities = client.capabilities()
    if args.ubd_command == "capabilities":
        ubd_connector.verify_capabilities(capabilities, required_tools=["system.capabilities"])
        return {
            "ok": True,
            "command": "ubd capabilities",
            "data": capabilities,
            "safety": {"read_only": True, "formal_state_changed": False},
        }

    if args.ubd_command == "work-list":
        if args.limit < 1 or args.limit > 100:
            raise MailCtlError("--limit must be between 1 and 100 for UBD work-list.")
        ubd_connector.verify_capabilities(
            capabilities,
            required_tools=["system.capabilities", "work.list"],
            business_unit_id=args.business_unit_id,
        )
        payload: dict[str, Any] = {"businessUnitId": args.business_unit_id, "limit": args.limit}
        if args.status:
            payload["status"] = args.status
        if args.updated_after:
            payload["updatedAfter"] = args.updated_after
        response = checked_read_response(client.tool("work.list", payload), "work.list")
        return {
            "ok": True,
            "command": "ubd work-list",
            "data": response,
            "safety": {"read_only": True, "formal_state_changed": False},
        }

    if args.ubd_command == "work-get":
        ubd_connector.verify_capabilities(
            capabilities,
            required_tools=["system.capabilities", "work.get"],
            business_unit_id=args.business_unit_id,
        )
        response = checked_read_response(
            client.tool(
                "work.get",
                {"businessUnitId": args.business_unit_id, "workItemId": args.work_item_id},
            ),
            "work.get",
        )
        return {
            "ok": True,
            "command": "ubd work-get",
            "data": response,
            "safety": {"read_only": True, "formal_state_changed": False},
        }

    if args.ubd_command == "propose-campaign-audit":
        work = work_for_proposal(client, capabilities, args)
        audit = campaign.audit_campaign(args.manifest, database=args.database)
        event = ubd_connector.campaign_audit_event(
            args.manifest,
            audit,
            business_unit_id=args.business_unit_id,
            work_item_id=args.work_item_id,
        )
        response = store_event_proposal(client, event, work, args)
        return {
            "ok": True,
            "command": "ubd propose-campaign-audit",
            "data": {"event": event, "proposal_receipt": response},
            "safety": {
                "mail_mutated": False,
                "ubd_formal_state_changed": False,
                "proposal_stored": True,
                "human_review_required": True,
            },
        }

    if args.ubd_command == "propose-classification":
        work = work_for_proposal(client, capabilities, args)
        result = run_backend(
            {
                "command": "read",
                "message_id": args.message_id,
                "mailbox": args.mailbox,
                "account": args.account,
                "max_chars": args.max_chars,
            },
            args.timeout,
        )
        message = result["data"]
        event = ubd_connector.classification_event(
            message,
            classify_message(message),
            business_unit_id=args.business_unit_id,
            work_item_id=args.work_item_id,
        )
        response = store_event_proposal(client, event, work, args)
        return {
            "ok": True,
            "command": "ubd propose-classification",
            "data": {"event": event, "proposal_receipt": response},
            "safety": {
                "mail_mutated": False,
                "ubd_formal_state_changed": False,
                "proposal_stored": True,
                "human_review_required": True,
            },
        }

    if args.ubd_command == "propose-dashboard":
        work = work_for_proposal(client, capabilities, args)
        dashboard = campaign.campaign_dashboard(args.manifest, database=args.database, triage_path=args.triage)
        event = ubd_connector.campaign_dashboard_event(
            args.manifest,
            dashboard,
            business_unit_id=args.business_unit_id,
            work_item_id=args.work_item_id,
        )
        response = store_event_proposal(client, event, work, args)
        return {
            "ok": True,
            "command": "ubd propose-dashboard",
            "data": {"event": event, "proposal_receipt": response},
            "safety": {
                "mail_mutated": False,
                "ubd_formal_state_changed": False,
                "proposal_stored": True,
                "human_review_required": True,
            },
        }

    raise MailCtlError(f"Unsupported UBD command: {args.ubd_command}")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "capabilities":
        return capabilities()
    if args.command == "doctor":
        return doctor(args.timeout)
    if args.command == "ubd":
        return execute_ubd(args)
    if args.command == "search":
        has_attachment = True if args.has_attachment else (False if args.no_attachment else None)
        unread = True if args.unread else (False if args.read else None)
        flagged = True if args.flagged else (False if args.unflagged else None)
        return {
            "ok": True,
            "command": "search",
            "data": mail_index.search_messages(
                database=args.database,
                states=args.state,
                query=args.query,
                sender=args.sender,
                recipient=args.recipient,
                subject=args.subject,
                attachment=args.attachment,
                has_attachment=has_attachment,
                unread=unread,
                flagged=flagged,
                since=args.since,
                before=args.before,
                limit=args.limit,
                cursor=args.cursor,
            ),
        }
    if args.command == "thread":
        return {
            "ok": True,
            "command": "thread",
            "data": mail_index.thread_messages(
                args.conversation_id,
                database=args.database,
                limit=args.limit,
                cursor=args.cursor,
            ),
        }
    if args.command == "triage":
        return triage_mailbox(args)
    if args.command == "index":
        path, rows = mail_index.list_messages(
            database=args.database,
            states=args.state,
            recipient=args.recipient,
            subject=args.subject,
            limit=args.limit,
        )
        data = mail_index.counts(rows) if args.index_command == "counts" else rows
        return {"ok": True, "command": f"index {args.index_command}", "mail_index": str(path), "data": data}
    if args.command == "campaign":
        if args.campaign_command == "validate":
            return {"command": "campaign validate", **public_campaign_report(campaign.validate_manifest(args.manifest))}
        if args.campaign_command == "plan":
            report = campaign.validate_manifest(args.manifest, require_schedule=True)
            return {"command": "campaign plan", **public_campaign_report(report)}
        if args.campaign_command == "render":
            return {"command": "campaign render", **campaign.render_campaign(args.manifest, args.output, force=args.force)}
        if args.campaign_command == "audit":
            return {"command": "campaign audit", **campaign.audit_campaign(args.manifest, database=args.database, csv_path=args.csv)}
        if args.campaign_command == "dashboard":
            return {
                "command": "campaign dashboard",
                **campaign.campaign_dashboard(args.manifest, database=args.database, triage_path=args.triage),
            }
        if args.campaign_command == "rebalance":
            return {
                "command": "campaign rebalance",
                **campaign.rebalance_campaign(
                    args.manifest,
                    args.output,
                    start_at=args.start_at,
                    finish_by=args.finish_by,
                    database=args.database,
                    daily_maximum=args.daily_maximum,
                    minimum_gap_minutes=args.minimum_gap_minutes,
                    include_account_volume=not args.offline,
                    force=args.force,
                ),
            }
        if args.campaign_command == "next":
            return {
                "command": "campaign next",
                **campaign.next_batch(args.manifest, args.queue, database=args.database, limit=args.limit),
            }
        if args.campaign_command in {"reschedule-plan", "cancel-plan"}:
            action = "reschedule" if args.campaign_command == "reschedule-plan" else "cancel"
            return {
                "command": f"campaign {args.campaign_command}",
                **campaign.schedule_change_plan(args.manifest, database=args.database, action=action),
            }
        if args.campaign_command == "session-start":
            return {
                "command": "campaign session-start",
                **campaign.start_schedule_session(
                    args.manifest,
                    args.output,
                    action=args.action,
                    queue_path=args.queue,
                    database=args.database,
                ),
            }
        if args.campaign_command == "session-next":
            return {
                "command": "campaign session-next",
                **campaign.next_session_operations(args.session, database=args.database, limit=args.limit),
            }
        if args.campaign_command == "session-record":
            return {
                "command": "campaign session-record",
                **campaign.record_schedule_session(args.session, key=args.key, result=args.result, note=args.note),
            }
        if args.campaign_command == "session-status":
            return {"command": "campaign session-status", **campaign.session_status(args.session)}
        if args.campaign_command == "session-reconcile":
            reconciled = campaign.reconcile_schedule_session(args.session, database=args.database)
            return {
                "ok": True,
                "command": "campaign session-reconcile",
                "changed": reconciled["changed"],
                "audit_summary": reconciled["audit"]["summary"],
                "session": campaign.session_status(args.session),
            }

    if args.command == "send":
        return guarded_send(args)
    if args.command == "compose":
        payload, evidence = outgoing_payload(args, args.command)
        result = run_backend(payload, args.timeout)
        result["preflight"] = {"to": args.to, "cc": args.cc, "bcc": args.bcc, "sender": args.sender, "subject": args.subject, "attachments": evidence}
        return result
    if args.command == "reply":
        return run_backend(
            {
                "command": "reply",
                "message_id": args.message_id,
                "mailbox": args.mailbox,
                "account": args.account,
                "body": validated_body(args, "Reply body"),
                "reply_all": args.reply_all,
            },
            args.timeout,
        )
    if args.command in {"read", "classify"}:
        result = run_backend(
            {
                "command": "read",
                "message_id": args.message_id,
                "mailbox": args.mailbox,
                "account": args.account,
                "max_chars": args.max_chars,
            },
            args.timeout,
        )
        if args.command == "classify":
            message = result["data"]
            result = {
                "ok": True,
                "command": "classify",
                "data": {
                    "message": {key: message.get(key) for key in ["id", "message_id", "subject", "sender", "date_received", "account", "mailbox"]},
                    "classification": classify_message(message),
                },
            }
        return result
    if args.command == "accounts" or args.command == "selection":
        return run_backend({"command": args.command}, args.timeout)
    if args.command == "mailboxes":
        return run_backend({"command": "mailboxes", "account": args.account, "depth": args.depth}, args.timeout)
    if args.command == "list":
        return run_backend(
            {
                "command": "list",
                "mailbox": args.mailbox,
                "account": args.account,
                "limit": args.limit,
                "scan_limit": args.scan_limit,
                "query": args.query,
                "search_body": args.search_body,
                "unread_only": args.unread,
                "flagged_only": args.flagged,
                "since": args.since,
                "before": args.before,
            },
            args.timeout,
        )
    if args.command == "mark":
        payload = {
            "command": "mark",
            "message_id": args.message_id,
            "mailbox": args.mailbox,
            "account": args.account,
            "read": args.read,
            "confirm_change": args.confirm_change,
        }
    elif args.command == "flag":
        payload = {
            "command": "flag",
            "message_id": args.message_id,
            "mailbox": args.mailbox,
            "account": args.account,
            "flagged": args.on,
            "confirm_change": args.confirm_change,
        }
    elif args.command == "move":
        payload = {
            "command": "move",
            "message_id": args.message_id,
            "mailbox": args.mailbox,
            "account": args.account,
            "to_mailbox": args.to_mailbox,
            "to_account": args.to_account,
            "confirm_change": args.confirm_change,
        }
    elif args.command == "trash":
        payload = {
            "command": "trash",
            "message_id": args.message_id,
            "mailbox": args.mailbox,
            "account": args.account,
            "confirm_trash": args.confirm_trash,
        }
    else:
        raise MailCtlError(f"Unsupported command: {args.command}")
    return run_backend(payload, args.timeout)


def structured_error(error: Exception, command: str | None) -> dict[str, Any]:
    message = str(error)
    if isinstance(error, MailCtlError):
        code = error.code
        hint = error.hint
        retryable = error.retryable
    elif isinstance(error, send_guard.SendGuardError):
        code, hint, retryable = error.code, "Inspect the private local send-attempt state and Mail before any further send decision.", False
    elif isinstance(error, campaign.CampaignError):
        code, hint, retryable = "campaign_error", "Review the campaign plan or audit details before retrying.", False
    elif isinstance(error, mail_index.MailIndexError):
        code, hint, retryable = "mail_index_error", "Run mailctl doctor and confirm Full Disk Access before retrying.", False
    elif isinstance(error, ubd_connector.UbdConnectorError):
        code, hint, retryable = "ubd_connector_error", "Verify UBD OS scope, revision, capabilities, and the pending proposal state.", False
    elif isinstance(error, OSError):
        code, hint, retryable = "filesystem_error", "Check the path and permissions; no Mail mutation was assumed.", False
    else:
        code, hint, retryable = "unexpected_error", "Run mailctl doctor and retry only after reviewing actual Mail state.", False
        message = "Unexpected local failure; no successful result was confirmed."
    return {
        "ok": False,
        "command": command,
        "error": {
            "code": code,
            "type": error.__class__.__name__,
            "message": message,
            "hint": hint,
            "retryable": retryable,
        },
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    try:
        result = execute(args)
        emit(result, compact=args.compact)
        return 0 if result.get("ok") else 1
    except (MailCtlError, campaign.CampaignError, mail_index.MailIndexError, ubd_connector.UbdConnectorError, OSError) as error:
        emit(structured_error(error, args.command), compact=args.compact)
        return 1
    except Exception as error:
        emit(structured_error(error, args.command), compact=args.compact)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
