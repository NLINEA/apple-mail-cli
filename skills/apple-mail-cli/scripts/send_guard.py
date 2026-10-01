"""Deterministic outbound intent and private one-attempt journal. No Mail calls."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

INTENT_SCHEMA = "mailctl-send-intent/v1"
ATTEMPT_SCHEMA = "mailctl-send-attempt/v1"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS = 20


class SendGuardError(RuntimeError):
    def __init__(self, message: str, code: str = "send_guard_error") -> None:
        super().__init__(message)
        self.code = code


def canonical_intent(payload: dict, evidence: list[dict]) -> dict:
    if len(evidence) > MAX_ATTACHMENTS or sum(item["size_bytes"] for item in evidence) > MAX_ATTACHMENT_BYTES:
        raise SendGuardError("Direct send permits at most 20 attachments totaling 25 MiB.", "send_attachment_limit")
    return {"schema": INTENT_SCHEMA, "sender": payload["sender"],
            "to": payload["to"], "cc": payload["cc"], "bcc": payload["bcc"],
            "subject": payload["subject"], "body": payload["body"],
            "attachments": [{key: item[key] for key in ("name", "size_bytes", "sha256")} for item in evidence]}


def intent_digest(intent: dict) -> str:
    try:
        encoded = json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except UnicodeError as error:
        raise SendGuardError("Outbound intent must contain valid UTF-8 text.", "send_intent_invalid") from error
    return hashlib.sha256(encoded).hexdigest()


def state_root() -> Path:
    return Path.home() / "Library/Application Support/apple-mail-cli/send-attempts/v1"


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SendGuardError("Send state directory must be owned by this user with mode 0700, without symlinks.", "send_state_unsafe")


def ensure_state_root(root: Path) -> None:
    missing = []
    current = root
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        sync_directory(path.parent)
    private_directory(root)


def write_record(path: Path, record: dict) -> None:
    temporary = path.parent / (".record-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class SendAttempt:
    def __init__(self, directory: Path, record: dict, snapshots: list[str]) -> None:
        self.directory = directory
        self.record = record
        self.snapshots = snapshots

    def finish(self, state: str) -> None:
        self.record["state"] = state
        self.record["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_record(self.directory / "attempt.json", self.record)


def reserve_attempt(digest: str, evidence: list[dict]) -> SendAttempt:
    if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
        raise SendGuardError("Invalid intent digest.", "send_intent_invalid")
    root = state_root()
    ensure_state_root(root)
    directory = root / digest
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SendGuardError("This intent already has a send-attempt reservation. Inspect Mail and its private attempt record; do not retry or remove the guard automatically.", "send_attempt_exists") from error
    record = {"schema": ATTEMPT_SCHEMA, "digest": digest, "state": "preparing",
              "created_at": datetime.now(timezone.utc).isoformat(), "delivery_confirmed": False}
    attempt = SendAttempt(directory, record, [])
    try:
        sync_directory(root)
        attempt.finish("preparing")
        total = 0
        for index, item in enumerate(evidence):
            name = item["name"]
            if not name or Path(name).name != name or name in {".", ".."}:
                raise SendGuardError("Invalid attachment filename.", "send_intent_invalid")
            target = directory / "attachments" / str(index) / name
            target.parent.mkdir(mode=0o700, parents=True)
            target.parent.parent.chmod(0o700)
            copied = 0
            checksum = hashlib.sha256()
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(descriptor, "wb") as output:
                source_fd = os.open(item["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(source_fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise SendGuardError("Attachment must remain a regular file.", "send_attachment_changed")
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        copied += len(chunk)
                        total += len(chunk)
                        if copied > item["size_bytes"] or total > MAX_ATTACHMENT_BYTES:
                            raise SendGuardError("Attachment changed after approval.", "send_attachment_changed")
                        checksum.update(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if copied != item["size_bytes"] or checksum.hexdigest() != item["sha256"]:
                raise SendGuardError("Attachment changed after approval.", "send_attachment_changed")
            sync_directory(target.parent)
            attempt.snapshots.append(str(target))
        if evidence:
            sync_directory(directory / "attachments")
        # Durable before automation. Any interruption leaves this intent reserved.
        attempt.finish("in_flight")
        return attempt
    except Exception:
        try:
            attempt.finish("not_submitted")
        except Exception:
            pass
        raise
