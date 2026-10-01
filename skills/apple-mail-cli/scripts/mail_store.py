"""Read-only evidence extraction from Apple Mail's local message store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from email import policy
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable


MAX_SEND_LATER_FILES = 100_000
MAX_HEADER_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 256 * 1024 * 1024
MAX_CACHE_BYTES = 64 * 1024 * 1024
CACHE_SCHEMA_VERSION = "apple-mail-cli-send-later-evidence/v1"


def normalize_message_id(value: Any) -> str:
    return str(value or "").strip().lower()


def empty_report(requested: set[str], *, reason: str | None = None) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "requested": len(requested),
        "files_scanned": 0,
        "message_ids_found": 0,
        "evidence_loaded": 0,
        "failures": 0,
        "cache_enabled": False,
        "cache_hits": 0,
        "cache_misses": 0,
        "evidence_cache_hits": 0,
        "cache_written": False,
    }
    if reason:
        diagnostics["unavailable_reason"] = reason
    return {"evidence": {}, "diagnostics": diagnostics}


def send_later_files(mail_version_root: Path) -> list[Path]:
    try:
        root = mail_version_root.expanduser().resolve(strict=True)
    except OSError:
        return []
    if not root.is_dir() or not root.name.startswith("V"):
        return []
    output: list[Path] = []
    for candidate in root.glob("*/SendLater.mbox/**/Messages/*.emlx"):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            output.append(resolved)
            if len(output) > MAX_SEND_LATER_FILES:
                raise RuntimeError("Send Later store exceeds the 100,000-file safety bound.")
    return sorted(output)


def default_cache_path(mail_version_root: Path) -> Path | None:
    try:
        root = mail_version_root.expanduser().resolve(strict=True)
        mail_root = (Path.home() / "Library" / "Mail").resolve(strict=True)
    except OSError:
        return None
    if not root.is_relative_to(mail_root) or not root.name.startswith("V"):
        return None
    return Path.home() / "Library" / "Caches" / "apple-mail-cli" / "send-later-evidence-v1.json"


def path_token(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def file_fingerprint(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def validated_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("source") != "send_later_emlx":
        return None
    senders = value.get("sender_addresses")
    attachments = value.get("attachments")
    if not isinstance(senders, list) or not all(isinstance(item, str) for item in senders):
        return None
    if not isinstance(attachments, list) or len(attachments) > 1000:
        return None
    normalized_attachments: list[dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            not isinstance(name, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            return None
        normalized_attachments.append({"name": name, "size_bytes": size, "sha256": digest})
    if value.get("attachment_count") != len(normalized_attachments):
        return None
    return {
        "source": "send_later_emlx",
        "sender_addresses": sorted(set(senders)),
        "attachments": sorted(
            normalized_attachments,
            key=lambda item: (item["name"], item["size_bytes"], item["sha256"]),
        ),
        "attachment_count": len(normalized_attachments),
    }


def load_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    try:
        cache_stat = path.stat()
        if path.is_symlink() or not path.is_file() or cache_stat.st_size > MAX_CACHE_BYTES or cache_stat.st_mode & 0o077:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = value.get("entries") if isinstance(value, dict) and value.get("schema_version") == CACHE_SCHEMA_VERSION else None
    if not isinstance(entries, dict) or len(entries) > MAX_SEND_LATER_FILES:
        return {}
    return {str(key): item for key, item in entries.items() if isinstance(item, dict)}


def write_cache(path: Path | None, entries: dict[str, dict[str, Any]]) -> bool:
    if path is None:
        return False
    payload = (json.dumps({"schema_version": CACHE_SCHEMA_VERSION, "entries": entries}, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_CACHE_BYTES:
        return False
    parent = path.expanduser().parent
    try:
        if parent.exists() and parent.is_symlink():
            return False
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            return False
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    except OSError:
        return False
    return True


def cached_entry(value: Any, fingerprint: dict[str, int]) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("fingerprint") != fingerprint:
        return None
    message_id = normalize_message_id(value.get("message_id"))
    message_size = value.get("message_size")
    if not isinstance(message_size, int) or isinstance(message_size, bool) or not 0 <= message_size <= MAX_MESSAGE_BYTES:
        return None
    output: dict[str, Any] = {
        "fingerprint": fingerprint,
        "message_id": message_id,
        "message_size": message_size,
    }
    evidence = validated_evidence(value.get("evidence"))
    if evidence is not None:
        output["evidence"] = evidence
    return output


def read_emlx_headers(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        first_line = handle.readline()
        try:
            message_size = int(first_line.strip())
        except ValueError as error:
            raise ValueError("Invalid EMLX byte-count line.") from error
        if message_size < 0 or message_size > MAX_MESSAGE_BYTES:
            raise ValueError("EMLX message exceeds the 256 MiB evidence limit.")
        remaining = message_size
        chunks: list[bytes] = []
        total = 0
        while remaining > 0:
            line = handle.readline(min(remaining, MAX_HEADER_BYTES - total + 1))
            if not line:
                break
            chunks.append(line)
            total += len(line)
            remaining -= len(line)
            if total > MAX_HEADER_BYTES:
                raise ValueError("EMLX headers exceed the 1 MiB evidence limit.")
            if line in {b"\n", b"\r\n"}:
                break
    headers = BytesHeaderParser(policy=policy.default).parsebytes(b"".join(chunks))
    return normalize_message_id(headers.get("Message-ID")), message_size


def read_emlx_message(path: Path, expected_size: int) -> bytes:
    with path.open("rb") as handle:
        first_line = handle.readline()
        try:
            observed_size = int(first_line.strip())
        except ValueError as error:
            raise ValueError("Invalid EMLX byte-count line.") from error
        if observed_size != expected_size:
            raise ValueError("EMLX message changed during verification.")
        if observed_size < 0 or observed_size > MAX_MESSAGE_BYTES:
            raise ValueError("EMLX message exceeds the 256 MiB evidence limit.")
        message_bytes = handle.read(observed_size)
    if len(message_bytes) != observed_size:
        raise ValueError("EMLX message is incomplete.")
    return message_bytes


def safe_filename(value: Any) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def parse_evidence(message_bytes: bytes) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(message_bytes)
    sender_addresses = sorted(
        {
            address.strip().lower()
            for _, address in getaddresses(message.get_all("From", []))
            if address.strip()
        }
    )
    attachments: list[dict[str, Any]] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename and part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            raise ValueError("An attachment payload could not be decoded.")
        attachments.append(
            {
                "name": safe_filename(filename),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    attachments.sort(key=lambda item: (item["name"], item["size_bytes"], item["sha256"]))
    return {
        "source": "send_later_emlx",
        "sender_addresses": sender_addresses,
        "attachments": attachments,
        "attachment_count": len(attachments),
    }


def load_send_later_evidence(
    message_ids: Iterable[str],
    *,
    mail_version_root: Path | None,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    requested = {normalize_message_id(value) for value in message_ids if normalize_message_id(value)}
    if not requested:
        return empty_report(requested)
    if mail_version_root is None:
        return empty_report(requested, reason="mail_store_path_unavailable")
    try:
        files = send_later_files(mail_version_root)
    except RuntimeError as error:
        return empty_report(requested, reason=str(error))
    if not files:
        return empty_report(requested, reason="send_later_store_unavailable")

    resolved_cache_path = cache_path or default_cache_path(mail_version_root)
    previous_entries = load_cache(resolved_cache_path)
    current_entries: dict[str, dict[str, Any]] = {}
    matches: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    failures = 0
    cache_hits = 0
    cache_misses = 0
    for path in files:
        try:
            fingerprint = file_fingerprint(path)
        except (OSError, ValueError):
            failures += 1
            continue
        token = path_token(path)
        entry = cached_entry(previous_entries.get(token), fingerprint)
        if entry is None:
            cache_misses += 1
            try:
                message_id, message_size = read_emlx_headers(path)
            except (OSError, ValueError):
                failures += 1
                continue
            entry = {
                "fingerprint": fingerprint,
                "message_id": message_id,
                "message_size": message_size,
            }
        else:
            cache_hits += 1
            message_id = entry["message_id"]
        current_entries[token] = entry
        if message_id in requested:
            matches.setdefault(message_id, []).append((path, entry))

    evidence: dict[str, dict[str, Any]] = {}
    ambiguous = 0
    evidence_cache_hits = 0
    for message_id, local_matches in matches.items():
        if len(local_matches) != 1:
            ambiguous += 1
            continue
        path, entry = local_matches[0]
        cached = validated_evidence(entry.get("evidence"))
        if cached is not None:
            evidence[message_id] = cached
            evidence_cache_hits += 1
            continue
        try:
            parsed = parse_evidence(read_emlx_message(path, entry["message_size"]))
            if file_fingerprint(path) != entry["fingerprint"]:
                raise ValueError("EMLX message changed during verification.")
            evidence[message_id] = parsed
            entry["evidence"] = parsed
        except (OSError, ValueError):
            failures += 1

    cache_changed = current_entries != previous_entries
    cache_written = write_cache(resolved_cache_path, current_entries) if cache_changed else False

    return {
        "evidence": evidence,
        "diagnostics": {
            "requested": len(requested),
            "files_scanned": len(files),
            "message_ids_found": len(matches),
            "evidence_loaded": len(evidence),
            "ambiguous_message_ids": ambiguous,
            "failures": failures,
            "cache_enabled": resolved_cache_path is not None,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "evidence_cache_hits": evidence_cache_hits,
            "cache_entries": len(current_entries),
            "cache_pruned": max(0, len(previous_entries) - len(current_entries)),
            "cache_written": cache_written,
        },
    }
