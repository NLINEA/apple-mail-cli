# Resumable visible-UI scheduling

Apple Mail has no reliable public scripting setter for Send Later timestamps.
The CLI therefore owns planning, integrity, checkpoints, and verification while
the agent operates the exact messages through Mail's visible UI.

## Start

For new scheduling, render first and bind the session to the queue:

```bash
mailctl campaign session-start campaign.json \
  --action schedule \
  --queue /private/render/queue.json \
  --output /private/render/session.json
```

For a changed manifest whose expected times differ from existing Send Later
items, use `--action reschedule`. For removal, use `--action cancel`. These
actions do not use a rendered queue.

Starting a session does not authorize Mail changes. Before a bulk operation,
show the campaign, count, sender, time range, daily maximum, exclusions, and
attachments and obtain current-turn confirmation.

## Operate and record

```bash
mailctl campaign session-next /private/render/session.json --limit 20 --owner codex-current-task
mailctl campaign session-record /private/render/session.json \
  --key row-0001 \
  --claim-token UUID-FROM-SESSION-NEXT \
  --result applied
mailctl campaign session-reconcile /private/render/session.json
```

Operate only the returned batch and retain its ownership token. Record `failed`
with a short `--note` only when you have confirmed that no change took effect.
For timeouts, a locked screen, or other uncertain outcomes, keep the claim and
audit first; never release it into a retry just because the UI stopped responding.
`skipped` is terminal and should be used
only for an intentional user-approved exclusion.

`applied` becomes `applied_unverified`; it is not success. Reconciliation is
the only path to `verified`:

- schedule/reschedule: one exact recipient-set and subject match is in Sent or
  Send Later at the expected time, with no sender or attachment issue. Missing
  Send Later index fields are supplemented from the matching read-only `.emlx`
  evidence by Message-ID;
- cancel: an item previously recorded as applied is absent from Send Later and
  has not already entered Sent.

Stop on `verification_failed`. Audit actual Mail state before retrying so a
timeout or closed window cannot create a duplicate.

## Recovery

Session files are private `0600` JSON, bound to the exact manifest SHA-256, and
updated atomically. `session-next` can be called after a restart; it first
reconciles verified state, then claims only safe pending or confirmed-failed
operations. Verification failures block the batch. Recipient, sender,
attachment, duplicate or replica conflicts also block it. The manifest, actual
queue fields (including BCC), and rendered EML bytes are rechecked on every resume.

Claims are private, cross-process locks shared across separate sessions. Version
0.6 acquires the campaign/key, sender + exact To/CC/BCC + subject intent, and any
observed Message-IDs together, independent of campaign name or requested time.
A second agent cannot claim an active matching operation. Claims never
expire into automatic retries. `session-status` exposes operations and active
claim tokens for recovery, including a crash between broker and session writes.
Audit actual Mail first, then record using the original token. Do not operate
Mail from `campaign next`: that command previews work but does not claim it.
Old sessions can still be read/reconciled, but a new result record requires a
claimed operation. Existing `applied_unverified` rows must be reconciled, not repeated.

`blocked_claims` identifies the owning session when another campaign has already
claimed the same identity. This is concurrent-operation protection, not permanent
suppression of all future mail to that contact. Reschedule/cancel sessions pin
observed Message-IDs and stop if the target identity changes. New groups acquire
locks in a fixed order; a crash during publication can leave conservative blocking
claims. Group release requires the original token. Never delete a claim to force
a retry. Upgrade/stop old workers before concurrent cross-campaign work: existing
legacy single-resource claims retain only their original campaign/key coverage.
