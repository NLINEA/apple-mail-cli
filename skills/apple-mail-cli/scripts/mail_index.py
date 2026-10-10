"""Read-only adapter for Apple Mail's local Envelope Index."""

from __future__ import annotations

import sqlite3
import base64
import hashlib
import json
import time
import unicodedata
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit, urlparse


class MailIndexError(RuntimeError):
    pass


SEARCH_CURSOR_VERSION = 2
INDEX_QUERY_TIMEOUT_SECONDS = 30
MAX_CURSOR_CHARS = 2048
MAILBOX_ROLE_VERSION = 1
# Conservative root-path aliases. Provider/localized/nested paths need native
# validation; a custom folder containing a role word is never delivery evidence.
ROOT_MAILBOX_ROLES = {
    "sendlater": "scheduled",
    "sent": "sent", "sent messages": "sent", "sent mail": "sent",
    "draft": "draft", "drafts": "draft",
    "trash": "trash", "deleted": "trash", "deleted messages": "trash",
    "inbox": "inbox", "in": "inbox",
    "junk": "junk", "spam": "junk", "outbox": "outbox",
}


def folded_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).casefold()


def contains_sql(expression: str) -> str:
    """Literal Unicode substring match; user text never becomes SQL syntax."""
    return f"instr(mailctl_casefold(coalesce({expression}, '')), ?) > 0"


class ReadOnlyConnection(sqlite3.Connection):
    def __exit__(self, *args: Any) -> bool:
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def find_index(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise MailIndexError(f"Mail index not found: {path}")
        return path
    candidates = list((Path.home() / "Library" / "Mail").glob("V*/MailData/Envelope Index"))
    if not candidates:
        raise MailIndexError("Apple Mail Envelope Index was not found. Full Disk Access may be required.")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10, factory=ReadOnlyConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.create_function("mailctl_casefold", 1, folded_text, deterministic=True)
        connection.create_function("mailctl_mailbox_state", 1, mailbox_state, deterministic=True)
        deadline = time.monotonic() + INDEX_QUERY_TIMEOUT_SECONDS
        connection.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        return connection
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise MailIndexError(f"Cannot open Mail index read-only: {error}") from error


def mailbox_state(url: str) -> str:
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return "other"
    if parsed.scheme.lower() not in {"", "imap", "imaps", "local"} or parsed.query or parsed.fragment:
        return "other"
    # Split before decoding so an encoded slash cannot manufacture a root role.
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        return "other"
    return ROOT_MAILBOX_ROLES.get(folded_text(parts[0]), "other")


def iso_epoch(value: int | float | None) -> str | None:
    if value in (None, 0):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


BASE_QUERY = """
WITH
to_recipients AS (
  SELECT r.message,
         group_concat(lower(a.address), char(31)) AS addresses
    FROM recipients r
    JOIN addresses a ON a.ROWID = r.address
   WHERE r.type = 0
   GROUP BY r.message
),
cc_recipients AS (
  SELECT r.message,
         group_concat(lower(a.address), char(31)) AS addresses
    FROM recipients r
    JOIN addresses a ON a.ROWID = r.address
   WHERE r.type = 1
   GROUP BY r.message
),
bcc_recipients AS (
  SELECT r.message,
         group_concat(lower(a.address), char(31)) AS addresses
    FROM recipients r
    JOIN addresses a ON a.ROWID = r.address
   WHERE r.type = 2
   GROUP BY r.message
),
attachment_names AS (
  SELECT message,
         group_concat(name, char(31)) AS names,
         count(*) AS attachment_count
    FROM attachments
   GROUP BY message
),
sender_values AS (
  SELECT sa.sender,
         group_concat(lower(a.address), char(31)) AS addresses
    FROM sender_addresses sa
    JOIN addresses a ON a.ROWID = sa.address
   GROUP BY sa.sender
)
SELECT m.ROWID AS row_id,
       m.message_id AS internal_message_hash,
       g.message_id_header AS message_id,
       m.conversation_id AS conversation_id,
       s.subject AS subject,
       coalesce(sm.summary, '') AS summary,
       mb.url AS mailbox_url,
       g.send_later_date AS send_later_epoch,
       m.date_sent AS date_sent_epoch,
       m.date_received AS date_received_epoch,
       m.read AS is_read,
       m.flagged AS is_flagged,
       m.deleted AS is_deleted,
       m.size AS size_bytes,
       coalesce(tr.addresses, '') AS to_addresses,
       coalesce(cr.addresses, '') AS cc_addresses,
       coalesce(br.addresses, '') AS bcc_addresses,
       coalesce(sv.addresses, '') AS sender_addresses,
       coalesce(an.names, '') AS attachment_names,
       coalesce(an.attachment_count, 0) AS attachment_count
  FROM messages m
  JOIN mailboxes mb ON mb.ROWID = m.mailbox
  JOIN subjects s ON s.ROWID = m.subject
  JOIN message_global_data g ON g.ROWID = m.global_message_id
  LEFT JOIN summaries sm ON sm.ROWID = m.summary
  LEFT JOIN to_recipients tr ON tr.message = m.ROWID
  LEFT JOIN cc_recipients cr ON cr.message = m.ROWID
  LEFT JOIN bcc_recipients br ON br.message = m.ROWID
  LEFT JOIN attachment_names an ON an.message = m.ROWID
  LEFT JOIN sender_values sv ON sv.sender = m.sender
"""


def split_values(value: str) -> list[str]:
    return sorted({item.strip() for item in (value or "").split(chr(31)) if item.strip()})


def row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def normalize_row(row: sqlite3.Row) -> dict[str, Any]:
    message_id = (row["message_id"] or "").strip()
    if not message_id:
        internal_hash = row["internal_message_hash"]
        message_id = f"hash:{internal_hash}" if internal_hash is not None else f"row:{row['row_id']}"
    return {
        "row_id": row["row_id"],
        "conversation_id": row_value(row, "conversation_id", 0),
        "message_id": message_id,
        "subject": row["subject"] or "",
        "summary": row_value(row, "summary", "") or "",
        "state": mailbox_state(row["mailbox_url"]),
        "mailbox_url": row["mailbox_url"],
        "account_key": urlparse(row["mailbox_url"] or "").netloc,
        "scheduled_at": iso_epoch(row["send_later_epoch"]),
        "date_sent": iso_epoch(row["date_sent_epoch"]),
        "date_received": iso_epoch(row["date_received_epoch"]),
        "read": bool(row["is_read"]),
        "flagged": bool(row["is_flagged"]),
        "deleted": bool(row["is_deleted"]),
        "size_bytes": row["size_bytes"],
        "to": split_values(row["to_addresses"]),
        "cc": split_values(row["cc_addresses"]),
        "bcc": split_values(row["bcc_addresses"]),
        "sender_addresses": split_values(row["sender_addresses"]),
        "attachments": split_values(row["attachment_names"]),
        "attachment_count": row["attachment_count"],
    }


def list_messages(
    *,
    database: str | None = None,
    states: Iterable[str] | None = None,
    recipient: str | None = None,
    subject: str | None = None,
    limit: int = 5000,
) -> tuple[Path, list[dict[str, Any]]]:
    path = find_index(database)
    where: list[str] = []
    params: list[Any] = []
    state_values = set(states or [])
    if state_values:
        selected_states = sorted(state_values)
        where.append("mailctl_mailbox_state(mb.url) IN (" + ",".join("?" for _ in selected_states) + ")")
        params.extend(selected_states)
    if recipient:
        where.append(contains_sql("tr.addresses"))
        params.append(folded_text(recipient))
    if subject:
        where.append(contains_sql("s.subject"))
        params.append(folded_text(subject))
    sql = BASE_QUERY
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY max(coalesce(g.send_later_date, 0), coalesce(m.date_sent, 0), coalesce(m.date_received, 0)) DESC LIMIT ?"
    params.append(max(1, min(limit, 100000)))
    try:
        with connect_read_only(path) as connection:
            rows = connection.execute(sql, params).fetchall()
    except sqlite3.Error as error:
        raise MailIndexError(f"Mail index query failed: {error}") from error
    return path, [normalize_row(row) for row in rows]


def parse_iso_epoch(value: str | None, label: str) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MailIndexError(f"{label} must be an ISO date/time.") from error
    if parsed.tzinfo is None:
        raise MailIndexError(f"{label} must include a UTC offset.")
    return parsed.timestamp()


def _cursor_digest(filters: dict[str, Any]) -> str:
    encoded = json.dumps(filters, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def encode_cursor(anchor: list[int | float], filters: dict[str, Any]) -> str:
    payload = json.dumps(
        {"v": SEARCH_CURSOR_VERSION, "after": anchor, "query": _cursor_digest(filters)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str | None, filters: dict[str, Any]) -> list | None:
    if not value:
        return None
    if len(value) > MAX_CURSOR_CHARS:
        raise MailIndexError("Search cursor is invalid.")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise MailIndexError("Search cursor is invalid.") from error
    if (
        not isinstance(payload, dict)
        or payload.get("v") != SEARCH_CURSOR_VERSION
        or payload.get("query") != _cursor_digest(filters)
        or not isinstance(payload.get("after"), list)
        or len(payload["after"]) != 2
        or any(type(part) not in {int, float} or not math.isfinite(part) for part in payload["after"])
    ):
        raise MailIndexError("Search cursor does not match this query.")
    return payload["after"]


def search_messages(
    *,
    database: str | None = None,
    states: Iterable[str] | None = None,
    query: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    subject: str | None = None,
    attachment: str | None = None,
    has_attachment: bool | None = None,
    unread: bool | None = None,
    flagged: bool | None = None,
    since: str | None = None,
    before: str | None = None,
    conversation_id: int | None = None,
    limit: int = 100,
    cursor: str | None = None,
    chronological: bool = False,
    account: str | None = None,
    exact_address: bool = False,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Search the complete metadata index without reading message bodies."""
    path = find_index(database)
    state_values = sorted(set(states or []))
    database_stat = path.stat()
    filters = {
        "database": str(path),
        "database_identity": [database_stat.st_dev, database_stat.st_ino],
        "mailbox_role_version": MAILBOX_ROLE_VERSION,
        "states": state_values,
        "query": folded_text((query or "").strip()),
        "sender": folded_text((sender or "").strip()),
        "recipient": folded_text((recipient or "").strip()),
        "subject": folded_text((subject or "").strip()),
        "attachment": folded_text((attachment or "").strip()),
        "has_attachment": has_attachment,
        "unread": unread,
        "flagged": flagged,
        "since": since,
        "before": before,
        "conversation_id": conversation_id,
        "chronological": chronological,
        "account": account,
        "exact_address": exact_address,
        "message_id": message_id,
    }
    anchor = decode_cursor(cursor, filters)
    page_limit = max(1, min(int(limit), 1000))
    where: list[str] = []
    params: list[Any] = []
    where.append("coalesce(m.deleted, 0) = 0")
    if account:
        where.append("mb.url LIKE ? ESCAPE '\\'")
        escaped = account.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%://{escaped}/%")
    if message_id:
        where.append("g.message_id_header = ?")
        params.append(message_id)
    if state_values:
        where.append("mailctl_mailbox_state(mb.url) IN (" + ",".join("?" for _ in state_values) + ")")
        params.extend(state_values)
    if filters["sender"]:
        if exact_address:
            where.append("EXISTS (SELECT 1 FROM sender_addresses xs JOIN addresses xa ON xa.ROWID = xs.address WHERE xs.sender = m.sender AND mailctl_casefold(xa.address) = ?)")
            params.append(filters["sender"])
        else:
            where.append(contains_sql("sv.addresses"))
            params.append(filters["sender"])
    if filters["recipient"]:
        if exact_address:
            where.append("EXISTS (SELECT 1 FROM recipients xr JOIN addresses xa ON xa.ROWID = xr.address WHERE xr.message = m.ROWID AND mailctl_casefold(xa.address) = ?)")
            params.append(filters["recipient"])
        else:
            where.append(contains_sql("coalesce(tr.addresses, '') || char(31) || coalesce(cr.addresses, '') || char(31) || coalesce(br.addresses, '')"))
            params.append(filters["recipient"])
    if filters["subject"]:
        where.append(contains_sql("s.subject"))
        params.append(filters["subject"])
    if filters["attachment"]:
        where.append(contains_sql("an.names"))
        params.append(filters["attachment"])
    if has_attachment is True:
        where.append("coalesce(an.attachment_count, 0) > 0")
    elif has_attachment is False:
        where.append("coalesce(an.attachment_count, 0) = 0")
    if unread is not None:
        where.append("m.read = ?")
        params.append(0 if unread else 1)
    if flagged is not None:
        where.append("m.flagged = ?")
        params.append(1 if flagged else 0)
    if conversation_id is not None:
        where.append("m.conversation_id = ?")
        params.append(int(conversation_id))
    timestamp_expression = "max(coalesce(g.send_later_date, 0), coalesce(m.date_sent, 0), coalesce(m.date_received, 0))"
    if anchor is not None:
        comparison = ">" if chronological else "<"
        where.append(f"({timestamp_expression} {comparison} ? OR ({timestamp_expression} = ? AND m.ROWID {comparison} ?))")
        params.extend([anchor[0], anchor[0], anchor[1]])
    since_epoch = parse_iso_epoch(since, "since")
    before_epoch = parse_iso_epoch(before, "before")
    if since_epoch is not None:
        where.append(f"{timestamp_expression} >= ?")
        params.append(since_epoch)
    if before_epoch is not None:
        where.append(f"{timestamp_expression} < ?")
        params.append(before_epoch)
    if filters["query"]:
        expressions = ("s.subject", "sm.summary", "sv.addresses", "tr.addresses", "cr.addresses", "br.addresses", "an.names")
        where.append("(" + " OR ".join(contains_sql(expression) for expression in expressions) + ")")
        params.extend([filters["query"]] * len(expressions))
    sql = BASE_QUERY
    if where:
        sql += " WHERE " + " AND ".join(where)
    direction = "ASC" if chronological else "DESC"
    sql += f" ORDER BY {timestamp_expression} {direction}, m.ROWID {direction} LIMIT ?"
    params.append(page_limit + 1)
    try:
        with connect_read_only(path) as connection:
            fetched = connection.execute(sql, params).fetchall()
    except sqlite3.Error as error:
        raise MailIndexError(f"Mail index search failed: {error}") from error
    has_more = len(fetched) > page_limit
    rows = [normalize_row(row) for row in fetched[:page_limit]]
    return {
        "mail_index": str(path),
        "messages": rows,
        "returned": len(rows),
        "has_more": has_more,
        "next_cursor": encode_cursor([max(fetched[page_limit - 1][key] or 0 for key in ("send_later_epoch", "date_sent_epoch", "date_received_epoch")), fetched[page_limit - 1]["row_id"]], filters) if has_more else None,
        "body_searched": False,
    }


def thread_messages(
    conversation_id: int,
    *,
    database: str | None = None,
    limit: int = 500,
    cursor: str | None = None,
) -> dict[str, Any]:
    if int(conversation_id) <= 0:
        raise MailIndexError("conversation_id must be positive.")
    return search_messages(
        database=database,
        conversation_id=int(conversation_id),
        limit=limit,
        cursor=cursor,
        chronological=True,
    )


def schema_report(database: str | None = None) -> dict[str, Any]:
    path = find_index(database)
    required = {
        "messages": {"ROWID", "message_id", "global_message_id", "sender", "mailbox", "subject", "summary", "conversation_id", "date_sent", "date_received", "read", "flagged", "deleted", "size"},
        "message_global_data": {"ROWID", "message_id_header", "send_later_date"},
        "mailboxes": {"ROWID", "url"},
        "subjects": {"ROWID", "subject"},
        "summaries": {"ROWID", "summary"},
        "sender_addresses": {"sender", "address"},
        "recipients": {"message", "address", "type"},
        "addresses": {"ROWID", "address"},
        "attachments": {"message", "name"},
    }
    tables: dict[str, list[str]] = {}
    missing: dict[str, list[str]] = {}
    try:
        with connect_read_only(path) as connection:
            for table, columns in required.items():
                present = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
                if "ROWID" in columns and present and "ROWID" not in present:
                    try:
                        connection.execute(f"SELECT ROWID FROM {table} LIMIT 0")
                        present.add("ROWID")
                    except sqlite3.Error:
                        pass
                tables[table] = sorted(present)
                absent = sorted(columns - present)
                if absent:
                    missing[table] = absent
    except sqlite3.Error as error:
        raise MailIndexError(f"Mail index schema check failed: {error}") from error
    return {"mail_index": str(path), "compatible": not missing, "missing": missing, "tables": tables}


def unique_messages(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict]] = {}
    rank = {"sent": 5, "scheduled": 4, "draft": 3, "outbox": 2, "inbox": 1, "other": 0, "trash": -1}

    def score(row: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
        recipients = sum(len(row.get(field) or []) for field in ("to", "cc", "bcc"))
        sender = int(bool(row.get("sender_addresses")))
        attachments = int(row.get("attachment_count") or 0)
        dated = int(bool(row.get("scheduled_at") or row.get("date_sent") or row.get("date_received")))
        row_id = int(row.get("row_id") or 0)
        return (rank.get(str(row.get("state") or ""), 0), recipients, sender, attachments, dated, -row_id)

    for row in rows:
        key = row["message_id"]
        grouped.setdefault(key, []).append(row)
        current = selected.get(key)
        if current is None or score(row) > score(current):
            selected[key] = row
    output = []
    for key in sorted(selected):
        chosen = selected[key]
        # Inbox/Sent replicas describe different lifecycle views; compare same-state evidence.
        peers = [row for row in grouped[key] if row.get("state") == chosen.get("state")]
        conflicts = []
        for field in ("sender_addresses", "to", "cc", "bcc", "attachments", "scheduled_at"):
            values = {json.dumps(sorted(row[field]) if isinstance(row[field], list) else row[field], sort_keys=True)
                      for row in peers if field in row and (row.get(field) or (field in {"cc", "bcc", "attachments"} and row.get("to") and row.get("sender_addresses")))}
            if len(values) > 1:
                conflicts.append(field)
        if conflicts:
            chosen = {**chosen, "replica_conflicts": conflicts}
        output.append(chosen)
    return output


def counts(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    unique = unique_messages(materialized)
    return {
        "index_rows": len(materialized),
        "unique_message_ids": len(unique),
        "states": dict(sorted(Counter(row["state"] for row in unique).items())),
        "replica_rows": len(materialized) - len(unique),
    }
