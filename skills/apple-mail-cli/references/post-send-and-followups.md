# Post-send observations and follow-up review

These commands never mutate Mail and never authorize another send.

## Observe Sent for an operation

```bash
mailctl receipt OPERATION-UUID
mailctl search --state sent --account ACCOUNT-KEY --sender sender@example.com --exact-address
mailctl receipt-reconcile OPERATION-UUID --account-key ACCOUNT-KEY \
  --message-id '<exact-rfc-id@example.com>'
```

Use the exact index `account_key`, not the Mail account display name. The operator
selects the exact RFC Message-ID; a draft's numeric ID is not interchangeable.
Only new `send` receipts with saved intent can be reconciled. Older receipts
require manual inspection rather than invented intent or guessed message linkage.

The command reads all Sent replicas of the exact account/ID, bounded at 1,000 rows.
It checks sender, exact To/CC/BCC, subject, attachment names/count, and a Sent date
between operation creation (rounded to the second) and observation. Conflicting
dates or any mismatched replica block a positive observation. Missing results are
`sent_not_observed`, not proof of failed delivery. Index sync can lag.

`ok: true` / `sent_observed: true` means only that these metadata checks matched.
`verified`, `body_verified`, `attachment_bytes_verified`,
`operation_correlation_verified`, and `delivery_verified` stay false. Even an
identical message may originate from another operation. For full content/integrity,
inspect the exact message and attachments separately. Never claim recipient delivery
from Sent alone.

The private receipt gains `sent_observation`; its original dispatch `state` and
original `result` remain unchanged. `accepted`, `submitting`, and `unknown` do not
become replayable sends through reconciliation. Unknown sends must not be repeated
with a fresh operation ID to circumvent protection. Observations are timestamped
snapshots, not an ongoing monitor. No polling or scheduled automation is created.

## Build the review worklist

```bash
mailctl campaign followups campaign.json --account-key ACCOUNT-KEY \
  --after-days 7 --output /private/path/followup-review.json
```

`--after-days` accepts 1–365. `--as-of` optionally accepts a timezone-aware timestamp
for reproducible calculations; it evaluates the current index, not a historical
snapshot. The index scan refuses its 100,000-row boundary instead of claiming a
complete result. `complete` concerns this index/manifest scope, not server sync,
other accounts, archived external mail or proof that no reply exists. Only active
approved manifest rows are included; exclusions remain excluded.

| Status | What to do |
| --- | --- |
| `follow_up_review_due` | Review the complete conversation, opt-outs, complaints, bounces and other accounts before drafting. |
| `waiting` | Review date has not arrived. No automatic reminder is installed. |
| `related_activity_review` | Inspect possible replies, bounces, later sends or contact activity, including other conversations. |
| `scheduled_no_followup` | Original is still scheduled; do not chase an unsent message. |
| `not_sent_observed` | Inspect original state; absence is not failure or permission to resend. |
| `ambiguous_mail_review` / `insufficient_evidence_review` | Resolve duplicates, identity or missing dates/thread information first. |

Mail conversation IDs are only grouping hints, not RFC reply linkage or proof of a
human reply. The worklist contains no reply bodies, makes no final reply/opt-out
classification, and does not suppress contacts automatically. Related activity
prevents a simple due label. Every row is `requires_human_review: true` and
`send_authorized: false`. Full original body and attachment integrity remain manual
checks. Fresh confirmation of final recipients/body/attachments is still required
for any subsequent send.

Output is an optional new `0600` local JSON file; existing paths are refused. It
contains private addresses, subjects and Mail identifiers: never publish it, attach
it to UBD proposals or treat it as a redacted event. This release does not create
UBD work items, submit proposals, schedule follow-ups, or contact anyone.
