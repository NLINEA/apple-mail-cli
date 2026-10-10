---
name: apple-mail-cli
description: Use when operating local macOS Apple Mail or reconciling outreach with UBD OS. Provides structured JSON for full-index and conversation search, bilingual reply and bounce triage, Sent and Send Later reconciliation, quota-aware campaign rebalancing, resumable visible-UI scheduling, visible drafts, confirmed sending, dashboards, and redacted proposal-only UBD OS handoff. Do not use for webmail or non-Apple email clients.
license: MIT
metadata:
  hermes:
    tags: [Apple Mail, email, macOS, outreach, UBD OS, CLI]
---

# Apple Mail CLI

Use the bundled CLI instead of ad hoc Mail database queries or fragile text replacement. It combines Apple Mail's supported automation interface, read-only local Mail evidence, deterministic campaign rendering, and carefully checkpointed UI scheduling.

## Command

Prefer the installed command from any working directory:

```bash
mailctl <command> [options]
```

If `mailctl` is unavailable, resolve the fallback relative to this skill directory:

```bash
python3 scripts/mailctl.py <command> [options]
```

Start with `capabilities`, then `doctor`. The first command is offline and exposes the machine-readable safety contract. Apple Mail or macOS may ask for Automation, Accessibility, or Full Disk Access depending on the next command. Respect a denied permission; never bypass it.

Read [cli-contract.md](references/cli-contract.md) when wiring a new agent,
automation, or calling environment.

## Choose the workflow

- Inspect recent mail: use `accounts`, `mailboxes`, `list`, `read`, or `classify`.
- Read explicit selections with `read-batch --id ... --id ...`; triage uses the same grouped-read path by default. Account filters here use Mail display names.
- Search older mail without scanning bodies: use `search`, then `thread <conversation_id>` for the full conversation.
- Triage a bounded inbox batch: use `triage`; save a private `--output` artifact only when it will feed a campaign dashboard.
- A triage batch with failures returns successful `items` plus `errors`, `not_attempted_ids`, and `complete: false`; its private partial artifact is for local recovery, not a dashboard. Inspect failed messages individually. A permission failure or Mail timeout stops further reads; do not loop around it.
- Reconcile Mail state: use `index list`, `index counts`, or `campaign audit`.
- Observe a direct send with `receipt-reconcile UUID --account-key ... --message-id '<...>'`. This adds metadata evidence only, never delivery proof or permission to retry.
- Build a private human-review list with `campaign followups MANIFEST --account-key ... --after-days 7 --output ...`; it never sends, schedules, or creates UBD work.
- Hand a redacted result to UBD OS: use `ubd campaign-event`, `ubd dashboard-event`, `ubd classify-event`, or the proposal-only UBD workflow.
- Prepare one message: use `compose` to create a visible draft.
- Inspect or verify an open draft: use `draft show` or `draft verify`; use `draft update` only with the exact returned revision and `--confirm-change`. Updates replace the supplied draft fields, not a patch or merge.
- When editor attachment readback is unavailable, use the saved-copy human-review workflow below. It does not enable automatic send or establish a link to the open editor.
- Locate a message by RFC Message-ID: use `resolve` in an exact account/mailbox. Numeric outgoing draft IDs and RFC Message-IDs are different identifiers.
- Inspect/save attachments: use `attachments list` and `attachments save --attachment-id ... --output ...`. Existing output files are never overwritten.
- Explain one campaign row: use `campaign explain --key ...`; compare approved revisions with `campaign diff` before changes.
- Prepare an outreach batch: validate a campaign, render fresh `.eml` files, then schedule only the audited missing rows.
- Preview one message: `send` without confirmation is offline and writes no send state.
- Send one message directly: approve the full preview in the current conversation, then pass its exact digest to `send --confirm-send SHA256`.

### Saved-copy human review (no automatic send)

1. Read `draft show ID`. With authorization to save that exact draft, run
   `draft save ID --expected-revision REVISION --confirm-change --operation-id UUID`.
   The revision covers readable editor fields only, not unavailable attachments.
   Save acceptance is not proof of an identified stored copy, send or delivery.
2. Run `draft saved-list --account ACCOUNT --subject 'EXACT SUBJECT'`. These are
   metadata candidates only. Never identify an open draft solely by subject.
   Saving may replace both the numeric stored ID and RFC Message-ID; refresh them.
3. Select an exact stored RFC Message-ID in the exact account, then run
   `draft review-saved '<MESSAGE-ID>' --account ACCOUNT --sender ADDRESS --to ADDRESS
   --subject 'SUBJECT' --body-file /absolute/body.txt --attachment /absolute/file
   --output /absolute/new-private-review.json`, including every intended CC/BCC
   and attachment. An explicit sender is required. This reads one saved MIME copy,
   checks its identity twice and hashes decoded attachment bytes. It never sends.
4. Read every `issues` entry. `attachment_bytes_verified` and `envelope_verified`
   describe only this saved copy; `open_draft_link_verified` remains false.
   Empty/missing/multiple plain-text parts or changed body text need review.
   HTML can differ from plain text and always requires visual inspection in Mail.
5. Before a separate send decision, rerun the same review with
   `--expected-review-digest DIGEST`; changed source, identity or expectations
   invalidate old evidence. This is a point-in-time check, not a draft lock.
   The review digest is NOT a `--confirm-send` token, and neither review status
   constitutes user approval. Obtain fresh explicit approval of the exact visible
   message after inspection. Do not auto-send, create a duplicate, change the old
   intent, remove guards or retry an uncertain prior send through this workflow.

Review JSON contains private message bodies and addresses; keep it local and
never publish or send it to UBD OS. The workflow intentionally ends at human
review; it provides no `send-draft` command and does not certify delivery.
- Change a received message: use `mark`, `flag`, `move`, or `trash` only when explicitly requested.

Use `python3 scripts/mailctl.py <command> --help` for exact options. Read [campaign-manifest.md](references/campaign-manifest.md) before creating or operating a campaign manifest.
Read [post-send-and-followups.md](references/post-send-and-followups.md) before
receipt reconciliation or follow-up review. These index workflows require exact
`account_key` values rather than Mail account display names.

## UBD OS handoff

Read [ubd-os-connector.md](references/ubd-os-connector.md) before using any
`ubd` command.

- UBD OS remains the only operational source of truth.
- Prefer local `ubd campaign-event`, `ubd dashboard-event`, or `ubd classify-event`; all are read-only
  and exclude message bodies, subjects, addresses, attachments, Mail account
  names, and raw result rows.
- Use `ubd capabilities`, `ubd work-list`, and `ubd work-get` only through the
  existing server-to-server Agent API. Never use Supabase credentials or a
  browser session.
- `ubd propose-campaign-audit` stores a revision-bound proposal only. It
  requires `--confirm-proposal`, an exact work item, its current revision, and
  an idempotency UUID. The proposal must still be accepted by a human in UBD
  OS.
- `ubd propose-classification` applies the same boundary to one redacted
  message classification. Proposal access requires `system.capabilities`,
  `work.list`, `work.get`, and `work.propose_update`, matching the current UBD
  OS capability dependency contract.
- `ubd propose-dashboard` attaches aggregate campaign, account-volume, quota,
  and triage counts to one work item under the same revision and human-review gates.
- Stop if the API policy claims send, publish, payment, delete, or direct
  database authority; the Mail connector deliberately rejects those powers.
- Never use a UBD proposal as authorization to send or schedule mail.

## Inbox and reply triage

1. Run `triage --mailbox INBOX --limit 30` for a body-free summary, or `list --mailbox INBOX --limit 30` when selecting individual messages.
2. Read likely replies with `read <id>`.
3. Run `classify <id>` for bounce, auto-reply, DMARC, complaint, or human-reply signals.
4. Separate actionable human replies, better-contact auto-replies, bounces, DMARC reports, and routine acknowledgements.
5. Do not delete or move anything unless the user asked. For bounce cleanup, first confirm the failure is captured in the user's tracking artifact, then use `trash`.

Use `--query` for sender/subject search. Add `--search-body` only when necessary because it reads more sensitive content and is slower. If `truncated` is true, explain the scan boundary and retry with a larger `--scan-limit` when the requested mail may be older.

For older messages, prefer `search`; it reads the complete local metadata index,
supports opaque pagination, and never searches message bodies. Use the returned
`conversation_id` with `thread` before drafting a contextual reply.
Read [search-and-triage.md](references/search-and-triage.md) for filters,
pagination, and private triage artifacts.

## Campaign workflow

1. Generate every message independently from structured fields. Never clone a previous company's prose with Find/Replace.
2. Put final recipients, sender, subject, full body, schedule time, and exact attachments in a JSON manifest.
3. Run `campaign validate <manifest>`. Fix every error before touching Mail.
4. Run `campaign plan <manifest>` and review exclusions, daily volume, time gaps, and duplicate recipients.
5. Run `campaign dashboard <manifest>` to include actual account-wide Sent and Send Later volume. Add `--triage <artifact>` when reply and bounce aggregates are needed.
6. If the requested finish date or quota is not met, run `campaign rebalance` to create a new manifest. Never overwrite the approved source manifest. Review and approve the new manifest revision before scheduling.
7. Run `campaign render <manifest> --output <directory>` to create deterministic `.eml` files and a queue manifest.
8. Run `campaign audit <manifest>` before scheduling. Operate only rows reported as `missing`; never re-create `sent` or `scheduled` rows.
9. After explicit bulk-schedule confirmation, run `campaign session-start` for `schedule`, `reschedule`, or `cancel`. Use `session-next` to obtain only the next exact visible-UI batch.
10. Perform those rows through Mail's visible Send Later UI using the computer-use capability. Record each attempt with `session-record --claim-token <returned token>`; `applied` means attempted, not verified. Use a distinct `session-next --owner` per agent. Never treat `campaign next` as ownership; it is a read-only preview.
11. Run `session-reconcile` or `session-next` after every batch. Only an operation confirmed by read-only Mail evidence becomes `verified`. Stop on `verification_failed`.
12. Finish with `campaign dashboard` and `campaign audit --csv <path>`; report scheduled, sent, missing, duplicate, wrong-time, wrong-recipient, wrong-sender, sender-unverified, attachment-unverified, and wrong-attachment counts.

Apple Mail's public scripting dictionary does not expose a reliable Send Later timestamp setter. Do not write to Mail's SQLite database to simulate scheduling. Use the visible Send Later UI for schedule, reschedule, and cancellation, with the CLI providing the plan, rendered messages, and read-only verification.
Read [scheduling-sessions.md](references/scheduling-sessions.md) before running
or resuming a visible-UI bulk scheduling session.

`campaign next` refuses a changed manifest, stale or edited queue row, moved
`.eml`, altered `.eml` digest, duplicate queue key, or queue path outside its
private render directory. `bcc` stays in the private `0600` queue rather than
the MIME headers; apply and verify it visibly before scheduling.

`campaign next` updates the private checkpoint with verified completed and
pending keys. Scheduling sessions add an append-only attempt history and use
atomic `0600` updates. A session never treats a closed compose window or a
successful click as delivery evidence.

`campaign render --force` replaces only paths owned by a valid previous queue
for the same campaign. It stages a new render and creates a private backup of
the previous owned artifacts; unrelated files and unreferenced old EMLs remain
untouched. Unsafe paths, foreign metadata and filename collisions are refused.
Use only the active queue, never every EML in the folder. Do not delete backups
or stale files without an exact, separately approved cleanup scope. Caught
publication failures are rolled back; after a hard interruption, verify the
queue/checkpoint and use the returned/private backup for recovery before resuming.

Before changing existing scheduled mail, run `campaign reschedule-plan` or `campaign cancel-plan`. Operate exactly those visible Mail items, then rerun `campaign audit` before continuing.

## Required preflight checks

- Verify the exact sender identity.
- Require at least one recipient and reject malformed or duplicate addresses.
- Check that subject and body are non-empty.
- Check required signature text when configured.
- Preserve paragraph spacing and require the body to end with a blank line before attachments when configured.
- Verify each attachment path, filename, byte size, and SHA-256 when supplied.
- Reject duplicate campaign keys, duplicate recipient-plus-subject pairs, invalid schedule timestamps, or gaps below the configured minimum.
- Interpret daily limits and allowed hours in `policy.timezone`; reject invalid IANA timezone names.
- Compare the exact normalized To, CC, and BCC sets. Any extra, missing, or shifted recipient blocks continuation.
- Count actual account-wide Sent and Send Later volume before accepting a rebalance plan.
- Honor `manual`, `exclude`, `do_not_send`, and risk exclusions.
- Compare with Sent and Send Later before every mutation.
- Treat missing sender evidence or any extra, missing, or count-mismatched
  attachment as unsafe; never assume absent index metadata proves a match.
- For Send Later only, `campaign audit` may supplement missing index fields by
  reading the matching local `.emlx` by exact Message-ID. It ignores body
  content and verifies attachment filenames, decoded byte sizes, and SHA-256.
  Missing fallback evidence becomes `attachment_unverified`, not
  `wrong_attachment`; both remain unsafe until resolved.
- Repeated Send Later audits may reuse a private `0600` evidence cache keyed by
  an opaque path token and the file's device, inode, byte size, and nanosecond
  modification and change times, with a 15-minute maximum evidence age. Use `campaign audit --fresh` to bypass reuse. The cache contains
  no body text or local message paths; do not copy or publish it because sender
  and attachment evidence remains private Mail data.
- When `wrong_attachment` is reported, inspect
  `verification.attachments.mismatch_fields` to distinguish `count`,
  `filename`, `size_bytes`, and `sha256` differences before deciding what to
  fix. Never weaken exact verification just to resume a batch.

## Safety rules

- Treat `doctor`, `accounts`, `mailboxes`, `list`, `search`, `thread`, `read`, `classify`, `triage`, all `index` commands, `campaign validate`, `campaign plan`, `campaign dashboard`, `campaign rebalance`, `campaign render`, `campaign audit`, all `campaign session-*` commands, `ubd campaign-event`, `ubd dashboard-event`, `ubd classify-event`, `ubd capabilities`, `ubd work-list`, and `ubd work-get` as read-only with respect to Mail. Rendering, rebalancing, checkpoints, sessions, triage artifacts, evidence caching, and event export write only private local artifacts.
- Treat `ubd propose-campaign-audit` as a UBD OS proposal mutation, not a formal work change. Execute only when requested, and verify the response says `applied: false` and `requiresHumanReview: true`.
- Treat `mark`, `flag`, `move`, `trash`, `compose`, visible Send Later actions, rescheduling, and cancellation as Mail mutations. Execute only when requested. `mark`, `flag`, and `move` require `--confirm-change`; `trash` requires `--confirm-trash`.
- Prefer visible drafts. A request to write, draft, prepare, or compose never authorizes sending.
- Before direct send, use its offline preview to show normalized sender and To/CC/BCC, exact subject/body and attachment name/size/hash. Obtain explicit confirmation in the current conversation, then pass `--confirm-send SHA256` with that preview's exact digest. The old boolean flag is invalid. Any drift requires a new preview and human approval.
- A direct-send reservation is retained before automation. Never automatically retry a timeout/error/interruption, remove its private attempt guard, or change content solely to evade the guard. Inspect Mail and the private record before obtaining a new human decision. `submission_accepted` means accepted by Mail; neither queued persistence nor delivery is confirmed.
- Direct send also requires matching observable draft readback and attachment references/bytes. Missing or changed evidence leaves the visible draft unsent. This is not MIME-delivery verification.
- Give each Mail mutation a stable `--operation-id` UUID. Repeating an identical completed request returns its private receipt; in-flight/unknown outcomes block automatic retry. Inspect `receipt <uuid>` and actual Mail state first. Never generate a new UUID merely to bypass an uncertain result.
- `ubd retry-proposal <uuid> --confirm-proposal` reuses the saved endpoint, exact proposal body and idempotency key, after current confirmation and capability checks. This is proposal-only and never authorizes a Mail retry.
- Receipts may contain private draft text/metadata or the existing UBD work detail. Keep them local under `~/Library/Application Support/apple-mail-cli`; never publish them or include them in UBD evidence events.
- Scheduling claims are shared across processes and sessions and do not expire into automatic retries. Use `session-status` to recover claim tokens after a restart, then audit before recording. Record `failed` only when no effect is confirmed; otherwise keep the claim and reconcile. Do not bypass another agent's active claim.
- Version 0.6 claims also cover sender + exact To/CC/BCC + subject across campaign names, plus observed Message-IDs. Inspect `blocked_claims` instead of bypassing a conflict. Reschedule/cancel targets pin Message-IDs; changed identity stops the session. Use one current installed version for all workers; legacy active claims do not provide cross-campaign identity coverage. Claims are active-operation guards, not permanent contact suppression.
- `session-next` rechecks fresh Mail evidence after acquiring claims and before issuing the batch. An unsafe/failed recheck or changed state/Message-ID blocks the new batch and releases only its unissued claims, never prior in-flight work. Reconcile before resuming; do not blindly retry. Manual edits and later synchronization still require visible pre-action checks and post-action reconciliation.
- Sent receipt observations keep body, attachment-byte, operation-correlation and delivery verification false; the original dispatch state remains unchanged. Old receipts without saved intent require manual inspection, never a guessed match.
- Follow-up worklists are private metadata review aids, not send queues. Inspect related activity, opt-outs/complaints/bounces and other accounts before drafting. `follow_up_review_due` is not proof of no reply or approval to contact. Do not submit these raw private artifacts to UBD.
- Before a bulk schedule, show campaign, count, time range, daily maximum, exclusions, sender, and attachment summary and obtain explicit confirmation. Schedule only the approved manifest revision.
- If the screen locks, Mail becomes unresponsive, permission is denied, or verification differs from the plan, stop the current mutation, audit actual Mail state, and resume only missing rows.
- Never mark a scheduling session `verified` from UI state alone. `applied_unverified` must be reconciled against Sent or Send Later.
- Deduplicate by the real `Message-ID` when assessing whether multiple Mail index rows represent multiple sends. Do not count iCloud cache replicas as separate delivery attempts.
- The local index and `.emlx` evidence adapters are strictly read-only. Never update, delete, vacuum, or repair Mail's database or message store.
- Avoid exposing full message bodies, addresses, or account details beyond the user's task.

## State interpretation

Use aggregate mailbox aliases `INBOX`, `SENT`, `DRAFTS`, `TRASH`, and `JUNK`. A custom mailbox can be a slash-delimited path; add `--account` when ambiguous.

For campaign status, use this precedence:

1. `duplicate` when more than one distinct Message-ID matches the campaign identity; stop even if one copy is already in Sent.
2. `sent` when exactly one matching unique Message-ID is in Sent.
3. `scheduled` when exactly one matching unique Message-ID is in Send Later with the expected timestamp.
4. `wrong_time` when exactly one copy is in Send Later at another timestamp.
5. `missing` when no matching Sent or Send Later item exists.

Use recipient plus exact subject as the default campaign identity. Add a unique campaign token to the subject when the business wording permits it; otherwise use the manifest key only for local checkpoints.

An overlapping subject and recipient with different To, CC, or BCC sets is
`missing` with `wrong_recipients` when it is the only candidate. Beside another
distinct copy, it becomes `duplicate` with `wrong_recipients`; an exact copy
never hides the conflict. Both stop continuation. Unknown mailbox-role matches
raise `mailbox_unverified` and cannot prove delivery or a safe missing slot.

Scheduled attachment matches require readable `.emlx` filename, size and hash
evidence; index names/counts alone are `attachment_unverified`. Sent attachment
checks are explicitly metadata-only and do not prove bytes. Required body-read
failures stop reading/classification/triage; do not interpret them as empty mail.
