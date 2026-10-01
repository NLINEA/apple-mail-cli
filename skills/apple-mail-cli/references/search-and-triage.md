# Search, conversations, and triage

Use `list` for the newest bounded mailbox view. Use `search` when the target may
be older than the first scan window:

```bash
mailctl search --sender press@example.com --since 2026-08-01T00:00:00+08:00
mailctl search --query partnership --has-attachment --limit 100
```

`search` reads the complete local metadata index but never message bodies. It
can filter sender, any recipient field, subject, summary, attachment name,
state, read/flag state, and date. Continue with the returned `next_cursor` only
under the exact same filters. Use a result's `conversation_id` to view the
thread chronologically:

```bash
mailctl thread 3423
```

Text filters normalize NFC and use Unicode case folding. They match literal
substrings, so `%` and `_` are not SQL wildcards. The index may contain sensitive
body-derived summaries even though full bodies are not searched.

Version 0.4.1 cursors use format v2 and bind the database path/file identity as
well as the filters. Old cursors or cursors from a replaced/different database
are rejected; restart the search. Page size may change between requests. The
index remains live: offset pagination can repeat or skip rows if Mail changes
the matching set between pages. It does not promise a frozen snapshot.

Version 0.4.2 also binds cursors to the mailbox-role mapping revision. State
filters and returned rows use the same conservative root-path classification;
unknown custom/provider paths are `other`. See `cli-contract.md` for the exact
synthetic mapping and native-validation limit. Body-access failures in `read`,
body search and triage propagate rather than becoming an empty successful read.

Use `triage` for a bounded classification batch:

```bash
mailctl triage --mailbox INBOX --unread --limit 30 \
  --output /private/path/triage.json
```

Triage recognizes permanent and transient bounces, DMARC, auto-replies,
out-of-office messages, opt-outs, complaints, alternate contacts, and positive,
negative, question-bearing, or unclassified human replies in English and
Chinese. It reads bounded bodies locally but excludes body content from the
returned artifact. The private artifact may feed `campaign dashboard`; never
send it to UBD OS. Only aggregate counts cross that boundary.
