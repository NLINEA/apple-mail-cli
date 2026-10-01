# UBD OS Connector

The connector keeps UBD OS as the only operational source of truth. It uses
the existing UBD Agent API v1 and never connects to Supabase or a UBD OS
database directly.

## Boundary

```text
Apple Mail / local Envelope Index
  -> redacted reconciliation event
  -> optional UBD work-update proposal
  -> human review in UBD OS
  -> existing UBD OS command, receipt, and audit path
```

The connector can:

- produce a redacted classification event for one Mail message;
- produce a redacted campaign reconciliation event;
- produce a redacted campaign dashboard event containing only status, issue,
  account-volume, quota, and triage counts;
- read Agent API capabilities and bounded work items;
- submit a revision-bound campaign or message-classification work-detail
  proposal for human review.

It cannot:

- send, schedule, move, flag, or delete mail through UBD OS;
- directly change a formal UBD OS work item;
- access Supabase, user cookies, or browser sessions;
- copy message bodies, subjects, addresses, attachment content, or raw Mail
  identifiers into an event;
- follow HTTP redirects with an Agent API bearer token.

## Environment

Set the API origin and one-time-visible token in the current terminal session.
Do not store the token in a manifest, repository, shell history, or generated
artifact.

```bash
export UBD_AGENT_API_ORIGIN='https://os.example.invalid'
export UBD_AGENT_TOKEN='token supplied by UBD OS'
```

The origin above is a nonfunctional placeholder. Configure the authorized
service's real origin privately; the CLI does not assume a production endpoint.

The API token must have only the exact business units and tools needed. A
proposal needs `system.capabilities`, `work.list`, `work.get`, and
`work.propose_update`; UBD OS requires the two work-read capabilities whenever
proposal access is granted. The CLI refuses a capabilities response that
claims direct database, send, publish, payment, or delete authority.

## Read-only event workflow

Campaign reconciliation:

```bash
python3 scripts/mailctl.py ubd campaign-event campaign.json \
  --business-unit-id '<business-unit-uuid>' \
  --work-item-id '<work-item-uuid>' \
  --output /private/path/mail-reconciliation.json
```

Campaign dashboard with an optional private triage artifact:

```bash
mailctl ubd dashboard-event campaign.json \
  --triage /private/path/triage.json \
  --business-unit-id '<business-unit-uuid>' \
  --work-item-id '<work-item-uuid>' \
  --output /private/path/mail-dashboard.json
```

Message classification:

```bash
python3 scripts/mailctl.py ubd classify-event 12345 \
  --mailbox INBOX \
  --business-unit-id '<business-unit-uuid>' \
  --work-item-id '<work-item-uuid>'
```

An output artifact is created with mode `0600` and will not overwrite an
existing file. Events contain only aggregate counts, a SHA-256 message
reference, fixed classification/action codes, exact UBD scope, and a stable
event UUID. They exclude addresses, subjects, bodies, Mail account names,
mailbox names, result rows, and attachments.

## UBD work workflow

Always discover capabilities before reading work:

```bash
python3 scripts/mailctl.py ubd capabilities
python3 scripts/mailctl.py ubd work-get \
  --business-unit-id '<business-unit-uuid>' \
  --work-item-id '<work-item-uuid>'
```

To attach a campaign reconciliation to one existing work item, use its current
revision and a UUID idempotency key:

```bash
python3 scripts/mailctl.py ubd propose-campaign-audit campaign.json \
  --business-unit-id '<business-unit-uuid>' \
  --work-item-id '<work-item-uuid>' \
  --expected-revision 3 \
  --idempotency-key '<new-uuid>' \
  --confirm-proposal
```

This stores only a proposal. It preserves existing human-authored work detail
and replaces only the section between `[UBD Mail Connector]` markers. The CLI
stops on a stale revision, mismatched business unit, oversized detail, missing
capability, redirect, uncertain API result, or any response suggesting that the
proposal was applied without human review.

The equivalent workflow for a single locally classified message is:

```bash
mailctl ubd propose-classification 12345 \
  --mailbox INBOX \
  --business-unit-id '<business-unit-uuid>' \
  --work-item-id '<work-item-uuid>' \
  --expected-revision 3 \
  --idempotency-key '<new-uuid>' \
  --confirm-proposal
```

Only the classification code, confidence, fixed next-action code, counts,
SMTP status codes, hashed Message-ID reference, and timestamps enter the
proposal. The original sender, recipient, subject, body, account, mailbox, and
attachments remain local.

The equivalent dashboard proposal is:

```bash
mailctl ubd propose-dashboard campaign.json \
  --triage /private/path/triage.json \
  --business-unit-id '<business-unit-uuid>' \
  --work-item-id '<work-item-uuid>' \
  --expected-revision 3 \
  --idempotency-key '<new-uuid>' \
  --confirm-proposal
```

Dashboard proposals include daily sent/scheduled/total counts, over-limit day
flags, campaign status and issue counts, and classification counts. They never
include the triage artifact path or any message-level row.

Reuse an idempotency key only for the exact same proposal content. Do not retry
401 or 403 responses. For timeout or connection failure, read the work and its
pending proposals in UBD OS before deciding whether to retry.
