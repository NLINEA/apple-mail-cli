# Campaign manifest

Use UTF-8 JSON. Times must include an offset, for example `2026-08-18T09:02:00+08:00`.

```json
{
  "schema_version": 1,
  "campaign_id": "kurio-2026-p3",
  "sender": "partners@example.com",
  "defaults": {
    "attachments": [
      {
        "path": "/absolute/path/Media-Kit.pdf",
        "name": "Media-Kit.pdf",
        "size_bytes": 3250585,
        "sha256": "optional-lowercase-hex"
      }
    ],
    "required_signature": "KURIO Partnerships",
    "require_blank_line_before_attachment": true
  },
  "policy": {
    "timezone": "Asia/Hong_Kong",
    "minimum_gap_minutes": 7,
    "daily_maximum": 120,
    "allowed_hours": ["08:30", "22:30"],
    "excluded_risks": ["do_not_send", "highest_risk"]
  },
  "messages": [
    {
      "key": "p3-0001",
      "company": "Example Company",
      "to": ["marketing@example.com"],
      "cc": [],
      "bcc": [],
      "subject": "Media partnership opportunities for Example Company",
      "body": "Dear Example Company team,\n\nComplete independently generated body.\n\nBest regards,\nExample Author\nExample Team\n\nE: partners@example.com\nT: +852 0000 0000\nW: example.com\n\n",
      "schedule_at": "2026-08-18T09:02:00+08:00",
      "risk": "confirmed",
      "manual": false,
      "exclude": false
    }
  ]
}
```

## Rules

- Keep `campaign_id` and every message `key` stable across retries.
- Use only letters, digits, dots, colons, underscores, and hyphens for
  `campaign_id` and `key`; each is limited to 120 characters.
- Use arrays for `to`, `cc`, and `bcc`.
- Put the final body in each row. Do not depend on replacing a previous company's name.
- Top-level `sender` and `defaults.attachments` apply unless a message overrides them.
- Set `manual`, `exclude`, or risk values listed by `policy.excluded_risks` to prevent rendering or scheduling.
- Specify attachment name, size, and SHA-256 when the campaign requires an exact approved file.
- A message without `schedule_at` can be rendered as a draft but cannot pass a scheduling plan.
- `policy.timezone` must be an IANA timezone such as `Asia/Hong_Kong`. Daily
  limits and allowed hours are calculated after timestamps are converted into
  that timezone; each timestamp offset still identifies the exact instant.
- Overnight allowed-hour windows are supported, for example `["22:00", "02:00"]`.
- Every top-level or per-message sender must be a valid address. Header control
  characters are rejected before MIME rendering.

## Audit matching

The audit first narrows by overlapping normalized primary To recipient plus
exact subject, then deduplicates database rows by real Message-ID. It preserves
every overlapping candidate and requires exact normalized To, CC, and BCC sets.
A single non-exact candidate becomes `missing` with `wrong_recipients`; distinct
IDs become `duplicate`, even when one copy is exact. Any recipient drift blocks
continuation. It compares the Send Later timestamp when `schedule_at` is present.
Same-subject mail with no primary To overlap is not campaign identity evidence.

Sender and attachment checks fail closed. Missing sender evidence becomes
`sender_unverified`, and a proven wrong sender becomes `wrong_sender`. Apple
Mail can omit Send Later attachment rows from the metadata index, so the audit
uses the matching local `.emlx` to verify MIME filename, decoded byte size, and
SHA-256. Missing evidence becomes `attachment_unverified`; a proven missing,
extra, renamed, size-changed, hash-changed, or count-mismatched attachment
becomes `wrong_attachment`. Scheduled mail requires readable `.emlx` byte
evidence even when index names/counts agree or the expected attachment list is
empty. Historical Sent checks can be `metadata_matched` with `sha256_checked:
false`; that weaker result does not prove byte equality. Matching mail in an
unknown/custom/nested mailbox role adds `mailbox_unverified` and blocks
continuation. Reconciliation also stops if the bounded Sent/Send Later/unknown
index query reaches 100,000 rows, because that result may be truncated.

Each result includes `verification.attachments.mismatch_fields`. Its values are
drawn from `count`, `filename`, `size_bytes`, and `sha256`, allowing an operator
to distinguish a stale manifest from a missing or changed attachment without
exposing attachment payloads. Repeated audits can reuse a private evidence
cache only while the underlying `.emlx` fingerprint is unchanged.

## Rendered output

`campaign render` creates:

- one `.eml` per active message;
- `queue.json` with queue index, source key, To/CC/BCC recipients, subject,
  schedule time, attachment evidence, source fingerprint, file path, and EML
  SHA-256;
- `checkpoint.json` initialized for resumable scheduling;
- a manifest SHA-256 binding the queue and checkpoint to the exact source
  bytes.

The render directory is mode `0700`; `.eml`, queue, checkpoint, UBD event, and
audit CSV artifacts are mode `0600`. BCC addresses are deliberately excluded
from MIME headers and retained only in the private queue so an imported raw
message cannot expose them. Apply BCC in Mail's visible composer and verify it
before scheduling.

Keep these files as operational artifacts. Rerun `campaign audit` after each
scheduling batch and use `campaign next` for the missing set. It refuses a
changed manifest, stale source fingerprint, edited EML, duplicate key, missing
artifact, or EML path outside the queue directory; rerender instead of
overriding an integrity failure.

`campaign next` updates `checkpoint.json` with index-verified completed keys,
pending keys, and the last audit summary. For long or high-volume work, create
a separate `campaign session-start` artifact and use `session-next`,
`session-record`, and `session-reconcile` so UI attempts can pause and resume
without being mistaken for verified state.

## Quota-aware rebalance

`campaign rebalance` creates a new private manifest and refuses to overwrite
the approved source. It locks already-sent rows, excludes current campaign
rows from account baseline volume, counts other Sent and Send Later messages,
preserves every valid existing slot, and moves only rows that violate the new
window, gap, allowed hours, or daily maximum.

```bash
mailctl campaign rebalance campaign.json \
  --output campaign-rebalanced.json \
  --start-at 2026-08-19 \
  --finish-by 2026-08-21
```

Use `--offline` only for isolated planning where ignoring the real account
volume is intentional. Validate, review, and explicitly approve the generated
manifest before any visible Mail scheduling action.
