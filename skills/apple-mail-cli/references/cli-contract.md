# CLI contract

`mailctl` is the shared adapter for Codex/ChatGPT, Hermes, and the UBD OS
handoff. It writes one JSON object to standard output and uses these exit
codes:

| Exit | Meaning |
| --- | --- |
| `0` | Command completed and the returned object has `ok: true`. |
| `1` | Runtime, validation, incomplete audit, permission, or safety failure. A JSON error object is returned. |
| `2` | Invalid command-line usage: argparse text on stderr, no JSON on stdout. |

Run `mailctl capabilities` without opening Mail or reading user data. Its
`apple-mail-cli/v1` response lists current command classes and hard authority
boundaries. Run `mailctl --version` when exact implementation identity matters.

## Operational classes

- Offline read-only: contract discovery and campaign validation/planning.
- Mail read-only: bounded listing, complete metadata search, conversation views,
  reading, bilingual classification, local Mail audit, campaign reconciliation,
  quota dashboards, and UBD event construction.
- Local artifact writes: MIME rendering, replacement rebalance manifests,
  resumable scheduling sessions and checkpoints, private triage JSON, private
  CSV, private event JSON, and an automatically invalidated private Send Later
  evidence cache.
- Draft mutations: visible `compose`, `reply`, and revision-guarded `draft update`; none sends.
- Confirmed Mail mutations: `mark`, `flag`, `move`, `trash`, and direct `send`.
- UBD read-only: capabilities and scoped work reads.
- UBD proposal-only: a stored campaign or classification proposal that still
  requires human acceptance in UBD OS.

Mail-index operations always open SQLite with `mode=ro` plus
`PRAGMA query_only=ON`. There is no Mail database write path. Send Later uses
Mail's visible UI because Apple Mail does not expose a reliable supported
schedule-time setter.

Apple Mail can omit sender and attachment rows from its Send Later metadata
index. For exact campaign candidates only, reconciliation may read the matching
local `.emlx` by Message-ID. It does not return body content; it extracts the
From address and verifies attachment MIME filename, decoded byte size, and
SHA-256. The adapter never modifies the message store. If neither source can
prove attachment state, the issue is `attachment_unverified`; a proven
mismatch is `wrong_attachment`.

Unchanged `.emlx` evidence may be reused from a `0600` cache under the user's
local cache directory. Cache keys contain an opaque SHA-256 path token rather
than a local message path; entries are bound to device, inode, file size, and
nanosecond modification/change times with a 15-minute evidence TTL. `--fresh`
bypasses reuse. A changed file is reparsed before it can be
trusted. The cache is bounded, atomically replaced, and never written into
Apple Mail's directories.

`campaign session-record --result applied` records an attempted visible-UI
action only. The operation remains `applied_unverified` until
`session-reconcile` observes the exact expected state in Sent or Send Later.
Session files are bound to the exact manifest digest and updated atomically.
`session-next` claims work across processes/sessions; `session-record` requires
its ownership token. Claims do not auto-expire, and unsafe audit results block
continuation. `session-status` is the private recovery view.

Version 0.6 adds cross-campaign intent and Message-ID claim resources, with fixed
lock order and token-bound group release. A crash can leave blocking partial
claims; the campaign/key recovery anchor is published first. `blocked_claims`
identifies conflicts. All workers must use 0.6 for cross-campaign guarantees;
old active claims still protect only their legacy scope. Changed pinned Mail
identity stops reschedule/cancel sessions.

`campaign rebalance` always writes a separate manifest. It interprets allowed
hours and daily counts in `policy.timezone`, counts account-wide Sent and Send
Later volume by default, locks already-sent campaign rows, preserves valid
slots, and moves only rows that do not fit the requested window or policy.

## Confirmation contract

- `mark`, `flag`, and `move`: require `--confirm-change`.
- `trash`: requires `--confirm-trash`.
- `send`: previews offline by default; requires `--confirm-send SHA256` after the current conversation has shown
  the exact To/CC/BCC, sender, subject, body, and attachment evidence.
- `send` requires an explicit `--sender`, reads the created draft back, and
  guards its snapshot again before asking Mail to send. Missing evidence blocks
  sending. Draft verification concerns observable fields and referenced files,
  not server delivery. Acceptance returns `submission_accepted: true`,
  `delivery_confirmed: false`, and the confirmed `intent_digest`.
- `draft update`: a full field replacement gated by `--expected-revision` from
  `draft show` and `--confirm-change`; it remains unsent even if verification fails.
- `ubd propose-*`: requires `--confirm-proposal`, exact business-unit/work UUIDs,
  positive current revision, and a UUID idempotency key.
- A draft request never authorizes `send`; a UBD proposal never authorizes a
  Mail mutation.

These flags are execution gates, not substitutes for current-turn user
authorization.

`campaign render --force` is an owned-artifact replacement, never blanket EML
cleanup. New files are staged, previous owned artifacts receive a private backup,
and unrelated/stale files remain. Caught publication failures roll back; hard
interruptions still require checking queue/checkpoint integrity before recovery.

Triage message failures produce a partial structured result with `ok: false`
and exit 1, retaining successful `items` and recording `errors`. A partial
`--output` artifact remains private and is rejected as dashboard input.
Permission or automation timeout stops additional reads, preserving completed
results. `complete` covers only the selected batch; `truncated` remains separate.

`read-batch` and triage group up to 20 messages per backend call (up to 100 selected
IDs overall). `--batch-size 1` uses the single-message path. Responses must account
for every requested ID exactly once as success, error or explicitly not attempted.
Whole-call timeout yields no recoverable intermediate bodies for that chunk;
previous chunks remain available. There is no automatic retry.

`receipt-reconcile` is Mail-read-only but annotates the private receipt. A positive
`sent_observed` is account/Message-ID metadata matching, not full integrity or
delivery verification. Dispatch state remains unchanged. `campaign followups`
is a local human-review worklist, optionally saved privately; it never contacts
recipients or creates UBD work. Read [post-send-and-followups.md](post-send-and-followups.md)
before interpreting either command.

## Privacy and credentials

Direct Mail read commands can return addresses and body text because that is
their purpose. Campaign `.emlx` fallback parsing never returns body content or
local message paths. Its private evidence cache can contain raw Message-ID,
sender addresses, attachment filenames, sizes, and hashes, so it must remain
local and must never be attached to a UBD event. It contains no message bodies
or local message paths. Redacted UBD events cannot contain addresses, subject, body,
headers, mailbox/account names, raw identifiers, result rows, or attachment
data. Raw Message-ID values are replaced by SHA-256 references.

`search` and `thread` use the complete read-only metadata index and never
search message bodies. Their opaque cursors are bound to the exact filter set;
changing a filter invalidates the cursor. `triage` reads only a bounded batch
of bodies and returns no body content in its result artifact.

## Error object

Runtime failures use a stable object:

```json
{
  "ok": false,
  "command": "campaign",
  "error": {
    "code": "campaign_error",
    "type": "CampaignError",
    "message": "Human-readable detail",
    "hint": "Safe next step",
    "retryable": false
  }
}
```

`retryable: true` never authorizes an automatic mutation retry. Audit actual
Mail state first whenever a previous attempt could have changed visible Mail.

Version 0.4.1 sends Python-to-JXA requests through UTF-8 stdin and validates the
backend response object, command and boolean `ok` field. Invalid responses fail
with `backend_contract_error`. Backend confirmation checks run before loading
Mail. No automatic retries are performed. Each SQLite connection closes on
context exit and interrupts query VM work after approximately 30 seconds;
SQLite lock waits are capped separately at 10 seconds. This is a per-connection
bound, not an end-to-end workflow deadline.

An explicit sender is required for direct `send` (`sender_required` otherwise).
Any supplied outgoing sender must match exactly one enabled account before a
draft is created. A successful send reports Mail's acceptance only, with
`delivery_confirmed: false`; it does not prove remote delivery. Draft requests
without a sender still use Mail's default identity and require visible review.
Duplicate account names are rejected even for aggregate mailbox aliases.

In 0.4.3, direct-send intent is `mailctl-send-intent/v1`: normalized sender and
ordered To/CC/BCC arrays, exact subject/body strings, and ordered attachment
descriptors `{name, size_bytes, sha256}`. Python serializes sorted keys with
compact separators, UTF-8 and no ASCII escaping; the backend independently
recomputes the same SHA-256 using macOS CommonCrypto. Unicode strings are not
NFC-normalized. Paths are excluded from the digest; filenames and bytes are
included. Preview uses no Mail automation or journal writes and exposes the
full private content for human review. Confirmation must exactly equal its
lowercase 64-character digest. Old boolean confirmation is rejected.

Confirmed sends reserve a digest directory atomically and fsync a minimal
private journal before automation. Source attachments are copied to mode 0400
private snapshots and rehashed; at most 20 files totaling 25 MiB are accepted.
The backend verifies the intent, reservation, private modes/ownership and
snapshot byte hashes, then atomically creates a one-use claim before loading
Mail. Journal and snapshots are retained under
`~/Library/Application Support/apple-mail-cli/send-attempts/v1/`. A same-intent
second confirmation fails `send_attempt_exists`; raw backend replay cannot
reuse its claim. No automatic cleanup/reset/resend occurs. Corrupt, absent or
uncertain journal state is not permission to retry.

The new success data uses `submission_accepted: true`, `outcome: accepted_by_mail`,
`intent_digest`, and `delivery_confirmed: false`; the former `sent: true` field
is removed. This means Mail accepted the request, not verified queuing or
delivery. Any exception after invoking the backend or persisting its final
outcome yields `send_outcome_unknown`, `retryable: false`, with a retained guard.
Preparation errors retain a reservation without invoking Mail. Process death
may leave `preparing` or `in_flight`; both still block another attempt.
Manual removal of state, another user/host, or a changed intent can bypass local
duplicate protection; the digest is not an authorization signature. See the
README migration and SECURITY.md before adapting agents or scripts.

CLI flag values can appear in the caller's argv and shell history. Prefer
`--body-file`; subject, recipients and attachment paths are still CLI arguments.
Raw mail output remains private. The stdin change does not make mail public-safe.

Version 0.4.2 retains all subject/primary-recipient-overlap candidates in audit
`matches` and `distinct_message_ids`. Any recipient conflict blocks continuation;
multiple distinct candidate IDs are `duplicate` even beside an exact Sent copy.
Legitimate same-ID index replicas remain deduplicated. Recipient-plus-subject
identity cannot attribute an entirely misaddressed copy; a unique campaign token
helps that boundary.

Scheduled attachment matches require readable `.emlx` evidence of filename,
decoded size and SHA-256. Matching index names/counts alone become
`attachment_unverified`. Proven metadata mismatches remain `wrong_attachment`.
Historical Sent metadata-only matches explicitly use `metadata_matched` with
`sha256_checked: false`; they do not certify attachment bytes.

Mailbox roles use the same classifier in SQLite state filters and row output.
Supported root aliases are SendLater; Sent / Sent Messages / Sent Mail; Draft /
Drafts; INBOX / In; Trash / Deleted / Deleted Messages; Junk / Spam; and Outbox.
The supported URL schemes are imap, imaps and local, plus bare root paths.
URL path components are decoded individually. Nested/provider/localized paths
not in this mapping remain `other`, including nested folders named Sent.
These are synthetic mapping fixtures, not proof of native provider roles.
Campaign audit scans Sent, Send Later and unknown-role metadata; a matching
unknown-location copy raises `mailbox_unverified` and prevents continuation.
The broader conservative audit remains bounded at 100,000 rows. Cursors created
before the mailbox-role mapping revision require a fresh search.

Required body access failures propagate as backend errors, so `read` and body
search cannot return success with inaccessible content. `classify` and `triage`
stop on that failure. Empty content is valid only when body access succeeded.

UBD credentials are accepted only through the environment. The default names
are `UBD_AGENT_API_ORIGIN` and `UBD_AGENT_TOKEN`; `--token-env` may select
another valid environment-variable name. Tokens are never accepted as command
arguments, stored in artifacts, sent through proxies, or forwarded through
HTTP redirects. HTTPS is mandatory except for localhost contract tests.

## Cross-agent invocation

All command results and runtime errors include version, schema and observation
time. `--help`/`--version` and parser usage errors remain ordinary CLI text.
`mark`/`flag` read back the modified field; move/trash report accepted but
unverified. Independently inspect destination state before claiming completion.

Mail mutations accept a caller-supplied stable `--operation-id` UUID. Private
0600 receipts persist request fingerprints and outcomes before/after dispatch.
Identical completed requests replay their receipts; uncertain or in-flight Mail
mutations never retry automatically. `receipt UUID` reads the journal.
If omitted, a UUID is generated and returned; callers needing crash recovery
must supply and retain their own ID before invoking the command.
The default state directory is `~/Library/Application Support/apple-mail-cli`.
`MAILCTL_STATE_DIR` is only for isolated tests; every production agent must use
the same directory or ownership/idempotency protection cannot span agents.
Receipts can contain private draft text/metadata or existing UBD work detail;
unlike redacted events, they must never be published or passed to UBD.

`ubd retry-proposal UUID --confirm-proposal` replays the exact persisted request
to the same validated origin with the same key, after a fresh capability check.
The server still checks its saved expected work revision and human-review gate.
It is safe against a changed observation timestamp; it does not regenerate the
proposal. Completed receipts are returned without another submission.

`attachments save` writes to a private temporary sibling and exclusively creates
the requested output. It reports actual saved bytes and SHA-256; Mail's reported
attachment size is approximate, not an integrity guarantee.

## Shared installation

Both Codex and Hermes should invoke the same installed `mailctl` command. The
plugin source remains the single implementation; the Hermes configuration
loads the same skill directory through `skills.external_dirs`. Do not copy the
Python/JXA files into another agent tree, because copies can drift from the
audited implementation.

In a new session, verify discovery before a mutation:

```bash
mailctl capabilities
mailctl doctor
```

For Hermes, `hermes skills list --enabled-only` must include
`apple-mail-cli`. For Codex, `codex plugin list` must show
`apple-mail-cli@personal` as `installed, enabled`.
