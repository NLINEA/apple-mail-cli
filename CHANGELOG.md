# 0.6.0 — 2026-10-10

## Public-source integration

- Integrates local 0.5/0.6 development into the public 0.4.3 baseline. The 0.5
  sections below describe that local development history, not previous GitHub releases.
- Preserves offline direct-send previews and `--confirm-send SHA256`, immutable
  attachment snapshots, persistent per-intent reservations and backend claims.
  A bare boolean confirmation is invalid. The backend additionally verifies
  observable draft fields and attachment references/bytes before dispatch;
  legacy raw `send_draft` requests cannot bypass the digest workflow.
- Preserves UTF-8 stdin transport, strict backend responses, literal Unicode
  search, bounded SQLite work and conservative unknown-mailbox handling.
- Acceptance still uses `submission_accepted: true` and
  `delivery_confirmed: false`. A receipt or Sent observation does not authorize retry.
- See VALIDATION.md for fresh synthetic results. The earlier independent 0.4.3
  review does not cover this integration; no live Mail mutation was exercised.

## New workflows

- Scheduling claims now acquire the legacy campaign/key, normalized sender +
  exact To/CC/BCC + subject identity, and observed Message-IDs together. Overlapping
  campaigns cannot concurrently claim the same identity. Claim conflicts expose
  the owning session; partial publications retain a recovery anchor. Unknown
  claims still never expire. Existing active old-version claims are respected for
  the same campaign/key; do not mix old workers with 0.6 workers for cross-campaign work.
- Reschedule/cancel sessions pin Message-IDs and stop when a different mail replaces
  the previously observed target. Protection is concurrent ownership plus current
  audit, not a permanent contact suppression registry.
- `triage` now reads groups of up to 20 messages per backend call; `read-batch`
  reads up to 100 explicitly selected IDs. One unavailable message does not lose
  its peers. A permission failure/timeout stops later chunks. Responses must cover
  exactly the requested IDs. No automatic retry; a whole-call timeout cannot recover
  that call's unreturned intermediate reads.
- `send` stores minimal immutable intent metadata and uses the same preflight
  snapshot for receipt and draft construction. `receipt-reconcile` adds explicit
  account/RFC-ID Sent metadata observations without changing dispatch/retry state.
  This does not verify body, attachment MIME bytes, operation correlation or delivery.
  Old receipts without intent are not guessed or backfilled.
- `campaign followups` creates a bounded, account-scoped, private human-review
  worklist from the current index. Related activity, duplicate identities and
  incomplete evidence require review. It never sends, schedules, creates UBD work
  or treats lack of local evidence as proof of no reply.
- Release verification uses synthetic fixtures; optional live read-only health/index
  checks are excluded from this public integration run. No live mail send,
  reschedule, cancellation, or deletion is part of testing.

# 0.5.1 — 2026-10-10

- Forced campaign rendering no longer deletes every EML in the output directory. It validates
  the owning queue/checkpoint, refuses unrelated filename collisions and unsafe paths, prepares
  the new files separately, and backs up only owned previous artifacts before replacement.
- Render publication is serialized. Caught publication failures restore replaced files; hard
  process termination is not an all-files atomic transaction, so queue/checkpoint integrity gates
  remain essential. A private backup remains available for inspection/recovery.
- EMLs removed from a new manifest remain on disk, unreferenced by the active queue; never schedule
  files by globbing the directory. No automatic backup or stale-file cleanup is performed.
- Explicit requests such as “I would like to unsubscribe” remain in the latest reply and are
  recognized. Common footer instructions and quoted history remain excluded; common English
  negations are not treated as an opt-out. This remains heuristic triage, not automatic suppression.
- Missing/malformed attachment metadata no longer crashes DMARC classification; it requires review.
- Triage retains successes and reports per-message failures. Incomplete batches return `ok: false`
  and exit 1, can still save a private partial artifact, and cannot feed a complete dashboard.
  Permission/platform/backend failures and automation timeouts stop further reads without discarding
  completed classifications. `complete` describes the selected batch, not the entire mailbox;
  `truncated` remains a separate mailbox scan boundary.
- This patch does not introduce the proposed 0.6 cross-campaign identity locks, Sent reconciliation,
  batched backend reads or follow-up work lists. No live Mail mutations are part of release testing.

# 0.5.0 — 2026-10-10

## Safety and correctness

- Compare actual queue fields, including private BCC, against the approved manifest.
- Revalidate queue/EML integrity and current audit blockers on session resume.
- Serialize session updates and claim campaign/key work across processes and sessions.
- Require a claim token when recording attempts; preserve uncertain claims for recovery.
- Metadata-only attachment matches remain unverified without content evidence.
- Detect contradictory complete same-state Message-ID replicas, including extra BCC.
- Distinguish Mail acceptance from independently observed Sent/delivery evidence.
- Read back mark/flag and guard draft fields before a confirmed send.
- Persist operation IDs and receipts; never automatically retry uncertain Mail mutations.
- Persist the exact UBD proposal and origin for idempotent, explicitly confirmed retries.
- Doctor probes actual Automation access, full query schema and all installed runtime modules.
- Cache fingerprints include ctime and a 15-minute TTL; fresh audits bypass cached evidence.
- Reject malformed allowed-hours policies and explain paired attachment hash/size differences.

## Workflow and performance

- Timestamp/row-ID keyset pagination, database-bound cursors, exact-address/account/Message-ID filters.
- Common quote/signature trimming and header signals for bilingual reply triage.
- Hash a shared campaign attachment once per validation; reuse dashboard audit inputs.
- New `resolve`, `draft show|verify|update`, `attachments list|save`, `receipt` commands.
- New `campaign diff`, `campaign explain`, `campaign audit --fresh`, `ubd retry-proposal`.

## Migration and limitations

- Restart old search cursors. Supply `--claim-token` from `session-next` to `session-record`.
- Use `session-status` to recover claims; record failed only after confirming no effect.
- Send requires explicit sender and matching readback. It returns accepted, never presumed delivered.
- Draft updates are full replacements and remain unsent. Unknown Mail scripting evidence blocks send.
- Receipts are private local data. All production agents must share the default operation state directory.
- Sent attachment metadata alone no longer certifies content integrity; this can leave audits incomplete.
- Mail still requires sequential visible-UI Send Later changes. This release cannot remove that bottleneck.
- No automatic follow-up sending, mailbox-wide watch service, account profile policy engine, or Sent MIME-store adapter is introduced.
- Tests use synthetic mutations plus an opt-in read-only live smoke test. Live sending, draft changes,
  attachment export, scheduling, and UBD proposal submission are not exercised as release tests.
