# Security and validation limits

Version 0.6.0 is experimental. The earlier bounded independent review covers
only 0.4.3; it does not certify this integration. Fresh 0.6.0 regression results
are in VALIDATION.md. One native draft-save and saved-copy review is recorded
there; other native mutation behavior and delivery remain unverified, with no
response-time commitment.

For a security report, use this repository's Security tab and **Report a
vulnerability** when GitHub private vulnerability reporting is available. Its
enablement is a publication setup step and is not asserted by this source
tree. If the button is absent, no verified private reporting channel is
available; avoid disclosing the vulnerability in a public issue. The maintainer
must [enable and verify the channel](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
when publishing. Do not post real messages, addresses, attachment data, API
credentials, send journals or personal account paths in public issues or examples.

## Intended protection

The CLI assumes a trusted local user, trusted executable source and a human
approving the displayed operation. Direct-send digest confirmation catches
accidental content/recipient/attachment drift. Its private per-user persistent
reservation and backend claim prevent the same local intent from being
automatically reissued after acceptance, error or interruption. This does not
authenticate a human, defend against code running as the same user, or supply
distributed idempotency. Deleting state, changing intent or using another
user/host bypasses that local protection. Do not do so to retry uncertain mail.

Source attachments are snapshotted and hashed before invocation, then checked
again by the backend before its claim and immediately before send. The backend
also requires matching observable draft fields and attachment references.
Mode 0400 makes snapshots read-only in
normal use; the owner can still change permissions. Mail's actual recipient,
body transformation, attachment readback and asynchronous behavior require
native tests. A passed protocol fixture does not establish final Mail content.

Mail send acceptance is not delivery or proof of Outbox persistence. The CLI
does not automatically retry mutations. Uncertain send results retain guards;
inspect actual Mail state and obtain a new human decision. Historical Sent
attachment name/count checks do not prove bytes. Unknown mailbox roles and
missing scheduled byte evidence block campaign continuation. Summary metadata
search does not provide full-body or remote-server coverage.

## Privacy and permissions

Raw reads, send previews, rendered campaigns, attachment snapshots and local
evidence caches are private. The digest attempt journal contains only digest,
timestamps and state, but its attachment snapshots contain approved file
bytes/names. Separate operation receipts may include private draft text and
metadata, saved send-intent metadata, or UBD work detail. Follow-up worklists
and Sent observations are also private. Never publish these artifacts.
Keep the persistent reservation after reviewing any snapshot-retention cleanup;
no cleanup automation is provided. CLI argument values can appear in process
listings and shell history. `--body-file` avoids putting the body in those flags.
Python-to-JXA payloads use UTF-8 stdin. This is not protection from the same user.

Core Mail commands use existing Automation permission for the launching app
to control Mail. Index/store reads require that app/runtime's existing Full
Disk Access. Visible Send Later control additionally requires Accessibility.
Offline capabilities and send previews need no Mail/FDA/Accessibility grants.
The CLI never grants, resets or bypasses these permissions. Core use needs no
provider credentials or external API. UBD OS access is separate and optional.

## Additional native validation

Independent review of the 0.4.3 protocol passed. Native Mail validation remains
separate, explicitly authorized work. The following paragraph records the older
0.4.3 checks; see VALIDATION.md for the later bounded saved-draft review.
Existing read-only index access and a
bounded synthetic-subject metadata query succeeded; no real draft, send,
delete, move, mark or flag was exercised. Mail was stopped during the account
preflight, so sender identity and existing Automation authority remain
unverified. No new permission was granted and no Mail startup was attempted.

A minimal staged native check is:

1. Start Mail normally only with the owner's authorization of normal startup
   effects. Query existing Automation authority without prompting, then read
   `accounts` only when permitted. Stop on denial; do not grant or bypass
   permissions as part of this check. Verify a unique enabled own sender and
   an owner-confirmed self-recipient privately.
2. After separate authorization to create one synthetic draft, inspect its
   exact sender, To/CC/BCC, Unicode subject/body and harmless attachment visibly.
   Verify persistence/readback and attachment bytes. This is a real Mail write.
3. For a separately approved self-send, review the complete offline intent,
   approve its digest once, then observe Mail's submission, queue/Sent state and
   receipt separately. The local guard must reject an exact repeated intent
   without another backend invocation. Never retry an uncertain outcome.
4. Check native denial/partial-failure/timeout handling only within separately
   authorized operations. Preserve the attempt guard and audit actual state.
5. Validate provider-specific mailbox URLs, multi-account and Unicode account
   matching, replies, Send Later and other mutations before extending native
   claims. Index/store reconciliation requires existing Full Disk Access;
   visible scheduling requires existing Accessibility permission.

Cross-version testing, CI, provider coverage and native mutation checks are
follow-up validation. Their absence limits the claims of this experimental
source release; it is not a reason to claim that synthetic tests certify them.
Keyset pagination is not a frozen snapshot; timestamp changes and deletions
can still affect results. Cross-campaign claims require all workers to use the
current version and shared local state; old-version active claims cover only
their legacy scope. Publishing source
does not replace a user's installation or authorize Mail mutations.
