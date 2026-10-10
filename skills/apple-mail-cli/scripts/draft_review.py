"""Inspect one saved Drafts MIME snapshot for human review. No Mail mutations."""
from __future__ import annotations

import base64
from collections import Counter
from email import policy
from email.parser import BytesParser
import hashlib
import json
import re

MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_BODY_BYTES = 5_000_000
SCHEMA = "mailctl-saved-draft-review/v1"


class DraftReviewError(ValueError):
    pass


def message_id(value: str) -> str:
    if not isinstance(value, str):
        raise DraftReviewError("An exact RFC Message-ID is required.")
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if not re.fullmatch(r"[^\s@<>\x00-\x1f\x7f]+@[^\s@<>\x00-\x1f\x7f]+", value):
        raise DraftReviewError("Invalid RFC Message-ID.")
    return "<" + value + ">"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def addresses(message, header: str, *, required=False) -> list[str]:
    values = message.get_all(header, [])
    if len(values) > 1 or (required and len(values) != 1):
        raise DraftReviewError("Missing or duplicate envelope header: " + header)
    if not values:
        return []
    field = values[0]
    if field.defects:
        raise DraftReviewError("Malformed envelope header: " + header)
    result = [address.addr_spec.lower() for address in field.addresses]
    if any(not re.fullmatch(r"[^\s@<>\x00-\x1f\x7f]+@[^\s@<>\x00-\x1f\x7f]+\.[^\s@<>\x00-\x1f\x7f]+", item) for item in result):
        raise DraftReviewError("Invalid envelope address in: " + header)
    return result


def decoded_bytes(part) -> bytes:
    if len(part.get_all("Content-Transfer-Encoding", [])) > 1:
        raise DraftReviewError("Duplicate transfer encoding.")
    encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
    if encoding not in {"base64", "quoted-printable", "7bit", "8bit", "binary"}:
        raise DraftReviewError("Unsupported transfer encoding.")
    raw = part.get_payload()
    if not isinstance(raw, str):
        raise DraftReviewError("Opaque multipart attachment cannot be verified.")
    if encoding == "base64":
        try:
            decoded = base64.b64decode(re.sub(rb"[\r\n\t ]", b"", raw.encode("ascii")), validate=True)
        except (ValueError, UnicodeError) as error:
            raise DraftReviewError("Invalid base64 payload.") from error
    else:
        if encoding == "quoted-printable" and re.search(r"=(?![0-9a-fA-F]{2}|\r?\n)", raw):
            raise DraftReviewError("Invalid quoted-printable payload.")
        decoded = part.get_payload(decode=True)
    if not isinstance(decoded, bytes) or part.defects:
        raise DraftReviewError("MIME payload is incomplete or malformed.")
    return decoded


def inspect_saved(snapshot: dict, intent: dict, body_compare, *, expected_review_digest: str | None = None) -> dict:
    """All positive assertions are scoped to this saved source, never its editor."""
    required = ("account", "account_id", "message_id", "source")
    if (not isinstance(snapshot, dict) or any(not isinstance(snapshot.get(k), str) or not snapshot[k] for k in required)
            or type(snapshot.get("numeric_id")) is not int or snapshot["numeric_id"] < 0
            or snapshot.get("source_stable") is not True):
        raise DraftReviewError("Saved draft source identity is incomplete.")
    identity = {key: snapshot[key] for key in ("account", "account_id", "numeric_id")}
    identity["message_id"] = message_id(snapshot["message_id"])
    try:
        raw = snapshot["source"].encode("utf-8")
        if len(raw) > MAX_SOURCE_BYTES:
            raise DraftReviewError("Saved MIME source exceeds the 40 MiB review limit.")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        mids = message.get_all("Message-ID", [])
        if len(mids) != 1 or mids[0].defects or message_id(str(mids[0])) != identity["message_id"]:
            raise DraftReviewError("MIME Message-ID does not match the selected saved draft.")
        subjects = message.get_all("Subject", [])
        if len(subjects) != 1 or subjects[0].defects:
            raise DraftReviewError("Missing, duplicate or malformed Subject header.")
        senders = addresses(message, "From", required=True)
        envelope = {"sender": senders[0] if len(senders) == 1 else None, "subject": str(subjects[0]),
                    "to": addresses(message, "To", required=True), "cc": addresses(message, "Cc"), "bcc": addresses(message, "Bcc")}
        envelope_issues = [field for field in ("sender", "subject") if envelope[field] != intent[field]]
        envelope_issues += [field for field in ("to", "cc", "bcc") if Counter(envelope[field]) != Counter(intent[field])]
        # Resent/Sender routing headers require explicit human review, not a guessed envelope.
        if message.get_all("Sender", []) or any(key.lower().startswith("resent-") for key in message.keys()):
            envelope_issues.append("alternate_routing_headers")
        attachments, plain, html, mime_issues = [], [], [], []
        stack, count, total_attachment_bytes = [(message, 0)], 0, 0
        while stack:
            part, depth = stack.pop()
            count += 1
            if count > 200 or depth > 20:
                raise DraftReviewError("MIME tree exceeds the review bound.")
            if part.defects or any(len(part.get_all(key, [])) > 1 or any(header.defects for header in part.get_all(key, []))
                                   for key in ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding")):
                raise DraftReviewError("Malformed or ambiguous MIME part.")
            name, disposition = part.get_filename(), part.get_content_disposition()
            kind = part.get_content_type()
            if name is not None or disposition == "attachment":
                if not name or len(name) > 255 or re.search(r"[\x00-\x1f\x7f/\\]", name):
                    raise DraftReviewError("Attachment has no safe, unambiguous filename.")
                data = decoded_bytes(part)
                total_attachment_bytes += len(data)
                if len(attachments) >= 20 or total_attachment_bytes > MAX_ATTACHMENT_BYTES:
                    raise DraftReviewError("Attachment review exceeds 20 files or 25 MiB.")
                attachments.append({"name": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            elif part.is_multipart():
                if kind not in {"multipart/mixed", "multipart/alternative", "multipart/related"}:
                    raise DraftReviewError("Signed, encrypted or opaque MIME needs a separate review.")
                stack.extend((child, depth + 1) for child in reversed(part.get_payload()))
            elif kind in {"text/plain", "text/html"}:
                data = decoded_bytes(part)
                if len(data) > MAX_BODY_BYTES:
                    raise DraftReviewError("Body part exceeds the review limit.")
                text = data.decode(part.get_content_charset() or "ascii", errors="strict")
                (plain if kind == "text/plain" else html).append(text)
            else:
                mime_issues.append("uninspectable_unnamed_part")
        signature = lambda items: Counter((item["name"], item["size_bytes"], item["sha256"]) for item in items)
        attachment_match = not mime_issues and signature(attachments) == signature(intent["attachments"])
        body = plain[0] if len(plain) == 1 else None
        # MIME line endings are transport syntax; preserve all other characters.
        comparison = body_compare(body.replace("\r\n", "\n") if body is not None else None, intent["body"])
        issues = ["envelope:" + item for item in envelope_issues] + mime_issues
        if not attachment_match:
            issues.append("attachments_mismatch_or_unverified")
        if comparison == "mismatch" or len(plain) != 1:
            issues.append("body_requires_review")
        result = {"schema": SCHEMA, "identity": identity,
                  "source_text_sha256": hashlib.sha256(raw).hexdigest(), "intent_digest": digest(intent),
                  "status": "needs_content_review" if issues else "awaiting_human_confirmation", "issues": issues,
                  "envelope": envelope, "envelope_verified": not envelope_issues,
                  "attachments": attachments, "attachment_bytes_verified": attachment_match,
                  "body_comparison": comparison, "body_exact_match": body == intent["body"],
                  "saved_plain_body": body, "expected_body": intent["body"],
                  "plain_body_parts": len(plain), "has_html": bool(html), "html_rendering_verified": False,
                  "requires_visual_review": True, "requires_human_confirmation": True,
                  "send_authorized": False, "send_attempted": False, "delivery_verified": False,
                  "open_draft_link_verified": False, "mail_mutated": False}
        result["review_digest"] = digest({"schema": SCHEMA, "identity": identity,
            "source_text_sha256": result["source_text_sha256"], "intent_digest": result["intent_digest"]})
        if expected_review_digest is not None and expected_review_digest != result["review_digest"]:
            raise DraftReviewError("Saved draft review changed. Inspect a fresh review; prior confirmation is stale.")
        return result
    except DraftReviewError:
        raise
    except (ValueError, TypeError, AttributeError, LookupError, UnicodeError, RecursionError) as error:
        raise DraftReviewError("Saved draft MIME could not be completely inspected.") from error
