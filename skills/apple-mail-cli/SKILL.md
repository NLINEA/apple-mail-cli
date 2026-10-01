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
- Search older mail without scanning bodies: use `search`, then `thread <conversation_id>` for the full conversation.
- Triage a bounded inbox batch: use `triage`; save a private `--output` artifact only when it will feed a campaign dashboard.
- Reconcile Mail state: use `index list`, `index counts`, or `campaign audit`.
- Hand a redacted result to UBD OS: use `ubd campaign-event`, `ubd dashboard-event`, `ubd classify-event`, or the proposal-only UBD workflow.
- Prepare one message: use `compose` to create a visible draft.
- Prepare an outreach batch: validate a campaign, render fresh `.eml` files, then schedule only the audited missing rows.
- Preview one message: `send` without confirmation is offline and writes no send state.
- Send one message directly: approve the full preview in the current conversation, then pass its exact digest to `send --confirm-send SHA256`.
- Change a received message: use `mark`, `flag`, `move`, or `trash` only when explicitly requested.

Use `python3 scripts/mailctl.py <command> --help` for exact options. Read [campaign-manifest.md](references/campaign-manifest.md) before creating or operating a campaign manifest.

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
10. Perform those rows through Mail's visible Send Later UI using the computer-use capability. Record each attempt with `session-record`; `applied` means attempted, not verified.
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
  modification time. Any file change invalidates the entry. The cache contains
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
