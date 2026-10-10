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
import uuid
import tempfile
from collections import Counter
from urllib.parse import unquote, urlparse
from email import policy as email_policy
from email.parser import Parser
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import campaign
import mail_index
import mail_store
import ubd_connector
import send_guard
import operation_state
import batch_read
import mail_workflow


VERSION = "0.6.0"
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
        if "-1712" in detail or "timed out" in detail.lower():
            raise MailCtlError(detail, code="mail_automation_timeout", hint="Inspect Mail before retrying.")
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
        if "-1712" in message or "timed out" in message.lower():
            raise MailCtlError(message, code="mail_automation_timeout", hint="Inspect Mail before retrying.")
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


DRAFT_FIELDS = ("draft_id", "sender", "subject", "content", "to", "cc", "bcc", "attachments", "visible")


def draft_snapshot(draft: dict) -> dict:
    return {key: draft.get(key) for key in DRAFT_FIELDS}


def draft_verification(draft: dict, expected: dict, attachments: list[dict]) -> dict:
    mismatches = []
    for field in ("to", "cc", "bcc"):
        try:
            if Counter(normalize_addresses(draft.get(field) or [], field)) != Counter(expected[field]):
                mismatches.append(field)
        except MailCtlError:
            mismatches.append(field)
    try:
        actual_sender = normalize_address(draft.get("sender"), "sender")
        if not expected.get("sender") or actual_sender != expected["sender"]:
            mismatches.append("sender")
    except MailCtlError:
        mismatches.append("sender")
    if draft.get("subject") != expected["subject"]:
        mismatches.append("subject")
    # Rich-text attachment markers are not silently stripped: uncertain body evidence blocks sending.
    if draft.get("content") != expected["body"]:
        mismatches.append("body")
    actual = draft.get("attachments")
    hashes_checked = False
    if not isinstance(actual, list):
        mismatches.append("attachments_unverified")
    else:
        try:
            paths = []
            for item in actual:
                value = str(item.get("path") or "")
                parsed = urlparse(value)
                if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
                    value = unquote(parsed.path)
                elif parsed.scheme or not Path(value).is_absolute():
                    raise ValueError("Unresolvable attachment reference")
                paths.append(value)
            evidence = attachment_preflight(paths)
            signature = lambda items: Counter((x["name"], x["size_bytes"], x["sha256"]) for x in items)
            hashes_checked = True
            if signature(evidence) != signature(attachments):
                mismatches.append("attachments")
        except (ValueError, OSError, MailCtlError):
            mismatches.append("attachments_unverified")
    return {"verified": not mismatches, "mismatch_fields": mismatches, "attachment_sha256_checked": hashes_checked,
            "evidence": "open_draft_readback_and_referenced_files", "delivery_verified": False}


def prepare_verified_draft(args: argparse.Namespace, *, update: bool = False, prepared=None) -> dict:
    if args.command == "send":
        raise MailCtlError("Direct send must use the digest-confirmed guarded workflow.")
    payload, evidence = prepared if prepared is not None else outgoing_preflight(args, "compose")
    if update:
        current = draft_snapshot(run_backend({"command": "draft_read", "draft_id": args.draft_id}, args.timeout)["data"])
        if operation_state.digest(current) != args.expected_revision:
            raise MailCtlError("Draft revision changed. Read the draft again before updating.")
        payload.update(command="draft_update", draft_id=args.draft_id, expected_snapshot=current, confirm_change=args.confirm_change)
    created = run_backend(payload, args.timeout)["data"]
    current = draft_snapshot(run_backend({"command": "draft_read", "draft_id": created["draft_id"]}, args.timeout)["data"])
    verification = draft_verification(current, payload, evidence)
    result = {"ok": True, "command": "draft update" if update else args.command,
              "data": {**current, "sent": False, "revision": operation_state.digest(current), "verification": verification},
              "preflight": {key: payload[key] for key in ("sender", "to", "cc", "bcc", "subject")}}
    return result


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
    search.add_argument("--account", help="Exact account_key from an index search result.")
    search.add_argument("--exact-address", action="store_true")
    search.add_argument("--message-id", help="Exact RFC Message-ID.")

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
    triage.add_argument("--batch-size", type=int, choices=range(1, 21), default=20)
    triage.add_argument("--output", help="Optional new private JSON artifact for dashboard reconciliation.")

    batch = sub.add_parser("read-batch", help="Read up to 100 exact Mail IDs in bounded groups.")
    batch.add_argument("--id", dest="message_ids", type=int, action="append", required=True)
    add_location(batch)
    batch.add_argument("--batch-size", type=int, choices=range(1, 21), default=20)
    batch.add_argument("--max-chars", type=int, default=30000)

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
    for mutation in (compose, send, reply, mark, flag, move, trash):
        mutation.add_argument("--operation-id", help="Stable UUID; repeated completed requests return their receipt.")
    receipt = sub.add_parser("receipt", help="Read one private operation receipt.")
    receipt.add_argument("operation_id")
    reconcile = sub.add_parser("receipt-reconcile", help="Annotate a send receipt with exact Sent metadata; never enables retries.")
    reconcile.add_argument("operation_id")
    reconcile.add_argument("--message-id", required=True, help="Exact RFC Message-ID including angle brackets.")
    reconcile.add_argument("--account-key", required=True, help="Exact index account_key, not the display name.")
    reconcile.add_argument("--database")

    resolve = sub.add_parser("resolve", help="Resolve an exact RFC Message-ID to a current Mail object in one mailbox.")
    resolve.add_argument("rfc_message_id")
    add_location(resolve)
    draft = sub.add_parser("draft", help="Inspect, verify, or revision-guard an open outgoing draft.")
    draft_sub = draft.add_subparsers(dest="draft_command", required=True)
    for name in ("show", "verify", "update"):
        item = draft_sub.add_parser(name)
        item.add_argument("draft_id", type=int)
        if name in {"verify", "update"}:
            add_outgoing(item)
        if name == "update":
            item.add_argument("--expected-revision", required=True)
            item.add_argument("--confirm-change", action="store_true", required=True)
            item.add_argument("--operation-id")
    attachments = sub.add_parser("attachments", help="List message attachment metadata or save one exact attachment locally.")
    attachments_sub = attachments.add_subparsers(dest="attachments_command", required=True)
    for name in ("list", "save"):
        item = attachments_sub.add_parser(name)
        item.add_argument("message_id", type=int)
        add_location(item)
        if name == "save":
            item.add_argument("--attachment-id", required=True)
            item.add_argument("--output", required=True, help="New destination file; existing files are refused.")

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
    audit.add_argument("--fresh", action="store_true", help="Read fresh attachment evidence instead of using cached results.")
    difference = campaign_sub.add_parser("diff", help="Preview exact differences between two campaign manifests.")
    difference.add_argument("before")
    difference.add_argument("after")
    explain = campaign_sub.add_parser("explain", help="Explain the evidence and blockers for one campaign row.")
    explain.add_argument("manifest")
    explain.add_argument("--key", required=True)
    explain.add_argument("--database")
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
    followups = campaign_sub.add_parser("followups", help="Build a local human-review worklist; never sends or schedules follow-ups.")
    followups.add_argument("manifest")
    followups.add_argument("--account-key", required=True)
    followups.add_argument("--database")
    followups.add_argument("--after-days", type=int, default=7)
    followups.add_argument("--as-of", help="Optional timezone-aware date/time for reproducible review.")
    followups.add_argument("--output", help="New private JSON artifact; refuses existing files.")
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
    session_next.add_argument("--owner", default="mailctl")
    session_record = campaign_sub.add_parser("session-record", help="Record one visible-UI attempt without claiming verification.")
    session_record.add_argument("session")
    session_record.add_argument("--key", required=True)
    session_record.add_argument("--result", required=True, choices=["applied", "failed", "skipped"])
    session_record.add_argument("--note")
    session_record.add_argument("--claim-token", help="Ownership token returned by session-next; required to record a result.")
    session_status = campaign_sub.add_parser("session-status", help="Summarize a resumable scheduling session.")
    session_status.add_argument("session")
    session_reconcile = campaign_sub.add_parser("session-reconcile", help="Verify session outcomes against Sent and Send Later.")
    session_reconcile.add_argument("session")
    session_reconcile.add_argument("--database")

    ubd = sub.add_parser("ubd", help="Create redacted UBD OS events or use its proposal-only Agent API.")
    ubd_sub = ubd.add_subparsers(dest="ubd_command", required=True)
    retry = ubd_sub.add_parser("retry-proposal", help="Resubmit exactly the saved redacted proposal with its original idempotency key.")
    retry.add_argument("operation_id")
    retry.add_argument("--confirm-proposal", action="store_true", required=True)
    add_ubd_connection(retry)

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
                    "receipt",
                    "campaign diff",
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
                    "attachments save",
                    "mutation receipts and ownership claims",
                    "receipt-reconcile",
                    "campaign followups --output",
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
                    "read-batch",
                    "receipt-reconcile",
                    "campaign followups",
                    "resolve",
                    "draft show",
                    "draft verify",
                    "attachments list",
                    "campaign explain",
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
                "mail_draft_mutation": ["compose", "reply", "draft update"],
                "mail_confirmed_mutation": ["mark", "flag", "move", "trash", "send"],
                "ubd_read_only": ["ubd capabilities", "ubd work-list", "ubd work-get"],
                "ubd_proposal_only": ["ubd propose-campaign-audit", "ubd propose-classification", "ubd propose-dashboard", "ubd retry-proposal"],
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
                "mail_mutations_automatically_retried": False,
                "send_acceptance_is_delivery_evidence": False,
                "scheduling_claim_token_required": True,
                "cross_campaign_identity_claims": True,
                "sent_observation_is_delivery_evidence": False,
                "followups_human_review_only": True,
                "read_batch_max_ids_per_backend_call": 20,
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
            checks["automation_probe"] = run_backend({"command": "probe"}, timeout)["data"]
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
    else:
        checks["plugin_manifest_error"] = "Plugin manifest is missing."
        ready = False
    cache_root = Path.home() / ".codex" / "plugins" / "cache" / "personal" / "apple-mail-cli"
    cache_copies = []
    if cache_root.is_dir():
        source_digest = {file.name: file_digest(file) for file in SCRIPT_DIR.iterdir() if file.suffix in {".py", ".js"}}
        for candidate in sorted(cache_root.glob("*/skills/apple-mail-cli/scripts/mailctl.py")):
            cache_copies.append(
                {
                    "version": candidate.parents[3].name,
                    "path": str(candidate),
                    "matches_current": {file.name: file_digest(file) for file in candidate.parent.iterdir() if file.suffix in {".py", ".js"}} == source_digest,
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


def latest_reply_text(content: str) -> str:
    lines = []
    for line in content.splitlines():
        if re.match(r"^\s*(On .+wrote:|[-_]{2,}\s*(Original Message|原始郵件)|From:|寄件者[:：]|在.+寫道[:：])", line, re.I):
            break
        if line.strip() == "--":
            break
        # Footer instructions must start the line. A person's "I would like to
        # unsubscribe" is intent, not a boilerplate footer.
        footer = re.match(r"^\s*(?:(?:to unsubscribe[,;:]?\s+(?:please\s+)?(?:click|visit|follow|use|reply|go to)\b)|click here to unsubscribe\b|manage (?:your )?(?:email )?preferences\b)", line, re.I)
        if line.lstrip().startswith(">") or footer:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def explicit_opt_out(content: str) -> bool:
    patterns = (
        r"\b(?:please\s+|kindly\s+)?unsubscribe\s+(?:me|us)\b",
        r"\b(?:please|kindly)\s+unsubscribe\b",
        r"\b(?:i|we)\s+(?:would\s+like|want|wish|need)\s+to\s+(?:be\s+unsubscribed|unsubscribe)\b",
        r"\b(?:i|we)['’]d\s+like\s+to\s+unsubscribe\b",
        r"\b(?:remove|take)\s+(?:me|us)\s+(?:from|off)\b",
        r"\b(?:stop emailing|do not email|don't email)\b",
        r"退訂|退订|停止寄送|不要再寄",
    )
    for sentence in re.split(r"[.!?。！？\n]+", content.lower()):
        if re.search(r"\b(?:do not|don't|never)\s+(?:unsubscribe|remove)\b", sentence):
            continue
        if sentence.strip() == "unsubscribe" or any(re.search(pattern, sentence) for pattern in patterns):
            return True
    return False


def classify_message(message: dict[str, Any]) -> dict[str, Any]:
    subject = str(message.get("subject") or "")
    sender = str(message.get("sender") or "")
    original_content = str(message.get("content") or "")
    content = latest_reply_text(original_content)
    headers = Parser(policy=email_policy.default).parsestr(str(message.get("headers") or "")[:65536], headersonly=True)
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
    attachments = message.get("attachments", [])
    attachment_metadata_unavailable = not isinstance(attachments, list)
    names = []
    for item in attachments if isinstance(attachments, list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"].lower())
        else:
            attachment_metadata_unavailable = True
    dmarc = "dmarc" in lowered and ("aggregate report" in lowered or any(name.endswith((".xml", ".zip", ".gz")) for name in names))
    if explicit_opt_out(content):
        kind, confidence, action = "unsubscribe_request", 0.99, "Review and record the explicit opt-out before any future campaign."
        evidence.append("Explicit unsubscribe or stop-contact wording in the latest reply")
    elif any(term in lowered for term in bounce_terms):
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
    elif (headers.get("Auto-Submitted", "no").lower() != "no") or any(term in lowered for term in ["automatic reply", "auto-reply", "automated response", "this is an automated", "自動回覆", "自动回复", "自動回信", "自动回信"]):
        discovered = sorted(set(address.lower() for address in EMAIL_PATTERN.findall(content)))
        if discovered:
            kind, confidence, action = "auto_reply_with_contacts", 0.9, "Review any suggested partnership or PR contact and update tracking before resending."
            evidence.append("Automated-response wording plus email address")
        else:
            kind, confidence, action = "auto_reply", 0.85, "Record the acknowledgement and wait unless it requests another action."
            evidence.append("Automated-response wording")
    elif headers.get("In-Reply-To") or subject.lower().startswith(("re:", "fw:", "fwd:", "回覆:", "回复:")):
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
    truncated = bool(message.get("content_truncated"))
    if truncated:
        confidence = min(confidence, 0.5)
    return {
        "kind": kind,
        "confidence": confidence,
        "evidence": evidence,
        "failed_recipients": failed_recipients,
        "smtp_codes": smtp_codes,
        "discovered_contacts": discovered_contacts[:20],
        "suggested_action": action,
        "confidence_basis": "heuristic_not_calibrated",
        "requires_review": truncated or attachment_metadata_unavailable or confidence < 0.8,
        "attachment_metadata_unavailable": attachment_metadata_unavailable,
        "quoted_or_signature_text_removed": content != original_content.strip(),
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
    errors = []
    stopped_early = False
    attempted = 0
    counts: dict[str, int] = {}
    attempted_ids = []
    for message_id, message, failure in batch_read.read_selected(args, listing.get("messages", []), run_backend, MailCtlError):
        attempted += 1
        attempted_ids.append(message_id)
        try:
            if failure:
                raise failure
            classification = classify_message(message)
        except Exception as error:
            code = error.code if isinstance(error, MailCtlError) else "message_classification_failed"
            # Do not copy arbitrary exception text into a body-free triage artifact.
            errors.append({"id": message_id, "code": code, "type": type(error).__name__,
                           "message": "This message could not be read or classified; inspect it individually.", "requires_review": True})
            if code in batch_read.FATAL_CODES:
                stopped_early = True
            continue
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
        "ok": not errors,
        "command": "triage",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "mailbox": listing.get("mailbox"),
        "account_filter": listing.get("account_filter"),
        "scanned": listing.get("scanned"),
        "returned": len(items),
        "selected": len(listing.get("messages", [])),
        "attempted": attempted,
        "failed": len(errors),
        "complete": not errors,
        "partial": bool(errors and items),
        "stopped_early": stopped_early,
        "not_attempted_ids": [row.get("id") for row in listing.get("messages", []) if row.get("id") not in attempted_ids],
        "errors": errors,
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


def guarded_send(args: argparse.Namespace, *, prepared=None) -> dict[str, Any]:
    payload, evidence = prepared if prepared is not None else outgoing_preflight(args, "send")
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
        raise MailCtlError(f"Refusing to overwrite existing local artifact: {path}") from error
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
    operation_id = str(uuid.UUID(args.idempotency_key))
    proposal = ubd_connector.proposal_payload(event, work, expected_revision=args.expected_revision)
    try:
        saved = operation_state.get(operation_id)
    except ValueError:
        saved = None
    if saved:
        request = saved.get("request", {})
        if (request.get("event_id") != event["event"]["id"] or request.get("proposal", {}).get("expectedRevision") != args.expected_revision
                or request.get("origin") != getattr(client, "origin", None)):
            raise MailCtlError("The proposal ID belongs to different evidence or a different work revision.")
        if saved["state"] == "completed":
            return saved["result"]
        raise MailCtlError("Proposal outcome is uncertain. Inspect receipt and use ubd retry-proposal with the same ID after reviewing UBD state.")
    operation_state.begin(operation_id, "ubd proposal", {"event_id": event["event"]["id"], "proposal": proposal, "origin": getattr(client, "origin", None)}, retain_request=True)
    return submit_saved_proposal(client, operation_id)


def submit_saved_proposal(client, operation_id: str, *, retry: bool = False) -> dict:
    saved = operation_state.get(operation_id)
    if saved.get("command") != "ubd proposal":
        raise MailCtlError("Receipt is not a UBD proposal.")
    proposal = saved["request"]["proposal"]
    if saved["request"].get("origin") != getattr(client, "origin", None):
        raise MailCtlError("Saved proposal belongs to a different UBD origin.")
    if saved["state"] == "completed":
        return saved["result"]
    permitted = {"prepared", "unknown", "submitting"} if retry else {"prepared"}
    if saved["state"] not in permitted:
        raise MailCtlError("Proposal is in flight or completed; inspect its receipt.")
    operation_state.transition(operation_id, "submitting", expected=saved["state"])
    try:
        response = client.tool("work.propose_update", proposal, idempotency_key=operation_id)
        checked = ubd_connector.verify_proposal_response(response, expected_work_item_id=proposal["workItemId"], expected_revision=proposal["expectedRevision"], expected_request_id=operation_id)
        operation_state.transition(operation_id, "completed", checked)
        return checked
    except BaseException:
        operation_state.transition(operation_id, "unknown")
        raise


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
    if args.ubd_command == "retry-proposal":
        saved = operation_state.get(args.operation_id)
        if saved.get("command") != "ubd proposal" or not args.confirm_proposal:
            raise MailCtlError("Retry requires a saved UBD proposal and current confirmation.")
        proposal = saved["request"]["proposal"]
        ubd_connector.verify_capabilities(capabilities, required_tools=ubd_connector.PROPOSAL_REQUIRED_TOOLS,
                                          business_unit_id=proposal["businessUnitId"])
        # Exact same endpoint, body and idempotency key; server enforces the saved revision.
        response = submit_saved_proposal(client, args.operation_id, retry=True)
        return {"ok": True, "command": "ubd retry-proposal", "data": response,
                "safety": {"formal_state_changed": False, "human_review_required": True}}
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


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "receipt-reconcile":
        return {"command": "receipt-reconcile", **mail_workflow.receipt_reconcile(
            args.operation_id, args.message_id, args.account_key, database=args.database)}
    if args.command == "read-batch":
        items, errors, attempted = [], [], set()
        for message_id, item, failure in batch_read.read_selected(args, [{"id": value} for value in args.message_ids], run_backend, MailCtlError):
            attempted.add(message_id)
            if failure:
                errors.append({"id": message_id, "code": failure.code, "requires_review": True})
            else:
                items.append(item)
        skipped = [value for value in args.message_ids if value not in attempted]
        return {"ok": not errors and not skipped, "command": "read-batch", "data": {
            "items": items, "errors": errors, "not_attempted_ids": skipped, "complete": not errors and not skipped}}
    if args.command == "receipt":
        return {"ok": True, "command": "receipt", "data": operation_state.get(args.operation_id)}
    if args.command == "resolve":
        return run_backend({"command": "resolve", "rfc_message_id": args.rfc_message_id,
                            "mailbox": args.mailbox, "account": args.account}, args.timeout)
    if args.command == "draft":
        if args.draft_command == "update":
            return prepare_verified_draft(args, update=True)
        current = draft_snapshot(run_backend({"command": "draft_read", "draft_id": args.draft_id}, args.timeout)["data"])
        data = {**current, "revision": operation_state.digest(current), "sent": False}
        if args.draft_command == "verify":
            payload, evidence = outgoing_preflight(args, "compose")
            data["verification"] = draft_verification(current, payload, evidence)
        return {"ok": data.get("verification", {}).get("verified", True), "command": "draft " + args.draft_command, "data": data}
    if args.command == "attachments":
        location = {"message_id": args.message_id, "mailbox": args.mailbox, "account": args.account}
        metadata = run_backend({"command": "attachments_list", **location}, args.timeout)["data"]
        if args.attachments_command == "list":
            return {"ok": metadata is not None, "command": "attachments list", "data": metadata}
        matches = [item for item in metadata or [] if item.get("id") == args.attachment_id]
        if len(matches) != 1:
            raise MailCtlError("Attachment ID must match exactly one readable attachment.")
        output = Path(args.output).expanduser().absolute()
        if output.exists() or output.is_symlink():
            raise MailCtlError("Attachment output already exists; overwriting is refused.")
        if not output.parent.is_dir():
            raise MailCtlError("Attachment output parent directory must already exist.")
        # Save to a private sibling first; publish via exclusive hard link so races cannot overwrite a file.
        with tempfile.TemporaryDirectory(prefix=".mailctl-attachment-", dir=output.parent) as temp:
            stage = Path(temp) / "attachment"
            run_backend({"command": "attachment_save", **location, "attachment_id": args.attachment_id, "output": str(stage)}, args.timeout)
            if not stage.is_file() or stage.is_symlink():
                raise MailCtlError("Mail did not produce a regular attachment file.")
            stage.chmod(0o600)
            evidence = attachment_preflight([str(stage)])[0]
            os.link(stage, output)
        evidence.update(path=str(output), name=output.name)
        return {"ok": True, "command": "attachments save", "data": evidence, "mail_mutated": False}
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
                account=args.account,
                exact_address=args.exact_address,
                message_id=args.message_id,
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
        if args.campaign_command == "followups":
            result = {"command": "campaign followups", **mail_workflow.followups(
                args.manifest, args.account_key, database=args.database, after_days=args.after_days, as_of=args.as_of)}
            if args.output:
                result["output"] = write_private_json(args.output, result)
            return result
        if args.campaign_command == "diff":
            return {"command": "campaign diff", **campaign.campaign_diff(args.before, args.after)}
        if args.campaign_command == "explain":
            audit = campaign.audit_campaign(args.manifest, database=args.database, fresh=True)
            matches = [row for row in audit["results"] if row["key"] == args.key]
            if not matches:
                raise MailCtlError("Campaign key not found.")
            return {"ok": True, "command": "campaign explain", "data": matches[0]}
        if args.campaign_command == "validate":
            return {"command": "campaign validate", **public_campaign_report(campaign.validate_manifest(args.manifest))}
        if args.campaign_command == "plan":
            report = campaign.validate_manifest(args.manifest, require_schedule=True)
            return {"command": "campaign plan", **public_campaign_report(report)}
        if args.campaign_command == "render":
            return {"command": "campaign render", **campaign.render_campaign(args.manifest, args.output, force=args.force)}
        if args.campaign_command == "audit":
            return {"command": "campaign audit", **campaign.audit_campaign(args.manifest, database=args.database, csv_path=args.csv, fresh=args.fresh)}
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
                **campaign.next_session_operations(args.session, database=args.database, limit=args.limit, owner=args.owner),
            }
        if args.campaign_command == "session-record":
            return {
                "command": "campaign session-record",
                **campaign.record_schedule_session(args.session, key=args.key, result=args.result, note=args.note, claim_token=args.claim_token),
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
        return prepare_verified_draft(args)
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
    result = run_backend(payload, args.timeout)
    if result.get("data", {}).get("verified") is False and args.command in {"mark", "flag"}:
        result.update(ok=False, error={"code": "mutation_unverified", "message": "Mail readback differs from the requested state.", "retryable": False})
    return result


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "send" and getattr(args, "confirm_send", None) is None:
        return guarded_send(args)  # Offline preview must not create receipts or load Mail.
    mutations = {"compose", "send", "reply", "mark", "flag", "move", "trash"}
    draft_update = args.command == "draft" and args.draft_command == "update"
    if args.command not in mutations and not draft_update:
        return _execute(args)
    operation_id = str(uuid.UUID(args.operation_id)) if args.operation_id else str(uuid.uuid4())
    request = {key: value for key, value in vars(args).items() if key not in {"operation_id", "compact", "timeout"}}
    prepared = outgoing_preflight(args, "send") if args.command == "send" else None
    if prepared:
        digest = send_guard.intent_digest(send_guard.canonical_intent(*prepared))
        if type(args.confirm_send) is not str or args.confirm_send != digest:
            raise MailCtlError("Confirmation does not match the current outbound intent.", code="send_intent_mismatch")
    if args.command in {"compose", "send", "reply"} or draft_update:
        request["body_sha256"] = hashlib.sha256((prepared[0]["body"] if prepared else validated_body(args)).encode()).hexdigest()
    if args.command in {"compose", "send"} or draft_update:
        request["attachment_evidence"] = prepared[1] if prepared else attachment_preflight(args.attachment)
    saved = operation_state.begin(operation_id, args.command, request)
    if saved["state"] in {"completed", "accepted"}:
        return {**saved["result"], "replayed_receipt": True}
    if saved["state"] != "prepared":
        raise MailCtlError(f"Operation {operation_id} is in flight or uncertain. Inspect its receipt before another attempt.", code="operation_unknown")
    if prepared:
        payload, evidence = prepared
        intent = {key: payload[key] for key in ("sender", "to", "cc", "bcc", "subject")}
        intent.update(body_sha256=request["body_sha256"], attachments=[
            {key: item[key] for key in ("name", "size_bytes", "sha256")} for item in evidence])
        operation_state.annotate(operation_id, "send_intent", intent)
    operation_state.transition(operation_id, "submitting", expected="prepared")
    try:
        result = guarded_send(args, prepared=prepared) if prepared else _execute(args)
        result["operation_id"] = operation_id
        state = "accepted" if result.get("data", {}).get("accepted") or result.get("data", {}).get("submission_accepted") else "completed" if result.get("ok") else "unknown"
        operation_state.transition(operation_id, state, result)
        return result
    except BaseException as error:
        operation_state.transition(operation_id, "unknown")
        if isinstance(error, MailCtlError):
            error.retryable = False
            error.hint = f"Inspect mailctl receipt {operation_id}; do not retry the mutation automatically."
        raise


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
    elif isinstance(error, ValueError):
        code, hint, retryable = "validation_error", "Review the exact arguments and saved evidence; do not bypass an uncertain operation.", False
        message = "Invalid arguments or inconsistent saved evidence; no successful result was confirmed."
    else:
        code, hint, retryable = "unexpected_error", "Run mailctl doctor and retry only after reviewing actual Mail state.", False
        message = "Unexpected local failure; no successful result was confirmed."
    return {
        "ok": False,
        "command": command,
        "schema_version": CLI_SCHEMA_VERSION,
        "version": VERSION,
        "observed_at": operation_state.now(),
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
        result.setdefault("schema_version", CLI_SCHEMA_VERSION)
        result.setdefault("version", VERSION)
        result.setdefault("observed_at", operation_state.now())
        emit(result, compact=args.compact)
        return 0 if result.get("ok") else 1
    except Exception as error:
        emit(structured_error(error, args.command), compact=args.compact)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
