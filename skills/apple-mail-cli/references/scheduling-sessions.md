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
mailctl campaign session-next /private/render/session.json --limit 20
mailctl campaign session-record /private/render/session.json \
  --key row-0001 \
  --result applied
mailctl campaign session-reconcile /private/render/session.json
```

Operate only the returned batch. Record `failed` with a short `--note` when the
screen locks, a picker value differs, Mail becomes unresponsive, or the visible
item cannot be identified exactly. `skipped` is terminal and should be used
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
reconciles verified state, then returns only pending, failed, or
verification-failed operations. A changed or missing manifest invalidates the
session rather than silently continuing.
