# Search, conversations, and triage

Use `list` for the newest bounded mailbox view. Use `search` when the target may
be older than the first scan window:

```bash
mailctl search --sender press@example.com --since 2026-08-01T00:00:00+08:00
mailctl search --query partnership --has-attachment --limit 100
mailctl search --recipient press@example.com --exact-address --account ACCOUNT-KEY
mailctl search --message-id '<exact-rfc-message-id>'
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

Version 0.4.2 also binds cursors to the mailbox-role mapping revision. State
filters and returned rows use the same conservative root-path classification;
unknown custom/provider paths are `other`. See `cli-contract.md` for the exact
synthetic mapping and native-validation limit. Body-access failures in `read` and
body search fail the command; triage returns explicit partial failure rather than
classifying an inaccessible body as an empty successful read.

Search account filters use the returned `account_key` (not the display name used
by AppleScript mailbox commands). Cursors bind the filters and database identity
and use timestamp/row ID keysets: a newly arriving mail does not shift the next
page into duplicates. They are not a frozen snapshot; timestamp changes or
deletions can still change visibility. Version 0.4 cursors must be restarted.
Deleted index rows are excluded. Use `resolve` with a Mail account display name
and mailbox when you need a current numeric object ID for an RFC Message-ID.

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

Classification strips common quoted-history and signature/footer boundaries so
an unsubscribe line in an earlier message does not override a positive reply.
Auto-Submitted and In-Reply-To headers supplement text signals. Confidence is a
heuristic, not a calibrated probability. Truncated content is capped at 0.5 and
requires review; classification never authorizes sending, suppression, or deletion.

Explicit unsubscribe requests in the newest reply are retained rather than
mistaken for footer instructions. Missing or malformed attachment metadata is
reported as `attachment_metadata_unavailable: true` and requires review.

Since version 0.5.1, triage isolates message failures and returns:

- `items` and `classification_counts` for successful classifications only;
- `errors`, `failed`, `attempted`, and `not_attempted_ids` for recovery;
- `ok: false` / exit 1 and `complete: false` if any selected message failed;
- `stopped_early: true` when permission, platform/backend failures or Mail
  automation timeout makes further reads inappropriate.

`complete` means all selected messages were classified, not that the entire
mailbox was scanned. Check `truncated` separately. `--output` still writes a
private partial artifact after message failures; dashboards reject that artifact
until the failed messages are resolved. Bodies, headers and raw exception text
are not copied into errors. There is no automatic message retry or follow-up send.

## Grouped reads (0.6)

```bash
mailctl read-batch --mailbox INBOX --account 'Account display name' --id 123 --id 456
mailctl triage --mailbox INBOX --limit 60 --batch-size 20
```

Explicit batches accept 1–100 unique positive numeric Mail IDs. Each backend call
resolves the mailbox once and reads at most 20 messages; `--batch-size 1` uses the
single-message route for troubleshooting. Bodies remain bounded per message by
`--max-chars` (backend range 1,000–200,000). `read-batch` returns bodies because it
is a read request; triage still excludes bodies and headers from its result.

The grouping reduces process launches, not Mail's underlying per-message work.
No live speedup multiplier is promised. If an entire call times out, its intermediate
reads cannot be recovered, so that chunk is marked failed and later chunks are not
attempted; completed earlier chunks are retained. Inspect errors rather than
treating missing results as empty messages or no replies.
