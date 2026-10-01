# Apple Mail CLI

A local JSON-first Apple Mail adapter shared by Codex/ChatGPT and Hermes, with
a redacted proposal-only bridge to UBD OS.

Search local Mail metadata and conversations, then inspect only the messages
you need. For campaign workflows, reconcile exact recipients, sender and
attachment evidence before continuing. The core uses Python's standard library
and macOS JXA; no package install, remote service or mail credentials are needed
for local Mail commands. The UBD OS connector is optional and requires its own
explicitly configured API access.

## Run from source

Requires macOS, Apple Mail configured, and Python 3.9 or newer with SQLite and
IANA timezone data. Python is not guaranteed to be included with every macOS
installation. Start in this source directory:

```bash
python3 skills/apple-mail-cli/scripts/mailctl.py --version
python3 skills/apple-mail-cli/scripts/mailctl.py capabilities
export PATH="$PWD/scripts:$PATH"
mailctl capabilities
```

Keep that directory in place. The launcher resolves the implementation relative
to itself; add its `scripts` directory to PATH instead of symlinking the launcher
alone. `MAILCTL_PYTHON` can select an existing Python executable. Core use does
not require Codex or Hermes. Their plugin/skill configuration is optional.

Maintained by **NLINEA**. Version **0.4.3 is experimental**. Independent review
passed the bounded source/synthetic scope: 83 Python tests passed, one live
test was skipped, and 40 fake JXA checks, four native protocol fixtures and
10 additional review scenarios passed. Actual Mail draft/send, attachment
readback, delivery and Send Later behavior have not been certified. See
[VALIDATION.md](VALIDATION.md) for the measured platform and exact limits.
Read [the CLI contract](skills/apple-mail-cli/references/cli-contract.md) and
[SECURITY.md](SECURITY.md) before connecting an agent or enabling mutations.
[PROVENANCE.md](PROVENANCE.md) records the creator statement, preserved license
and source-review boundary.

## Quick check

```bash
mailctl --version
mailctl capabilities
mailctl doctor
```

`capabilities` is offline. `doctor` checks macOS, the JXA backend and read-only
Envelope Index access/schema without reading message bodies. Its backend ping
does not prove Automation permission or connectivity to a mail provider. A
missing or inaccessible index makes `doctor` fail.

Index commands need existing Full Disk Access for the launching app/runtime.
JXA Mail commands need Automation permission. Visible Send Later operations
also need Accessibility permission. Respect a denied permission. The CLI does
not grant, reset or bypass permissions.

## Safe metadata examples

```bash
mailctl search --subject '香港 proposal 100%' --limit 10
mailctl search --sender person@example.com --has-attachment --limit 10
mailctl thread 42 --limit 20
```

These commands read the local index, including Mail's summaries, without
scanning full message bodies. Results are private mail data. Text filters match
literal Unicode substrings; `%` and `_` are ordinary characters. Continue only
when `has_more` is true, using the returned opaque cursor with the same database
and filters. A changing Mail index is not a frozen pagination snapshot.

## What it covers

- bounded inbox, mailbox, selection, and message reads;
- complete metadata-index search with opaque pagination and conversation views;
- reply, bounce, auto-reply, and DMARC classification;
- bounded bilingual triage for opt-outs, complaints, out-of-office messages,
  and positive, negative, or question-bearing human replies;
- visible drafts and replies, plus explicitly confirmed direct sending from an
  explicit configured sender;
- read-only Sent and Send Later reconciliation by real Message-ID, with a
  local `.emlx` fallback for missing Send Later sender and attachment metadata;
- private evidence caching to reuse repeated audit evidence, with
  file fingerprint invalidation whenever the underlying `.emlx` changes;
- validated, privately rendered, integrity-bound campaign queues;
- timezone-correct, account-quota-aware campaign dashboards and minimal-move
  rebalance manifests;
- resumable visible-UI scheduling, rescheduling, and cancellation sessions whose
  results are not trusted until a fresh read-only Mail audit verifies them;
- confirmed mailbox changes with backend enforcement;
- redacted UBD OS audit, dashboard, and classification events plus
  revision-bound work proposals that still require human review.

The CLI never writes Apple Mail's SQLite database. It does not give UBD OS
send, publish, payment, delete, or direct-database authority.

## Operational limits

- `compose` and `reply` create visible drafts; they are Mail mutations. Draft
  creation does not prove recipient or attachment state. Inspect the draft.
- `send` defaults to an offline preview and requires an explicit `--sender`.
  Sending requires `--confirm-send SHA256` matching that exact reviewed intent.
  A supplied sender must belong to exactly one enabled Mail account. A private
  persistent reservation blocks another attempt of the same intent, including
  errors/timeouts/interruption. Mail's acceptance is not queue or delivery
  evidence; `delivery_confirmed` is false. The digest is not human authorization.
- `mark`, `flag` and `move` require `--confirm-change`; `trash` requires
  `--confirm-trash`. Aggregate mailboxes reject unknown or duplicate account names.
- Use `--body-file` for private text. The Python-to-JXA request uses UTF-8 stdin,
  but values supplied in CLI flags still appear in the caller's process arguments
  and possibly shell history. JSON read results and local artifacts remain sensitive.
- `list` and body scans are bounded. Index text search covers local metadata,
  not a full-body FTS index or remote server mailbox. Unsynced mail is unavailable.
- JXA calls have a per-call timeout (90 seconds by default); each SQLite connection
  limits VM query work to about 30 seconds, with a 10-second lock wait. A multi-call
  triage/campaign workflow can take longer. There are no automatic mutation retries.
- The private Envelope Index schema and Mail scripting behavior can change with
  macOS. This candidate's regression tests use synthetic data; live account,
  draft, send and scheduling behavior needs separately authorized validation.

Version 0.4.2 addresses four independently reproduced review findings. An exact
Sent copy cannot hide a conflicting sibling: all overlapping candidates remain
visible, and distinct conflicting IDs block continuation. Send Later attachment
matches require `.emlx` filename/size/hash evidence, including readable evidence
of absence when no attachments are expected. Historical Sent checks remain
metadata-only (`metadata_matched`, `sha256_checked: false`), and do not establish
attachment bytes.

The index recognizes only explicit root mailbox aliases; custom paths such as
`Projects/Sentiment` and `Projects/Sent` remain `other`. Campaign audit includes
unknown-role metadata so a matching copy raises `mailbox_unverified` and blocks
continuation. Provider/localized/nested role mappings require separate native
validation. This conservative scan still stops at 100,000 rows. Required body
access failures stop `read`, body-search, `classify` and `triage` instead of
  returning a successful empty read. Empty readable content is valid.

## Direct-send migration in 0.4.3

The old boolean `--confirm-send` no longer sends. With no confirmation, `send`
prints the normalized sender and To/CC/BCC lists, exact subject/body, attachment
name/size/hash, and `intent_digest`, without loading Mail or writing send state:

```bash
mailctl send --sender sender@example.test --to recipient@example.test \
  --subject 'Synthetic example' --body-file /path/to/final-body.txt
```

Only after a human reviews and explicitly approves that full preview, repeat
the same command with `--confirm-send APPROVED_64_CHARACTER_DIGEST`. Any change
to the normalized envelope, list order, subject, body, attachment name, size or
bytes invalidates the confirmation. Body-file line endings are read as Python
UTF-8 text and displayed in the preview; Unicode content is not NFC-normalized.
Attachment paths are transport locations, not intent identity; the approved
filename and bytes are bound. Direct send permits at most 20 attachments
totaling 25 MiB. Visible compose retains its existing behavior.

Confirmed sends reserve a digest directory under
`~/Library/Application Support/apple-mail-cli/send-attempts/v1/` before invoking
automation. Its private `attempt.json` stores digest, timestamps and outcome;
attachment snapshots preserve approved filenames/bytes with mode 0400 inside
0700 directories. Records use mode 0600. Snapshots remain available because Mail
may read attachments asynchronously. Keep these artifacts private and retain
the reservation permanently; removing it disables duplicate-attempt protection.
There is no automatic reset or resend command. Even preparation failures retain
a reservation. Do not remove it, change content to obtain a fresh digest, or
retry after an uncertain outcome without auditing Mail and obtaining a new
human decision. This is a local per-user guard, not distributed idempotency.

Success reports `submission_accepted: true`, `outcome: accepted_by_mail`, and
`delivery_confirmed: false`, replacing the misleading `sent: true` field.
Mail may queue or process an accepted request; no queue persistence or remote
delivery is certified. Backend error, timeout, interruption or failure to
persist the final record leaves the intent blocked. Errors after invocation use
`send_outcome_unknown` with `retryable: false`; a repeated confirmed intent uses
`send_attempt_exists` before automation runs. Treat both as a stop for review.

## High-volume campaign flow

```bash
mailctl campaign dashboard campaign.json
mailctl campaign rebalance campaign.json \
  --output campaign-rebalanced.json \
  --start-at 2026-08-19 \
  --finish-by 2026-08-21
mailctl campaign validate campaign-rebalanced.json
mailctl campaign render campaign-rebalanced.json --output /private/render
mailctl campaign session-start campaign-rebalanced.json \
  --action schedule \
  --queue /private/render/queue.json \
  --output /private/render/session.json
mailctl campaign session-next /private/render/session.json --limit 20
```

The session returns only the next exact visible-UI operations. After each Mail
operation, record the attempt with `session-record`, then run `session-next` or
`session-reconcile`. An applied UI action remains `applied_unverified` until
Sent or Send Later proves the expected state. When Send Later's metadata index
omits sender or attachment fields, the audit reads only the matching local
`.emlx` message, ignores body content, and verifies attachment name, byte size,
and SHA-256 from the MIME payload.

Repeated audits reuse unchanged evidence from a private `0600` cache. The
cache stores only derived Message-ID, sender, and attachment evidence; it does
not store message bodies or local message paths. Audit results identify whether
an attachment mismatch came from count, filename, decoded byte size, or
SHA-256 instead of returning only a generic failure.

## Shared agent surface

The implementation lives once under `skills/apple-mail-cli/scripts/`. Codex
loads it from the personal plugin marketplace. Hermes loads the same skill
directory through `skills.external_dirs`; the installed `mailctl` wrapper
lets both agents call it from any working directory.

See:

- `skills/apple-mail-cli/SKILL.md` for agent operating rules;
- `skills/apple-mail-cli/references/cli-contract.md` for JSON, exit-code,
  confirmation, and privacy contracts;
- `skills/apple-mail-cli/references/campaign-manifest.md` for campaign format;
- `skills/apple-mail-cli/references/ubd-os-connector.md` for UBD OS setup and
  boundaries.

## Development verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_offline.py
# Optional synthetic JXA logic checks using an existing Node runtime:
node tests/test_backend_quality.js
```

The offline runner disables opt-in live Mail tests and excludes the HTTP
loopback test module. A full `unittest discover -s tests` also exercises a local
fake HTTP server; the live Mail module runs only with `APPLE_MAIL_CLI_LIVE_TESTS=1`
and can invoke permission checks. Never commit real mail, tokens, account data,
campaign outputs or evidence caches as fixtures.

The project owner states that they created this CLI and authorized NLINEA
project metadata. The original MIT notice (`Local developer`) is preserved;
the NLINEA maintainer label does not replace that copyright notice. No
competitor implementation is bundled in this project. See
[PROVENANCE.md](PROVENANCE.md).
