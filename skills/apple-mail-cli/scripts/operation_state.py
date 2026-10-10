"""Private local operation receipts and cross-process ownership; never Mail storage."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def root() -> Path:
    path = Path(os.environ.get("MAILCTL_STATE_DIR") or Path.home() / "Library/Application Support/apple-mail-cli").expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def locked(path: Path):
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(path: Path):
    if path.is_symlink():
        raise ValueError("Private state must not be a symbolic link.")
    if not path.exists():
        return None
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Private state exceeds 16 MiB.")
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".mailctl-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def claim(resource: str, owner: str, token: str, session: str) -> bool:
    return claim_many([resource], owner, token, session)


def claim_many(resources: list[str], owner: str, token: str, session: str) -> bool:
    """Lock every identity in a fixed order, including the legacy campaign key.

    A crash during publication intentionally leaves blocking orphan claims. Never
    expire or roll them back into automatic retries after an uncertain outcome.
    """
    resources = list(dict.fromkeys(resources))
    if not resources:
        raise ValueError("At least one ownership resource is required.")
    paths = sorted(root() / "claims" / (digest(resource) + ".json") for resource in resources)
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(locked(path))
        if any(read(path) is not None for path in paths):
            return False
        value = {"owner": owner, "token": token, "session": session, "created_at": now(), "resources": resources}
        # Publish the legacy session key first, so session-status can recover a
        # partial publication after a crash. Locks still use canonical order.
        for resource in resources:
            write(root() / "claims" / (digest(resource) + ".json"), value)
    return True


def release(resource: str, token: str) -> None:
    path = root() / "claims" / (digest(resource) + ".json")
    value = read(path)
    if not value or value.get("token") != token:
        return
    resources = value.get("resources") or [resource]
    paths = sorted({root() / "claims" / (digest(item) + ".json") for item in [resource, *resources]})
    with ExitStack() as locks:
        for item in paths:
            locks.enter_context(locked(item))
        for item in paths:
            current = read(item)
            if current and current.get("token") == token:
                item.unlink()


def inspect_claim(resource: str) -> dict | None:
    path = root() / "claims" / (digest(resource) + ".json")
    with locked(path):
        return read(path)


def receipt_path(operation_id: str) -> Path:
    return root() / "receipts" / (str(uuid.UUID(operation_id)) + ".json")


def begin(operation_id: str, command: str, request: dict, *, retain_request: bool = False) -> dict:
    path = receipt_path(operation_id)
    with locked(path):
        existing = read(path)
        fingerprint = digest(request)
        if existing:
            if existing["command"] != command or existing["request_sha256"] != fingerprint:
                raise ValueError("Operation ID already belongs to a different request.")
            return existing
        value = {"operation_id": operation_id, "command": command, "request_sha256": fingerprint,
                 "state": "prepared", "created_at": now(), "updated_at": now()}
        if retain_request:
            value["request"] = request
        write(path, value)
        return value


def transition(operation_id: str, state: str, result=None, *, expected: str | None = None) -> dict:
    path = receipt_path(operation_id)
    with locked(path):
        value = read(path)
        if not value:
            raise ValueError("Operation receipt does not exist.")
        if expected is not None and value["state"] != expected:
            raise ValueError("Operation is already claimed or completed; inspect its receipt.")
        value.update(state=state, updated_at=now())
        if result is not None:
            value["result"] = result
        write(path, value)
        return value


def get(operation_id: str) -> dict:
    value = read(receipt_path(operation_id))
    if value is None:
        raise ValueError("Operation receipt not found.")
    return value


def annotate(operation_id: str, field: str, data: dict) -> dict:
    """Add local evidence without changing retry eligibility or dispatch state."""
    if field not in {"send_intent", "sent_observation"}:
        raise ValueError("Unsupported receipt annotation.")
    path = receipt_path(operation_id)
    with locked(path):
        value = read(path)
        if not value:
            raise ValueError("Operation receipt not found.")
        if field == "send_intent" and field in value and value[field] != data:
            raise ValueError("Send intent is immutable.")
        value[field] = data
        value["updated_at"] = now()
        write(path, value)
        return value
