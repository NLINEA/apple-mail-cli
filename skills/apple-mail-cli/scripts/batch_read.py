"""Bounded Mail reads. Backend bodies remain transient unless explicitly requested."""

FATAL_CODES = {"mail_permission_denied", "platform_unavailable", "backend_missing",
               "backend_contract_error", "mail_automation_timeout"}


def read_selected(args, summaries, backend, error_type):
    size = getattr(args, "batch_size", 20)
    if not 1 <= size <= 20:
        raise error_type("--batch-size must be between 1 and 20.")
    ids = [row["id"] for row in summaries]
    if len(ids) > 100 or len(ids) != len(set(ids)) or any(type(item) is not int or item <= 0 for item in ids):
        raise error_type("Select 1 to 100 unique positive Mail IDs.")
    for offset in range(0, len(ids), size):
        chunk = ids[offset:offset + size]
        payload = {"mailbox": args.mailbox, "account": args.account, "max_chars": args.max_chars}
        try:
            if len(chunk) == 1:
                data = backend({**payload, "command": "read", "message_id": chunk[0]}, args.timeout)["data"]
                result = {"items": [data], "errors": [], "not_attempted_ids": []}
            else:
                result = backend({**payload, "command": "read_batch", "message_ids": chunk}, args.timeout)["data"]
            items = result["items"]
            errors = result["errors"]
            skipped = result.get("not_attempted_ids", [])
            observed = [row["id"] for row in items + errors] + skipped
            if (len(observed) != len(chunk) or set(observed) != set(chunk)
                    or len(observed) != len(set(observed))
                    or any(type(value) is not int for value in observed)
                    or any(row.get("code") not in FATAL_CODES | {"message_read_failed"} for row in errors)
                    or (skipped and (not result.get("stopped_early") or not any(row["code"] in FATAL_CODES for row in errors)))):
                raise ValueError("Incomplete batch partition")
        except Exception as error:
            if isinstance(error, error_type):
                failure = error
            else:
                failure = error_type("Invalid Mail batch response.", code="backend_contract_error")
            for message_id in chunk:
                yield message_id, None, failure
            if failure.code in FATAL_CODES:
                return
            continue
        by_id = {row["id"]: row for row in items}
        by_error = {row["id"]: row for row in errors}
        for message_id in chunk:
            if message_id in by_id:
                yield message_id, by_id[message_id], None
            elif message_id in by_error:
                yield message_id, None, error_type("Message read failed.", code=by_error[message_id]["code"])
        if skipped or result.get("stopped_early") or any(row["code"] in FATAL_CODES for row in errors):
            return
