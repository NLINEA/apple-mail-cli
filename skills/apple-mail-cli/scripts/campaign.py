"""Deterministic campaign validation, rendering, and Apple Mail reconciliation."""

from __future__ import annotations

import csv
import bisect
import copy
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mail_index import MailIndexError, list_messages, unique_messages
from mail_store import load_send_later_evidence
import operation_state


EMAIL_PATTERN = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
CONTROL_PATTERN = re.compile(r"[\u0000-\u001f\u007f]")
QUEUE_SCHEMA_VERSION = "apple-mail-cli-queue/v1"
SESSION_SCHEMA_VERSION = "apple-mail-cli-session/v1"
MAX_MANIFEST_BYTES = 10 * 1024 * 1024


class CampaignError(RuntimeError):
    pass


def load_manifest(path: str) -> tuple[Path, dict[str, Any]]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise CampaignError("Campaign manifest exceeds the 10 MiB local safety limit.")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CampaignError(f"Cannot read campaign manifest {manifest_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CampaignError(f"Invalid campaign JSON at line {error.lineno}, column {error.colno}: {error.msg}") from error
    if not isinstance(data, dict):
        raise CampaignError("Campaign manifest root must be a JSON object.")
    return manifest_path, data


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_identifier(value: Any, label: str, errors: list[str]) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        errors.append(f"{label} is required.")
    elif not SAFE_IDENTIFIER_PATTERN.fullmatch(normalized):
        errors.append(f"{label} must use 1-120 letters, digits, dots, colons, underscores, or hyphens.")
    return normalized


def bounded_policy_integer(value: Any, label: str, errors: list[str], *, maximum: int) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        errors.append(f"{label} must be an integer between 0 and {maximum}.")
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label} must be an integer between 0 and {maximum}.")
        return 0
    if parsed < 0 or parsed > maximum:
        errors.append(f"{label} must be between 0 and {maximum}.")
        return 0
    return parsed


def write_private_bytes(path: Path, value: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise CampaignError(f"Refusing to overwrite rendered artifact: {path}") from error
    except OSError as error:
        raise CampaignError(f"Cannot create rendered artifact {path}: {error}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
    except OSError as error:
        path.unlink(missing_ok=True)
        raise CampaignError(f"Cannot write rendered artifact {path}: {error}") from error


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    write_private_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def write_private_json_atomic(path: Path, value: dict[str, Any], *, create_only: bool = False) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_only and path.exists():
        raise CampaignError(f"Refusing to overwrite existing private artifact: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def parse_time(value: Any, label: str, errors: list[str]) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label}: invalid ISO timestamp '{value}'.")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{label}: timestamp must include a UTC offset.")
        return None
    return parsed


def policy_timezone(policy: dict[str, Any], errors: list[str] | None = None) -> ZoneInfo | None:
    value = str(policy.get("timezone") or "").strip()
    if not value:
        return None
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        if errors is not None:
            errors.append(f"policy.timezone is not a valid IANA timezone: '{value}'.")
            return None
        raise CampaignError(f"policy.timezone is not a valid IANA timezone: '{value}'.")


def localize(value: datetime, zone: ZoneInfo | None) -> datetime:
    return value.astimezone(zone) if zone is not None else value


def in_allowed_hours(value: datetime, allowed_hours: list[str] | None, zone: ZoneInfo | None) -> bool:
    if not allowed_hours:
        return True
    start = datetime.strptime(allowed_hours[0], "%H:%M").time()
    end = datetime.strptime(allowed_hours[1], "%H:%M").time()
    local_time = localize(value, zone).timetz().replace(tzinfo=None)
    if start <= end:
        return start <= local_time <= end
    return local_time >= start or local_time <= end


def addresses(value: Any, label: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{label}: must be an array of addresses.")
        return []
    result: list[str] = []
    for raw in value:
        address = str(raw).strip().lower()
        if not EMAIL_PATTERN.match(address):
            errors.append(f"{label}: invalid address '{raw}'.")
        elif address in result:
            errors.append(f"{label}: duplicate address '{address}'.")
        else:
            result.append(address)
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_attachment(raw: Any, label: str, errors: list[str], warnings: list[str], cache: dict | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        errors.append(f"{label}: attachment must be an object.")
        return None
    path_value = raw.get("path")
    if not path_value:
        errors.append(f"{label}: missing path.")
        return None
    path = Path(str(path_value)).expanduser().resolve()
    if not path.is_file():
        errors.append(f"{label}: file not found: {path}")
        return None
    stat = path.stat()
    actual_name = path.name
    if CONTROL_PATTERN.search(actual_name):
        errors.append(f"{label}: filename contains control characters.")
        return None
    fingerprint = (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    actual_hash = (cache or {}).get(fingerprint)
    if actual_hash is None:
        actual_hash = sha256(path)
        after = path.stat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != fingerprint[1:]:
            errors.append(f"{label}: attachment changed while being read.")
            return None
        if cache is not None:
            cache[fingerprint] = actual_hash
    expected_name = raw.get("name")
    expected_size = raw.get("size_bytes")
    expected_hash = str(raw.get("sha256") or "").lower()
    if expected_name and expected_name != actual_name:
        errors.append(f"{label}: expected filename '{expected_name}', found '{actual_name}'.")
    if expected_size is not None:
        try:
            parsed_size = int(expected_size)
        except (TypeError, ValueError):
            errors.append(f"{label}: size_bytes must be a non-negative integer.")
        else:
            if parsed_size < 0:
                errors.append(f"{label}: size_bytes must be a non-negative integer.")
            elif parsed_size != stat.st_size:
                errors.append(f"{label}: expected {expected_size} bytes, found {stat.st_size}.")
    if expected_hash and not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        errors.append(f"{label}: sha256 must contain exactly 64 lowercase hexadecimal characters.")
    if expected_hash and expected_hash != actual_hash:
        errors.append(f"{label}: SHA-256 does not match.")
    if not expected_hash:
        warnings.append(f"{label}: no expected SHA-256 supplied; recorded actual digest.")
    return {"path": str(path), "name": actual_name, "size_bytes": stat.st_size, "sha256": actual_hash}


def message_attachments(
    message: dict[str, Any], defaults: dict[str, Any], label: str, errors: list[str], warnings: list[str], cache: dict | None = None
) -> list[dict[str, Any]]:
    raw_values = message.get("attachments", defaults.get("attachments", []))
    if not isinstance(raw_values, list):
        errors.append(f"{label}.attachments: must be an array.")
        return []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_values):
        evidence = validate_attachment(raw, f"{label}.attachments[{index}]", errors, warnings, cache)
        if evidence:
            result.append(evidence)
    return result


def validate_manifest(
    manifest_path: str,
    *,
    require_schedule: bool = False,
    enforce_schedule_policy: bool = True,
) -> dict[str, Any]:
    path, manifest = load_manifest(manifest_path)
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1.")
    campaign_id = validate_identifier(manifest.get("campaign_id"), "campaign_id", errors)
    sender = str(manifest.get("sender") or "").strip().lower()
    if not EMAIL_PATTERN.match(sender):
        errors.append("sender must be a valid email address.")
    defaults = manifest.get("defaults") or {}
    if not isinstance(defaults, dict):
        errors.append("defaults must be an object.")
        defaults = {}
    policy = manifest.get("policy") or {}
    if not isinstance(policy, dict):
        errors.append("policy must be an object.")
        policy = {}
    zone = policy_timezone(policy, errors)
    raw_messages = manifest.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        errors.append("messages must be a non-empty array.")
        raw_messages = []

    raw_excluded_risks = policy.get("excluded_risks", [])
    if not isinstance(raw_excluded_risks, list):
        errors.append("policy.excluded_risks must be an array.")
        raw_excluded_risks = []
    excluded_risks = {str(item).strip().lower() for item in raw_excluded_risks if str(item).strip()}
    active: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    keys: set[str] = set()
    identities: set[tuple[tuple[str, ...], str]] = set()
    attachment_cache: dict = {}

    for index, raw in enumerate(raw_messages):
        label = f"messages[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: must be an object.")
            continue
        key = validate_identifier(raw.get("key"), f"{label}.key", errors)
        if key and key in keys:
            errors.append(f"{label}.key duplicates '{key}'.")
        if key:
            keys.add(key)
        to = addresses(raw.get("to"), f"{label}.to", errors)
        cc = addresses(raw.get("cc", []), f"{label}.cc", errors)
        bcc = addresses(raw.get("bcc", []), f"{label}.bcc", errors)
        if not to:
            errors.append(f"{label}.to requires at least one valid recipient.")
        if set(to) & (set(cc) | set(bcc)) or set(cc) & set(bcc):
            errors.append(f"{label}: an address appears in more than one recipient field.")
        subject = str(raw.get("subject") or "").strip()
        body = str(raw.get("body") or "")
        if not subject:
            errors.append(f"{label}.subject is required.")
        elif CONTROL_PATTERN.search(subject):
            errors.append(f"{label}.subject cannot contain control characters.")
        if not body.strip():
            errors.append(f"{label}.body is required.")
        required_signature = str(raw.get("required_signature", defaults.get("required_signature", "")))
        if required_signature and required_signature not in body:
            errors.append(f"{label}.body is missing required signature text '{required_signature}'.")
        require_blank = bool(raw.get("require_blank_line_before_attachment", defaults.get("require_blank_line_before_attachment", False)))
        attachments = message_attachments(raw, defaults, label, errors, warnings, attachment_cache)
        if attachments and require_blank and not body.endswith("\n\n"):
            errors.append(f"{label}.body must end with a blank line before attachments.")
        schedule_at = parse_time(raw.get("schedule_at"), f"{label}.schedule_at", errors)
        if require_schedule and schedule_at is None:
            errors.append(f"{label}.schedule_at is required for scheduling.")
        risk = str(raw.get("risk") or "").strip().lower()
        exclusion_reasons = []
        if raw.get("manual"):
            exclusion_reasons.append("manual")
        if raw.get("exclude"):
            exclusion_reasons.append("exclude")
        if raw.get("do_not_send"):
            exclusion_reasons.append("do_not_send")
        if risk and risk in excluded_risks:
            exclusion_reasons.append(f"risk:{risk}")

        message_sender = str(raw.get("sender") or sender).strip().lower()
        if not EMAIL_PATTERN.match(message_sender):
            errors.append(f"{label}.sender must be a valid email address.")

        normalized = {
            "index": index,
            "key": key,
            "company": str(raw.get("company") or "").strip(),
            "sender": message_sender,
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "body": body,
            "schedule_at": schedule_at.isoformat() if schedule_at else None,
            "risk": risk or None,
            "attachments": attachments,
            "exclusion_reasons": exclusion_reasons,
        }
        identity = (tuple(sorted(to)), subject)
        if not exclusion_reasons:
            if identity in identities:
                errors.append(f"{label}: duplicate recipient-plus-subject campaign identity.")
            identities.add(identity)
            active.append(normalized)
        else:
            skipped.append(normalized)

    times = sorted(
        [(datetime.fromisoformat(item["schedule_at"]), item["key"]) for item in active if item["schedule_at"]],
        key=lambda pair: pair[0],
    )
    minimum_gap = bounded_policy_integer(
        policy.get("minimum_gap_minutes", 0),
        "policy.minimum_gap_minutes",
        errors,
        maximum=1_440,
    )
    if enforce_schedule_policy:
        for previous, current in zip(times, times[1:]):
            gap = (current[0] - previous[0]).total_seconds() / 60
            if gap < minimum_gap:
                errors.append(
                    f"Schedule gap between '{previous[1]}' and '{current[1]}' is {gap:g} minutes; minimum is {minimum_gap}."
                )

    by_day = Counter(localize(value[0], zone).date().isoformat() for value in times)
    daily_maximum = bounded_policy_integer(
        policy.get("daily_maximum", 0),
        "policy.daily_maximum",
        errors,
        maximum=100_000,
    )
    if daily_maximum and enforce_schedule_policy:
        for day, count in by_day.items():
            if count > daily_maximum:
                errors.append(f"{day} has {count} messages; daily maximum is {daily_maximum}.")

    allowed_hours = policy.get("allowed_hours")
    if allowed_hours:
        try:
            if not isinstance(allowed_hours, list) or len(allowed_hours) != 2 or not all(isinstance(v, str) for v in allowed_hours):
                raise ValueError("Invalid allowed_hours shape")
            datetime.strptime(allowed_hours[0], "%H:%M").time()
            datetime.strptime(allowed_hours[1], "%H:%M").time()
            if enforce_schedule_policy:
                for scheduled, key in times:
                    if not in_allowed_hours(scheduled, allowed_hours, zone):
                        local_time = localize(scheduled, zone).strftime("%H:%M")
                        errors.append(
                            f"'{key}' at {local_time} is outside allowed hours {allowed_hours[0]}–{allowed_hours[1]}."
                        )
        except (ValueError, TypeError, IndexError):
            errors.append("policy.allowed_hours must be two HH:MM strings.")

    return {
        "ok": not errors,
        "manifest_path": str(path),
        "campaign_id": campaign_id,
        "sender": sender,
        "policy": policy,
        "timezone": str(zone) if zone else "timestamp_offsets",
        "active_messages": active,
        "skipped_messages": skipped,
        "summary": {
            "total": len(raw_messages),
            "active": len(active),
            "skipped": len(skipped),
            "scheduled": len(times),
            "first_schedule": times[0][0].isoformat() if times else None,
            "last_schedule": times[-1][0].isoformat() if times else None,
            "by_day": dict(sorted(by_day.items())),
        },
        "errors": errors,
        "warnings": warnings,
    }


def require_valid(report: dict[str, Any]) -> None:
    if not report["ok"]:
        raise CampaignError("Campaign validation failed; inspect the returned errors.")


def safe_filename(index: int, key: str) -> str:
    clean = SAFE_NAME_PATTERN.sub("-", key).strip("-.") or f"message-{index + 1}"
    return f"{index:04d}-{clean[:80]}.eml"


def source_fingerprint(item: dict[str, Any]) -> str:
    return text_sha256(
        canonical_json(
            {
                "key": item["key"],
                "sender": item["sender"],
                "to": item["to"],
                "cc": item["cc"],
                "bcc": item["bcc"],
                "subject": item["subject"],
                "body": item["body"],
                "schedule_at": item["schedule_at"],
                "attachments": item["attachments"],
            }
        )
    )


def prior_render_files(output: Path, campaign_id: str) -> list[Path]:
    """Resolve only artifacts explicitly owned by a readable previous queue."""
    queue_path, checkpoint_path = output / "queue.json", output / "checkpoint.json"
    if not queue_path.exists() and not queue_path.is_symlink():
        if checkpoint_path.exists() or checkpoint_path.is_symlink():
            raise CampaignError("Cannot replace an orphan checkpoint without its owning queue; use a new directory.")
        return []
    for path in (queue_path, checkpoint_path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise CampaignError("Previous render metadata is missing, oversized, or a symlink; use a new directory.")
    try:
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CampaignError("Previous render metadata is unreadable; use a new directory.") from error
    if (not isinstance(queue, dict) or not isinstance(checkpoint, dict)
            or queue.get("schema_version") != QUEUE_SCHEMA_VERSION or checkpoint.get("schema_version") != QUEUE_SCHEMA_VERSION
            or queue.get("campaign_id") != campaign_id or checkpoint.get("campaign_id") != campaign_id
            or not isinstance(queue.get("messages"), list)
            or not re.fullmatch(r"[0-9a-f]{64}", str(queue.get("manifest_sha256") or ""))
            or checkpoint.get("manifest_sha256") != queue["manifest_sha256"]):
        raise CampaignError("Previous render does not belong to this campaign; use a new directory.")
    owned = [queue_path, checkpoint_path]
    for index, row in enumerate(queue["messages"]):
        if not isinstance(row, dict) or not SAFE_IDENTIFIER_PATTERN.fullmatch(str(row.get("key") or "")):
            raise CampaignError("Previous queue contains an invalid row; use a new directory.")
        target = output / safe_filename(index, row["key"])
        recorded = Path(str(row.get("eml_path") or "")).expanduser()
        if recorded != target or target.is_symlink() or (target.exists() and not target.is_file()) or target in owned:
            raise CampaignError("Previous queue has an unsafe or conflicting artifact path; use a new directory.")
        if target.exists():
            owned.append(target)
    return owned


def render_campaign(manifest_path: str, output_directory: str, *, force: bool = False) -> dict[str, Any]:
    requested = Path(output_directory).expanduser()
    if requested.is_symlink():
        raise CampaignError("Render output must not be a symlink.")
    output = requested.resolve()
    # A directory-specific lock serializes competing CLI renderers; it lives outside the output.
    with operation_state.locked(output):
        return _render_campaign_locked(manifest_path, output, force=force)


def _render_campaign_locked(manifest_path: str, output: Path, *, force: bool) -> dict[str, Any]:
    manifest_digest = sha256(Path(manifest_path).expanduser().resolve())
    report = validate_manifest(manifest_path)
    require_valid(report)
    if output.exists() and any(output.iterdir()) and not force:
        raise CampaignError(f"Output directory is not empty: {output}. Pass --force to replace campaign render files.")
    owned = prior_render_files(output, report["campaign_id"]) if output.exists() else []
    new_names = [safe_filename(index, row["key"]) for index, row in enumerate(report["active_messages"])] + ["queue.json", "checkpoint.json"]
    for name in new_names:
        target = output / name
        if target.is_symlink() or ((target.exists()) and target not in owned):
            raise CampaignError(f"Refusing to overwrite an unowned artifact: {target}")
    snapshots = {path: sha256(path) for path in owned}
    with tempfile.TemporaryDirectory(prefix=".mailctl-render-", dir=output.parent) as temporary:
        stage = Path(temporary)
        result = render_staged_campaign(report, stage, output, manifest_digest)
        if sha256(Path(report["manifest_path"])) != manifest_digest:
            raise CampaignError("Manifest changed while rendering; previous artifacts are unchanged.")
        for path, digest in snapshots.items():
            if path.is_symlink() or not path.is_file() or sha256(path) != digest:
                raise CampaignError("Previous render changed concurrently; no artifacts were replaced.")
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.chmod(0o700)
        backup = Path(tempfile.mkdtemp(prefix=".mailctl-backup-", dir=output)) if owned else None
        if backup:
            for path in owned:
                shutil.copy2(path, backup / path.name)
                (backup / path.name).chmod(0o600)
        published = []
        try:
            # Publish the queue last. Readers stop on any transient checkpoint/digest mismatch.
            for name in [*new_names[:-2], "checkpoint.json", "queue.json"]:
                target = output / name
                if target in owned:
                    if target.is_symlink() or sha256(target) != snapshots[target]:
                        raise CampaignError("An owned artifact changed before publication; restoring the previous render.")
                    os.replace(stage / name, target)
                else:
                    os.link(stage / name, target)  # Exclusive creation, never replace an unexpected file.
                published.append(target)
        except BaseException:
            for target in reversed(published):
                if target in owned:
                    restore = stage / (target.name + ".restore")
                    shutil.copy2(backup / target.name, restore)
                    os.replace(restore, target)
                else:
                    target.unlink(missing_ok=True)
            raise
        result["backup_directory"] = str(backup) if backup else None
        result["retained_unreferenced_eml"] = [str(path) for path in owned if path.suffix == ".eml" and path.name not in new_names]
        return result


def render_staged_campaign(report: dict, output: Path, destination: Path, manifest_digest: str) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)

    queue: list[dict[str, Any]] = []
    for queue_index, item in enumerate(report["active_messages"]):
        message = EmailMessage()
        message["From"] = item["sender"]
        message["To"] = ", ".join(item["to"])
        if item["cc"]:
            message["Cc"] = ", ".join(item["cc"])
        message["Subject"] = item["subject"]
        message["X-Apple-Mail-CLI-Campaign"] = report["campaign_id"]
        message["X-Apple-Mail-CLI-Key"] = item["key"]
        message.set_content(item["body"], subtype="plain", charset="utf-8")
        for attachment in item["attachments"]:
            attachment_path = Path(attachment["path"])
            attachment_bytes = attachment_path.read_bytes()
            if len(attachment_bytes) != attachment["size_bytes"] or hashlib.sha256(attachment_bytes).hexdigest() != attachment["sha256"]:
                raise CampaignError(f"Attachment changed after validation: {attachment_path}")
            mime, _ = mimetypes.guess_type(attachment_path.name)
            maintype, subtype = (mime or "application/octet-stream").split("/", 1)
            message.add_attachment(
                attachment_bytes,
                maintype=maintype,
                subtype=subtype,
                filename=attachment["name"],
            )
        filename = safe_filename(queue_index, item["key"])
        target = output / filename
        rendered_bytes = message.as_bytes(policy=default_policy)
        write_private_bytes(target, rendered_bytes)
        queue.append(
            {
                "queue_index": queue_index,
                "key": item["key"],
                "company": item["company"],
                "to": item["to"],
                "cc": item["cc"],
                "bcc": item["bcc"],
                "sender": item["sender"],
                "subject": item["subject"],
                "schedule_at": item["schedule_at"],
                "attachments": item["attachments"],
                "eml_path": str(destination / filename),
                "eml_sha256": hashlib.sha256(rendered_bytes).hexdigest(),
                "source_sha256": source_fingerprint(item),
            }
        )
    queue_path = output / "queue.json"
    checkpoint_path = output / "checkpoint.json"
    write_private_json(
        queue_path,
        {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "campaign_id": report["campaign_id"],
            "manifest_sha256": manifest_digest,
            "messages": queue,
        },
    )
    write_private_json(
        checkpoint_path,
        {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "campaign_id": report["campaign_id"],
            "manifest_path": report["manifest_path"],
            "manifest_sha256": manifest_digest,
            "completed_keys": [],
            "last_audit": None,
        },
    )
    return {
        "ok": True,
        "campaign_id": report["campaign_id"],
        "output_directory": str(destination),
        "rendered": len(queue),
        "manifest_sha256": manifest_digest,
        "queue_path": str(destination / "queue.json"),
        "checkpoint_path": str(destination / "checkpoint.json"),
        "warnings": report["warnings"],
    }


def normalize_message_id(value: str) -> str:
    return value.strip().lower()


def mail_version_root(index_path: Path) -> Path | None:
    if index_path.name != "Envelope Index" or index_path.parent.name != "MailData":
        return None
    candidate = index_path.parent.parent
    return candidate if candidate.name.startswith("V") else None


def attachment_signature(values: list[dict[str, Any]]) -> Counter[tuple[str, int, str]]:
    return Counter(
        (
            str(item.get("name") or ""),
            int(item.get("size_bytes") or 0),
            str(item.get("sha256") or "").lower(),
        )
        for item in values
    )


def attachment_mismatch_fields(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    if len(expected) != len(actual):
        fields.append("count")
    if Counter(str(item.get("name") or "") for item in expected) != Counter(str(item.get("name") or "") for item in actual):
        fields.append("filename")
    if Counter((str(item.get("name") or ""), int(item.get("size_bytes") or 0)) for item in expected) != Counter((str(item.get("name") or ""), int(item.get("size_bytes") or 0)) for item in actual):
        fields.append("size_bytes")
    if Counter((str(item.get("name") or ""), str(item.get("sha256") or "").lower()) for item in expected) != Counter(
        (str(item.get("name") or ""), str(item.get("sha256") or "").lower()) for item in actual
    ):
        fields.append("sha256")
    return fields


def audit_campaign(manifest_path: str, *, database: str | None = None, csv_path: str | None = None, fresh: bool = False, _report: dict | None = None, _index_rows: tuple | None = None) -> dict[str, Any]:
    report = _report or validate_manifest(manifest_path, enforce_schedule_policy=False)
    require_valid(report)
    # Unknown mailbox roles cannot prove delivery or absence. Keep matching
    # unknown-location copies visible so continuation fails closed.
    index_path, raw_rows = _index_rows or list_messages(database=database, states={"sent", "scheduled", "other"}, limit=100000)
    if len(raw_rows) >= 100_000:
        raise CampaignError("Mail index audit reached its 100,000-row safety bound; narrow or extend the audited adapter before scheduling.")
    raw_rows = [row for row in raw_rows if not row.get("deleted")]
    rows = unique_messages(raw_rows)
    by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_subject[row["subject"]].append(row)

    results: list[dict[str, Any]] = []
    result_sources: list[dict[str, Any]] = []
    for item in report["active_messages"]:
        broad_candidates: list[dict[str, Any]] = []
        expected_to = {value.lower() for value in item["to"]}
        expected_cc = {value.lower() for value in item["cc"]}
        expected_bcc = {value.lower() for value in item["bcc"]}
        for row in by_subject.get(item["subject"], []):
            actual_all = {
                value.lower()
                for field in ("to", "cc", "bcc")
                for value in (row.get(field) or [])
            }
            if expected_to & actual_all:
                broad_candidates.append(row)
        seen_ids = {normalize_message_id(row["message_id"]) for row in broad_candidates}
        broad_candidates = [
            next(row for row in broad_candidates if normalize_message_id(row["message_id"]) == message_id)
            for message_id in sorted(seen_ids)
        ]
        candidates = [
            row
            for row in broad_candidates
            if {value.lower() for value in (row.get("to") or [])} == expected_to
            and {value.lower() for value in (row.get("cc") or [])} == expected_cc
            and {value.lower() for value in (row.get("bcc") or [])} == expected_bcc
        ]
        sent = [row for row in candidates if row["state"] == "sent"]
        scheduled = [row for row in candidates if row["state"] == "scheduled"]
        issues: list[str] = []
        chosen = sent[0] if sent else (scheduled[0] if scheduled else (broad_candidates[0] if broad_candidates else None))
        if len(broad_candidates) > 1:
            status = "duplicate"
        elif sent:
            status = "sent"
        elif scheduled:
            expected = datetime.fromisoformat(item["schedule_at"]) if item["schedule_at"] else None
            actual = datetime.fromisoformat(scheduled[0]["scheduled_at"]) if scheduled[0]["scheduled_at"] else None
            if expected and actual and abs((actual - expected).total_seconds()) <= 60:
                status = "scheduled"
            elif expected:
                status = "wrong_time"
                issues.append("wrong_time")
            else:
                status = "scheduled"
        else:
            status = "missing"

        if len(candidates) != len(broad_candidates):
            issues.append("wrong_recipients")
        if any(row["state"] == "other" for row in broad_candidates):
            issues.append("mailbox_unverified")

        results.append(
            {
                "key": item["key"],
                "company": item["company"],
                "to": item["to"],
                "cc": item["cc"],
                "bcc": item["bcc"],
                "subject": item["subject"],
                "expected_schedule": item["schedule_at"],
                "status": status,
                "issues": issues,
                "distinct_message_ids": sorted(seen_ids),
                "matches": broad_candidates,
            }
        )
        result_sources.append({"item": item, "chosen": chosen})

    send_later_ids = {
        normalize_message_id(source["chosen"]["message_id"])
        for source in result_sources
        if source["chosen"] and source["chosen"].get("state") == "scheduled"
    }
    local_report = load_send_later_evidence(
        send_later_ids,
        mail_version_root=mail_version_root(index_path),
        fresh=fresh,
    )
    local_evidence = local_report["evidence"]

    for result, source in zip(results, result_sources):
        item = source["item"]
        chosen = source["chosen"]
        verification = {
            "sender": {"source": "not_applicable", "status": "not_applicable"},
            "attachments": {
                "source": "not_applicable",
                "status": "not_applicable",
                "sha256_checked": False,
                "mismatch_fields": [],
            },
        }
        if chosen:
            if chosen.get("replica_conflicts"):
                result["issues"].append("replica_conflict")
            message_id = normalize_message_id(chosen["message_id"])
            fallback = local_evidence.get(message_id) if chosen.get("state") == "scheduled" else None

            senders = {value.lower() for value in chosen.get("sender_addresses", [])}
            sender_source = "mail_index"
            if fallback and fallback.get("sender_addresses"):
                senders = {value.lower() for value in fallback["sender_addresses"]}
                sender_source = "send_later_emlx"
            if not senders:
                result["issues"].append("sender_unverified")
                verification["sender"] = {"source": "unavailable", "status": "unverified"}
            elif senders != {item["sender"].lower()}:
                result["issues"].append("wrong_sender")
                verification["sender"] = {"source": sender_source, "status": "mismatched"}
            else:
                verification["sender"] = {"source": sender_source, "status": "matched"}

            if fallback:
                expected_signature = attachment_signature(item["attachments"])
                actual_signature = attachment_signature(fallback["attachments"])
                mismatch_fields = attachment_mismatch_fields(item["attachments"], fallback["attachments"])
                if expected_signature != actual_signature:
                    result["issues"].append("wrong_attachment")
                    attachment_status = "mismatched"
                else:
                    attachment_status = "matched"
                verification["attachments"] = {
                    "source": "send_later_emlx",
                    "status": attachment_status,
                    "sha256_checked": True,
                    "mismatch_fields": mismatch_fields,
                }
            else:
                expected_names = Counter(attachment["name"] for attachment in item["attachments"])
                actual_names = Counter(chosen.get("attachments", []))
                actual_count = int(chosen.get("attachment_count") or 0)
                metadata_matches = expected_names == actual_names and len(item["attachments"]) == actual_count
                if chosen.get("state") == "scheduled" and (actual_count == 0 or metadata_matches):
                    result["issues"].append("attachment_unverified")
                    verification["attachments"] = {
                        "source": "unavailable",
                        "status": "unverified",
                        "sha256_checked": False,
                        "mismatch_fields": [],
                    }
                elif expected_names != actual_names or len(item["attachments"]) != actual_count:
                    result["issues"].append("wrong_attachment")
                    mismatch_fields = []
                    if len(item["attachments"]) != actual_count:
                        mismatch_fields.append("count")
                    if expected_names != actual_names:
                        mismatch_fields.append("filename")
                    verification["attachments"] = {
                        "source": "mail_index",
                        "status": "mismatched",
                        "sha256_checked": False,
                        "mismatch_fields": mismatch_fields,
                    }
                elif item["attachments"]:
                    result["issues"].append("attachment_unverified")
                    verification["attachments"] = {
                        "source": "mail_index", "status": "metadata_matched",
                        "sha256_checked": False, "mismatch_fields": [],
                    }
                else:
                    verification["attachments"] = {
                        "source": "mail_index",
                        "status": "metadata_matched",
                        "sha256_checked": False,
                        "mismatch_fields": [],
                    }
        result["verification"] = verification

    status_counts = Counter(item["status"] for item in results)
    issue_counts = Counter(issue for item in results for issue in item["issues"])
    unsafe = any(item["status"] in {"duplicate", "wrong_time"} or item["issues"] for item in results)
    complete = not unsafe and all(item["status"] in {"sent", "scheduled"} for item in results)
    audit = {
        "ok": True,
        "complete": complete,
        "safe_to_resume_missing": not unsafe,
        "campaign_id": report["campaign_id"],
        "mail_index": str(index_path),
        "summary": {
            "active": len(results),
            "statuses": dict(sorted(status_counts.items())),
            "issues": dict(sorted(issue_counts.items())),
            "mail_index_rows_scanned": len(raw_rows),
            "unique_message_ids_scanned": len(rows),
            "replica_rows_ignored": len(raw_rows) - len(rows),
            "send_later_store": local_report["diagnostics"],
        },
        "results": results,
    }
    if csv_path:
        target = Path(csv_path).expanduser().resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["key", "company", "to", "subject", "expected_schedule", "status", "issues", "message_ids"],
                )
                writer.writeheader()
                for item in results:
                    writer.writerow(
                        {
                            "key": item["key"],
                            "company": item["company"],
                            "to": ";".join(item["to"]),
                            "subject": item["subject"],
                            "expected_schedule": item["expected_schedule"] or "",
                            "status": item["status"],
                            "issues": ";".join(item["issues"]),
                            "message_ids": ";".join(item["distinct_message_ids"]),
                        }
                    )
        except OSError as error:
            raise CampaignError(f"Cannot write campaign audit CSV {target}: {error}") from error
        audit["csv_path"] = str(target)
    return audit


def load_queue(path: str) -> tuple[Path, dict[str, Any]]:
    queue_path = Path(path).expanduser().resolve()
    try:
        value = json.loads(queue_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CampaignError(f"Cannot read rendered queue {queue_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CampaignError(f"Rendered queue is invalid JSON: {error.msg}") from error
    if not isinstance(value, dict) or value.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise CampaignError(f"Rendered queue must use {QUEUE_SCHEMA_VERSION}.")
    if not isinstance(value.get("messages"), list):
        raise CampaignError("Rendered queue must contain a messages array.")
    keys = [item.get("key") for item in value["messages"] if isinstance(item, dict)]
    if len(keys) != len(value["messages"]) or any(not isinstance(key, str) or not key for key in keys):
        raise CampaignError("Every rendered queue row must be an object with a non-empty key.")
    if len(set(keys)) != len(keys):
        raise CampaignError("Rendered queue contains duplicate campaign keys.")
    return queue_path, value


def verify_queue(report: dict, queue_path: str) -> tuple[Path, dict]:
    path, queue = load_queue(queue_path)
    if queue.get("campaign_id") != report["campaign_id"] or queue.get("manifest_sha256") != sha256(Path(report["manifest_path"])):
        raise CampaignError("Campaign manifest changed or rendered queue does not match; render a fresh queue.")
    sources = {item["key"]: item for item in report["active_messages"]}
    rows = {item["key"]: item for item in queue["messages"]}
    if set(rows) != set(sources):
        raise CampaignError("Rendered queue keys do not exactly match the manifest.")
    for key, row in rows.items():
        source = sources[key]
        for field in ("sender", "to", "cc", "bcc", "subject", "schedule_at", "attachments", "company"):
            if row.get(field) != source.get(field):
                raise CampaignError(f"Rendered queue {field} differs from approved manifest for '{key}'.")
        if row.get("source_sha256") != source_fingerprint(source):
            raise CampaignError(f"Rendered queue source changed for '{key}'.")
        eml = Path(str(row.get("eml_path") or "")).expanduser().resolve()
        if eml.parent != path.parent or eml.suffix != ".eml" or not eml.is_file() or sha256(eml) != row.get("eml_sha256"):
            raise CampaignError(f"Rendered EML integrity check failed for '{key}'.")
    return path, queue


def next_batch(manifest_path: str, queue_path: str, *, database: str | None = None, limit: int = 20) -> dict[str, Any]:
    report = validate_manifest(manifest_path)
    require_valid(report)
    audit = audit_campaign(manifest_path, database=database)
    if not audit["safe_to_resume_missing"]:
        raise CampaignError("Audit found duplicate, timing, recipient, sender, or attachment verification issues; resolve them before resuming.")
    resolved_queue_path, queue = verify_queue(report, queue_path)
    if queue.get("campaign_id") != audit["campaign_id"]:
        raise CampaignError("Rendered queue campaign_id does not match the manifest.")
    manifest_digest = sha256(Path(report["manifest_path"]))
    if queue.get("manifest_sha256") != manifest_digest:
        raise CampaignError("Campaign manifest changed after rendering; render a fresh queue before scheduling.")
    active_by_key = {item["key"]: item for item in report["active_messages"]}
    queue_by_key = {item["key"]: item for item in queue["messages"]}
    if set(queue_by_key) != set(active_by_key):
        raise CampaignError("Rendered queue keys do not exactly match the active manifest rows.")
    queue_directory = resolved_queue_path.parent.resolve()
    for key, item in queue_by_key.items():
        source = active_by_key[key]
        if item.get("source_sha256") != source_fingerprint(source):
            raise CampaignError(f"Rendered queue row changed or is stale for campaign key '{key}'.")
        eml_path = Path(str(item.get("eml_path") or "")).expanduser().resolve()
        if eml_path.parent != queue_directory or eml_path.suffix.lower() != ".eml":
            raise CampaignError(f"Rendered EML path escapes its queue directory for '{key}'.")
        if not eml_path.is_file():
            raise CampaignError(f"Rendered EML is missing for '{key}': {eml_path}")
        expected_digest = str(item.get("eml_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest) or sha256(eml_path) != expected_digest:
            raise CampaignError(f"Rendered EML integrity check failed for '{key}'.")
    missing_keys = [item["key"] for item in audit["results"] if item["status"] == "missing"]
    missing_queue = []
    for key in missing_keys:
        item = queue_by_key.get(key)
        if not item:
            raise CampaignError(f"Rendered queue is missing campaign key '{key}'.")
        missing_queue.append(item)
    checkpoint_path = resolved_queue_path.parent / "checkpoint.json"
    if checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CampaignError(f"Rendered checkpoint is unreadable: {checkpoint_path}") from error
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("schema_version") != QUEUE_SCHEMA_VERSION
            or checkpoint.get("campaign_id") != audit["campaign_id"]
            or checkpoint.get("manifest_sha256") != manifest_digest
        ):
            raise CampaignError("Rendered checkpoint does not match the campaign manifest.")
        checkpoint["completed_keys"] = sorted(
            item["key"] for item in audit["results"] if item["status"] in {"sent", "scheduled"} and not item["issues"]
        )
        checkpoint["pending_keys"] = missing_keys
        checkpoint["last_audit"] = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "summary": audit["summary"],
        }
        write_private_json_atomic(checkpoint_path, checkpoint)
    batch_limit = max(1, min(int(limit), 100))
    return {
        "ok": True,
        "campaign_id": audit["campaign_id"],
        "queue_path": str(resolved_queue_path),
        "checkpoint_path": str(checkpoint_path) if checkpoint_path.is_file() else None,
        "total_missing": len(missing_queue),
        "batch_size": min(len(missing_queue), batch_limit),
        "batch": missing_queue[:batch_limit],
        "audit_summary": audit["summary"],
    }


def schedule_change_plan(manifest_path: str, *, database: str | None = None, action: str) -> dict[str, Any]:
    audit = audit_campaign(manifest_path, database=database)
    if any(row["status"] == "duplicate" or set(row.get("issues", [])) - {"wrong_time"} for row in audit["results"]):
        raise CampaignError("Schedule change is blocked by recipient, sender, attachment, or replica conflicts.")
    if action == "cancel":
        selected = [item for item in audit["results"] if item["status"] in {"scheduled", "wrong_time"}]
    elif action == "reschedule":
        selected = [item for item in audit["results"] if item["status"] in {"scheduled", "wrong_time"}]
    else:
        raise CampaignError(f"Unsupported schedule plan action: {action}")
    return {
        "ok": True,
        "campaign_id": audit["campaign_id"],
        "action": action,
        "count": len(selected),
        "messages": selected,
        "note": "This is a read-only plan. Perform the change in Mail's visible Send Later UI, then rerun campaign audit.",
    }


def row_datetime(row: dict[str, Any]) -> datetime | None:
    value = row.get("scheduled_at") if row.get("state") == "scheduled" else row.get("date_sent")
    if not value:
        value = row.get("date_received")
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def volume_by_day(rows: list[dict[str, Any]], zone: ZoneInfo | None) -> dict[str, dict[str, int]]:
    output: dict[str, Counter[str]] = defaultdict(Counter)
    for row in unique_messages(rows):
        observed = row_datetime(row)
        if observed is None:
            continue
        day = localize(observed, zone).date().isoformat()
        output[day][str(row.get("state") or "other")] += 1
    return {day: dict(sorted(counts.items())) for day, counts in sorted(output.items())}


def load_triage_summary(path_value: str | None, recipients: set[str]) -> dict[str, Any]:
    if not path_value:
        return {"matched": 0, "classifications": {}}
    path = Path(path_value).expanduser().resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CampaignError(f"Cannot read triage artifact {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CampaignError(f"Triage artifact is invalid JSON: {error.msg}") from error
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise CampaignError("Triage artifact must contain an items array.")
    if value.get("ok") is False or value.get("complete") is False or value.get("errors") or value.get("failed"):
        raise CampaignError("Triage artifact has incomplete classification results; resolve failed messages before using it in a dashboard.")
    counts: Counter[str] = Counter()
    matched = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        sender_address = str(item.get("sender_address") or "").strip().lower()
        if sender_address not in recipients:
            continue
        classification = item.get("classification")
        if not isinstance(classification, dict):
            continue
        counts[str(classification.get("kind") or "unclassified")] += 1
        matched += 1
    return {"matched": matched, "classifications": dict(sorted(counts.items())), "source": str(path)}


def campaign_dashboard(
    manifest_path: str,
    *,
    database: str | None = None,
    triage_path: str | None = None,
) -> dict[str, Any]:
    report = validate_manifest(manifest_path, enforce_schedule_policy=False)
    require_valid(report)
    index_rows = list_messages(database=database, states={"sent", "scheduled", "other"}, limit=100000)
    audit = audit_campaign(manifest_path, database=database, _report=report, _index_rows=index_rows)
    zone = policy_timezone(report["policy"])
    index_path, raw_rows = index_rows
    if len(raw_rows) >= 100_000:
        raise CampaignError("Mail volume dashboard reached its 100,000-row safety bound.")
    recipients = {
        value.lower()
        for item in report["active_messages"]
        for value in (item["to"] + item["cc"] + item["bcc"])
    }
    daily_maximum = int(report["policy"].get("daily_maximum") or 0)
    mail_volume = volume_by_day(raw_rows, zone)
    quota = {}
    for day, counts in mail_volume.items():
        total = sum(counts.values())
        quota[day] = {
            "sent": counts.get("sent", 0),
            "scheduled": counts.get("scheduled", 0),
            "total": total,
            "daily_maximum": daily_maximum or None,
            "remaining": max(0, daily_maximum - total) if daily_maximum else None,
            "over_limit": bool(daily_maximum and total > daily_maximum),
        }
    triage = load_triage_summary(triage_path, recipients)
    return {
        "ok": True,
        "campaign_id": report["campaign_id"],
        "timezone": str(zone) if zone else "timestamp_offsets",
        "mail_index": str(index_path),
        "campaign": {
            "complete": audit["complete"],
            "safe_to_resume_missing": audit["safe_to_resume_missing"],
            "statuses": audit["summary"]["statuses"],
            "issues": audit["summary"]["issues"],
            "planned_by_day": report["summary"]["by_day"],
        },
        "account_volume_by_day": quota,
        "triage": triage,
    }


def parse_boundary(value: str, zone: ZoneInfo | None, *, end_of_day: bool) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise CampaignError("A schedule boundary is required.")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed_date = date.fromisoformat(raw)
            boundary_time = time(23, 59, 59) if end_of_day else time(0, 0)
            if zone is None:
                raise CampaignError("Date-only schedule boundaries require policy.timezone.")
            return datetime.combine(parsed_date, boundary_time, tzinfo=zone)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise CampaignError(f"Invalid schedule boundary: '{value}'.") from error
    if parsed.tzinfo is None:
        if zone is None:
            raise CampaignError("Schedule boundaries must include a UTC offset when policy.timezone is absent.")
        parsed = parsed.replace(tzinfo=zone)
    return localize(parsed, zone)


def identity_for_item(item: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]:
    return (
        tuple(sorted(value.lower() for value in item.get("to", []))),
        tuple(sorted(value.lower() for value in item.get("cc", []))),
        tuple(sorted(value.lower() for value in item.get("bcc", []))),
        str(item.get("subject") or ""),
    )


def candidate_slots(
    start: datetime,
    finish: datetime,
    *,
    zone: ZoneInfo | None,
    allowed_hours: list[str] | None,
    gap: timedelta,
) -> list[datetime]:
    slots: list[datetime] = []
    current_day = localize(start, zone).date()
    final_day = localize(finish, zone).date()
    if allowed_hours:
        try:
            start_time = datetime.strptime(allowed_hours[0], "%H:%M").time()
            end_time = datetime.strptime(allowed_hours[1], "%H:%M").time()
        except (ValueError, TypeError, IndexError) as error:
            raise CampaignError("policy.allowed_hours must be two HH:MM strings.") from error
    else:
        start_time, end_time = time(0, 0), time(23, 59)
    while current_day <= final_day:
        windows = [(start_time, end_time)] if start_time <= end_time else [(time(0, 0), end_time), (start_time, time(23, 59))]
        for window_start, window_end in windows:
            if zone is None:
                tz = start.tzinfo
            else:
                tz = zone
            cursor = datetime.combine(current_day, window_start, tzinfo=tz)
            window_finish = datetime.combine(current_day, window_end, tzinfo=tz)
            if cursor < start:
                elapsed = start - cursor
                steps = max(0, int(elapsed.total_seconds() // gap.total_seconds()))
                cursor += gap * steps
                while cursor < start:
                    cursor += gap
            while cursor <= window_finish and cursor <= finish:
                slots.append(cursor)
                cursor += gap
        current_day += timedelta(days=1)
    return sorted(set(slots))


def rebalance_campaign(
    manifest_path: str,
    output_path: str,
    *,
    start_at: str,
    finish_by: str,
    database: str | None = None,
    daily_maximum: int | None = None,
    minimum_gap_minutes: int | None = None,
    include_account_volume: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    report = validate_manifest(manifest_path, enforce_schedule_policy=False)
    require_valid(report)
    source_path, source = load_manifest(manifest_path)
    result_manifest = copy.deepcopy(source)
    policy = result_manifest.setdefault("policy", {})
    if not isinstance(policy, dict):
        raise CampaignError("policy must be an object.")
    zone = policy_timezone(policy)
    start = parse_boundary(start_at, zone, end_of_day=False)
    finish = parse_boundary(finish_by, zone, end_of_day=True)
    if finish < start:
        raise CampaignError("finish_by must not be earlier than start_at.")
    configured_gap = minimum_gap_minutes if minimum_gap_minutes is not None else int(policy.get("minimum_gap_minutes") or 0)
    if configured_gap < 1 or configured_gap > 1440:
        raise CampaignError("minimum_gap_minutes must be between 1 and 1440 for rebalancing.")
    configured_daily = daily_maximum if daily_maximum is not None else int(policy.get("daily_maximum") or 0)
    if configured_daily < 1 or configured_daily > 100000:
        raise CampaignError("daily_maximum must be between 1 and 100000 for rebalancing.")
    policy["minimum_gap_minutes"] = configured_gap
    policy["daily_maximum"] = configured_daily
    gap = timedelta(minutes=configured_gap)

    raw_rows: list[dict[str, Any]] = []
    index_path: Path | None = None
    if include_account_volume:
        index_path, raw_rows = list_messages(database=database, states={"sent", "scheduled"}, limit=100000)
        if len(raw_rows) >= 100_000:
            raise CampaignError("Rebalance reached its 100,000-row account-volume safety bound.")
        raw_rows = unique_messages(raw_rows)
    campaign_identities = {identity_for_item(item) for item in report["active_messages"]}
    external_rows = [row for row in raw_rows if identity_for_item(row) not in campaign_identities]
    occupied_seconds: list[float] = []
    day_counts: Counter[str] = Counter()
    for row in external_rows:
        observed = row_datetime(row)
        if observed is None:
            continue
        bisect.insort(occupied_seconds, observed.timestamp())
        day_counts[localize(observed, zone).date().isoformat()] += 1

    audit_results: dict[str, dict[str, Any]] = {}
    if include_account_volume:
        audit = audit_campaign(manifest_path, database=database)
        audit_results = {item["key"]: item for item in audit["results"]}
        blocking = [
            item
            for item in audit["results"]
            if set(item["issues"]) - {"wrong_time"} or item["status"] == "duplicate"
        ]
        if blocking:
            raise CampaignError("Campaign has duplicate, sender, recipient, or attachment issues; resolve them before rebalancing.")

    def gap_available(value: datetime) -> bool:
        stamp = value.timestamp()
        position = bisect.bisect_left(occupied_seconds, stamp)
        neighbors = occupied_seconds[max(0, position - 1) : position + 1]
        return all(abs(stamp - other) >= gap.total_seconds() for other in neighbors)

    def occupy(value: datetime) -> None:
        bisect.insort(occupied_seconds, value.timestamp())
        day_counts[localize(value, zone).date().isoformat()] += 1

    active_by_key = {item["key"]: item for item in report["active_messages"]}
    locked_keys: set[str] = set()
    for key, item in audit_results.items():
        if item["status"] != "sent":
            continue
        sent_matches = [row for row in item.get("matches", []) if row.get("state") == "sent"]
        observed = row_datetime(sent_matches[0]) if sent_matches else None
        if observed is not None:
            occupy(observed)
        locked_keys.add(key)

    preserved: dict[str, datetime] = {}
    to_move: list[dict[str, Any]] = []
    for item in sorted(
        (value for key, value in active_by_key.items() if key not in locked_keys),
        key=lambda value: (value.get("schedule_at") or "", value["index"]),
    ):
        original = datetime.fromisoformat(item["schedule_at"]) if item.get("schedule_at") else None
        if original is not None:
            original = localize(original, zone)
        day = localize(original, zone).date().isoformat() if original else ""
        can_preserve = bool(
            original
            and start <= original <= finish
            and in_allowed_hours(original, policy.get("allowed_hours"), zone)
            and day_counts[day] < configured_daily
            and gap_available(original)
        )
        if can_preserve and original is not None:
            preserved[item["key"]] = original
            occupy(original)
        else:
            to_move.append(item)

    available: list[datetime] = []
    for slot in candidate_slots(
        start,
        finish,
        zone=zone,
        allowed_hours=policy.get("allowed_hours"),
        gap=gap,
    ):
        day = localize(slot, zone).date().isoformat()
        if day_counts[day] >= configured_daily or not gap_available(slot):
            continue
        available.append(slot)
        occupy(slot)
        if len(available) == len(to_move):
            break
    if len(available) < len(to_move):
        raise CampaignError(
            f"No safe schedule fits {len(to_move)} messages between {start.isoformat()} and {finish.isoformat()} "
            f"with a {configured_gap}-minute gap and daily maximum {configured_daily}."
        )
    assigned = {item["key"]: slot for item, slot in zip(to_move, available)}
    assigned.update(preserved)
    changes: list[dict[str, Any]] = []
    for raw in result_manifest.get("messages", []):
        if not isinstance(raw, dict) or raw.get("key") not in assigned:
            continue
        old = raw.get("schedule_at")
        new = assigned[str(raw["key"])].isoformat()
        raw["schedule_at"] = new
        changes.append(
            {
                "key": raw["key"],
                "company": str(raw.get("company") or ""),
                "old_schedule": old,
                "new_schedule": new,
                "moved": old != new,
            }
        )
    output = Path(output_path).expanduser().resolve()
    if output == source_path:
        raise CampaignError("Rebalance output must be a new manifest path.")
    write_private_json_atomic(output, result_manifest, create_only=not force)
    verification = validate_manifest(str(output), require_schedule=True)
    if not verification["ok"]:
        output.unlink(missing_ok=True)
        raise CampaignError("Generated rebalance manifest failed validation.")
    return {
        "ok": True,
        "campaign_id": report["campaign_id"],
        "output": str(output),
        "source_manifest_sha256": sha256(source_path),
        "output_manifest_sha256": sha256(output),
        "timezone": str(zone) if zone else "timestamp_offsets",
        "start_at": start.isoformat(),
        "finish_by": finish.isoformat(),
        "minimum_gap_minutes": configured_gap,
        "daily_maximum": configured_daily,
        "account_volume_included": include_account_volume,
        "mail_index": str(index_path) if index_path else None,
        "locked_sent": len(locked_keys),
        "preserved": len(preserved),
        "moved": sum(1 for item in changes if item["moved"]),
        "changes": changes,
        "by_day": verification["summary"]["by_day"],
    }


def load_session(path_value: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_value).expanduser().resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CampaignError(f"Cannot read schedule session {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CampaignError(f"Schedule session is invalid JSON: {error.msg}") from error
    if not isinstance(value, dict) or value.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise CampaignError(f"Schedule session must use {SESSION_SCHEMA_VERSION}.")
    if not isinstance(value.get("operations"), list):
        raise CampaignError("Schedule session must contain an operations array.")
    return path, value


def start_schedule_session(
    manifest_path: str,
    output_path: str,
    *,
    action: str,
    queue_path: str | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    if action not in {"schedule", "reschedule", "cancel"}:
        raise CampaignError("Session action must be schedule, reschedule, or cancel.")
    report = validate_manifest(manifest_path, enforce_schedule_policy=False)
    require_valid(report)
    operations: list[dict[str, Any]] = []
    resolved_queue: str | None = None
    if action == "schedule":
        if not queue_path:
            raise CampaignError("A rendered queue is required for a schedule session.")
        audit = audit_campaign(manifest_path, database=database)
        if not audit["safe_to_resume_missing"]:
            raise CampaignError("Campaign audit is unsafe; resolve issues before starting a schedule session.")
        queue_file, queue = verify_queue(report, queue_path)
        if queue.get("campaign_id") != report["campaign_id"] or queue.get("manifest_sha256") != sha256(Path(report["manifest_path"])):
            raise CampaignError("Rendered queue does not match the campaign manifest.")
        queue_by_key = {item["key"]: item for item in queue["messages"]}
        active_by_key = {item["key"]: item for item in report["active_messages"]}
        if set(queue_by_key) != set(active_by_key):
            raise CampaignError("Rendered queue keys do not exactly match the active manifest rows.")
        for key, row in queue_by_key.items():
            source = active_by_key[key]
            if row.get("source_sha256") != source_fingerprint(source):
                raise CampaignError(f"Rendered queue row changed or is stale for campaign key '{key}'.")
            eml = Path(str(row.get("eml_path") or "")).expanduser().resolve()
            expected_digest = str(row.get("eml_sha256") or "")
            if (
                eml.parent != queue_file.parent
                or eml.suffix.lower() != ".eml"
                or not eml.is_file()
                or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
                or sha256(eml) != expected_digest
            ):
                raise CampaignError(f"Rendered EML integrity check failed for '{key}'.")
        for result in audit["results"]:
            if result["status"] != "missing":
                continue
            row = queue_by_key.get(result["key"])
            if not row:
                raise CampaignError(f"Rendered queue is missing campaign key '{result['key']}'.")
            eml = Path(str(row.get("eml_path") or "")).expanduser().resolve()
            operations.append(
                {
                    "key": result["key"],
                    "company": row.get("company"),
                    "subject": row.get("subject"),
                    "target_schedule": row.get("schedule_at"),
                    "eml_path": str(eml),
                    "status": "pending",
                    "attempts": 0,
                }
            )
        resolved_queue = str(queue_file)
    else:
        plan = schedule_change_plan(manifest_path, database=database, action=action)
        for item in plan["messages"]:
            operations.append(
                {
                    "key": item["key"],
                    "company": item.get("company"),
                    "subject": item.get("subject"),
                    "current_schedule": (item.get("matches") or [{}])[0].get("scheduled_at"),
                    "message_ids": item.get("distinct_message_ids", []),
                    "target_schedule": item.get("expected_schedule") if action == "reschedule" else None,
                    "status": "pending",
                    "attempts": 0,
                }
            )
    now = datetime.now(timezone.utc).isoformat()
    session = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "campaign_id": report["campaign_id"],
        "action": action,
        "manifest_path": report["manifest_path"],
        "manifest_sha256": sha256(Path(report["manifest_path"])),
        "queue_path": resolved_queue,
        "created_at": now,
        "updated_at": now,
        "operations": operations,
        "events": [],
    }
    output = Path(output_path).expanduser().resolve()
    write_private_json_atomic(output, session, create_only=True)
    return {"ok": True, "session": str(output), **session_status(str(output))}


def _reconcile_schedule_session(path_value: str, *, database: str | None = None) -> dict[str, Any]:
    path, session = load_session(path_value)
    manifest = Path(str(session.get("manifest_path") or "")).expanduser().resolve()
    if not manifest.is_file() or sha256(manifest) != session.get("manifest_sha256"):
        raise CampaignError("Schedule session manifest changed or is missing.")
    report = validate_manifest(str(manifest), enforce_schedule_policy=False)
    require_valid(report)
    keys = [op.get("key") for op in session["operations"]]
    sources = {item["key"]: item for item in report["active_messages"]}
    if len(keys) != len(set(keys)) or set(keys) - set(sources):
        raise CampaignError("Session keys differ from the approved manifest.")
    if session["action"] == "schedule":
        _, queue = verify_queue(report, str(session.get("queue_path") or ""))
        emls = {row["key"]: row["eml_path"] for row in queue["messages"]}
        for op in session["operations"]:
            if op.get("eml_path") != emls[op["key"]]:
                raise CampaignError("Session EML path changed after approval.")
    audit = audit_campaign(str(manifest), database=database)
    by_key = {item["key"]: item for item in audit["results"]}
    changed = 0
    for operation in session["operations"]:
        result = by_key.get(operation.get("key"))
        if not result:
            continue
        previous = operation.get("status")
        pinned = operation.get("message_ids")
        observed = result.get("distinct_message_ids", [])
        if pinned and observed and set(pinned) != set(observed):
            result["issues"].append("message_identity_changed")
            operation["status"] = "verification_failed"
            operation["verification_issues"] = ["message_identity_changed"]
            changed += previous != "verification_failed"
            continue
        if session["action"] in {"schedule", "reschedule"}:
            if result["status"] in {"scheduled", "sent"} and not result["issues"]:
                operation["status"] = "verified"
                operation["verified_state"] = result["status"]
            elif previous in {"applied_unverified", "verified"} and (result["status"] in {"wrong_time", "duplicate"} or result["issues"] or previous == "verified"):
                operation["status"] = "verification_failed"
                operation["verification_issues"] = result["issues"] or [result["status"]]
        elif session["action"] == "cancel" and previous == "applied_unverified":
            if result["status"] == "missing" and not result["issues"]:
                operation["status"] = "verified"
                operation["verified_state"] = "cancelled"
            elif result["status"] == "sent":
                operation["status"] = "verification_failed"
                operation["verification_issues"] = ["sent_before_cancel"]
        if operation.get("status") != previous:
            changed += 1
        if operation.get("status") == "verified" and operation.get("claim_token"):
            operation_state.release(f"{session['campaign_id']}:{operation['key']}", operation["claim_token"])
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    session["last_audit"] = audit["summary"]
    write_private_json_atomic(path, session)
    return {"changed": changed, "audit": audit}


def reconcile_schedule_session(path_value: str, *, database: str | None = None) -> dict[str, Any]:
    with operation_state.locked(Path(path_value).expanduser().resolve()):
        return _reconcile_schedule_session(path_value, database=database)


def record_schedule_session(
    path_value: str,
    *,
    key: str,
    result: str,
    note: str | None = None,
    claim_token: str | None = None,
) -> dict[str, Any]:
    with operation_state.locked(Path(path_value).expanduser().resolve()):
        return _record_schedule_session(path_value, key=key, result=result, note=note, claim_token=claim_token)


def _record_schedule_session(path_value: str, *, key: str, result: str, note: str | None, claim_token: str | None) -> dict[str, Any]:
    if result not in {"applied", "failed", "skipped"}:
        raise CampaignError("Session result must be applied, failed, or skipped.")
    path, session = load_session(path_value)
    matches = [item for item in session["operations"] if item.get("key") == key]
    if len(matches) != 1:
        raise CampaignError(f"Schedule session does not contain exactly one operation for '{key}'.")
    operation = matches[0]
    broker = operation_state.inspect_claim(f"{session['campaign_id']}:{key}")
    # Recover an interrupted claim write, without reissuing a UI operation.
    if (operation.get("status") in {"pending", "failed"} and broker and broker.get("token") == claim_token
            and broker.get("session") == str(path)):
        operation.update(status="claimed", claim_token=claim_token, owner=broker["owner"])
    if not claim_token or operation.get("claim_token") != claim_token or operation.get("status") not in {"claimed", "applied_unverified"}:
        raise CampaignError("Recording requires the claim_token returned by session-next for this operation.")
    operation["attempts"] = int(operation.get("attempts") or 0) + 1
    operation["status"] = "applied_unverified" if result == "applied" else result
    operation["last_note"] = (note or "")[:1000] or None
    observed = datetime.now(timezone.utc).isoformat()
    session["events"].append({"key": key, "result": result, "observed_at": observed, "note": operation["last_note"]})
    session["updated_at"] = observed
    write_private_json_atomic(path, session)
    if result in {"failed", "skipped"}:
        operation_state.release(f"{session['campaign_id']}:{key}", claim_token)
    return {"ok": True, "session": str(path), "operation": operation}


def session_status(path_value: str) -> dict[str, Any]:
    path, session = load_session(path_value)
    counts = Counter(str(item.get("status") or "unknown") for item in session["operations"])
    terminal = {"verified", "skipped"}
    complete = all(str(item.get("status")) in terminal for item in session["operations"])
    return {
        "ok": True,
        "session": str(path),
        "campaign_id": session["campaign_id"],
        "action": session["action"],
        "total": len(session["operations"]),
        "statuses": dict(sorted(counts.items())),
        "complete": complete,
        "updated_at": session.get("updated_at"),
        "last_audit": session.get("last_audit"),
        "operations": session["operations"],
        "active_claims": {item["key"]: claim for item in session["operations"]
                          if (claim := operation_state.inspect_claim(f"{session['campaign_id']}:{item['key']}"))},
    }


def next_session_operations(
    path_value: str,
    *,
    database: str | None = None,
    limit: int = 20,
    owner: str = "mailctl",
) -> dict[str, Any]:
    with operation_state.locked(Path(path_value).expanduser().resolve()):
        reconciled = _reconcile_schedule_session(path_value, database=database)
        path, session = load_session(path_value)
        audit = reconciled["audit"]
        allowed = {"wrong_time"} if session["action"] in {"reschedule", "cancel"} else set()
        if any(row["status"] == "duplicate" or set(row.get("issues", [])) - allowed for row in audit["results"]):
            raise CampaignError("Latest Mail evidence blocks this session; inspect campaign explain before continuing.")
        if any(op["status"] == "verification_failed" for op in session["operations"]):
            raise CampaignError("A previous operation failed verification; reconcile the discrepancy before continuing.")
        sources = {item["key"]: item for item in validate_manifest(session["manifest_path"], enforce_schedule_policy=False)["active_messages"]}
        results = {row["key"]: row for row in audit["results"]}
        actionable = [op for op in session["operations"] if op["status"] in {"pending", "failed"}]
        batch = []
        blocked = []
        for op in actionable:
            expected_states = {"missing"} if session["action"] == "schedule" else {"scheduled", "wrong_time"}
            if results[op["key"]]["status"] not in expected_states:
                continue
            token = str(uuid.uuid4())
            source = sources[op["key"]]
            # Intentionally exclude campaign name, row key, body and time: these
            # can differ between two agents trying to operate on the same mail.
            identity = {key: sorted(value.lower() for value in source[key]) for key in ("to", "cc", "bcc")}
            identity.update(sender=source["sender"].lower(), subject=source["subject"])
            resources = [f"{session['campaign_id']}:{op['key']}", "mail-intent:" + operation_state.digest(identity)]
            message_ids = results[op["key"]].get("distinct_message_ids", [])
            resources.extend("mail-message:" + normalize_message_id(value) for value in message_ids)
            if not operation_state.claim_many(resources, owner[:120], token, str(path)):
                conflicts = [claim for resource in resources if (claim := operation_state.inspect_claim(resource))]
                blocked.append({"key": op["key"], "reason": "shared_mail_identity_claimed",
                                "owners": sorted({claim["owner"] for claim in conflicts}),
                                "sessions": sorted({claim["session"] for claim in conflicts})})
                continue
            op.update(status="claimed", claim_token=token, owner=owner[:120], claimed_at=operation_state.now(),
                      claim_resources=resources, message_ids=message_ids)
            batch.append({**op, **{k: source[k] for k in ("to", "cc", "bcc", "sender", "subject", "attachments")},
                          "target_schedule": source["schedule_at"] if session["action"] != "cancel" else None,
                          "message_ids": results[op["key"]].get("distinct_message_ids", [])})
            if len(batch) >= max(1, min(int(limit), 100)):
                break
        write_private_json_atomic(path, session)
        return {**session_status(str(path)), "remaining_actionable": len(actionable), "batch_size": len(batch), "batch": batch, "blocked_claims": blocked,
                "instruction": "Use each claim_token with session-record. Claims never expire into automatic retries; audit uncertain operations first."}


def campaign_diff(before: str, after: str) -> dict:
    reports = [validate_manifest(value, enforce_schedule_policy=False) for value in (before, after)]
    for report in reports:
        require_valid(report)
    old, new = ({row["key"]: row for row in report["active_messages"]} for report in reports)
    changes = []
    for key in sorted(set(old) | set(new)):
        fields = {field: {"before": old.get(key, {}).get(field), "after": new.get(key, {}).get(field)}
                  for field in ("sender", "to", "cc", "bcc", "subject", "body", "schedule_at", "attachments")
                  if old.get(key, {}).get(field) != new.get(key, {}).get(field)}
        if fields:
            changes.append({"key": key, "change": "added" if key not in old else "removed" if key not in new else "updated", "fields": fields})
    return {"ok": True, "changes": changes, "changed": len(changes), "before": reports[0]["summary"], "after": reports[1]["summary"]}
