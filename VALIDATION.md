# 0.6.0 integration validation — 2026-10-10

Status: **experimental**. This public-source update combines local 0.5/0.6
development with the existing public 0.4.3 guard implementation. The earlier
independent review below is historical; it does not cover this integration.

| Check | Fresh result | Boundary |
| --- | --- | --- |
| Full Python suite | 166 passed; one opt-in live test skipped | Synthetic fixtures, mocked mutations and a local fake HTTP server |
| Offline Python suite | 164 passed; one opt-in live test skipped | Excludes the two HTTP loopback tests |
| Fake JXA logic | 47 checks passed | Fake Mail objects; includes draft BCC/body/attachment drift, sender checks before draft updates and legacy send bypass rejection |
| Native Foundation/CommonCrypto protocol | Four cases passed | Real platform APIs against temporary synthetic state; Mail never loaded |
| Syntax, launcher, JSON capabilities, skill structure, diff integrity | Passed | Public source and manifest report 0.6.0 |

The direct-send workflow retains exact-digest preview confirmation, immutable
attachment snapshots, durable intent reservations and backend claims. New
operation receipts do not replace that guard. Tests also cover cross-campaign
claims, pinned schedule targets, grouped partial reads, exact-account Sent
observations, review-only follow-ups, render rollback and existing campaign
review regressions. A historical Sent filename/count match remains labelled
`metadata_matched`, but expected attachments without byte evidence now raise
`attachment_unverified` and keep the audit incomplete.

No real Mail message was read, created, sent, scheduled, moved or deleted in
this integration run. No real UBD endpoint was called. Live doctor/account
checks and opt-in Mail tests were not run. In particular, the new draft
readback guard has only synthetic coverage; native Mail property availability,
signature/body transformation and asynchronous attachment behavior may block
it. This release does not certify native sends, queue persistence or delivery.
Publishing it does not update any existing local installation.

Reproduce with the commands in the historical record below, plus:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

Keep `APPLE_MAIL_CLI_LIVE_TESTS` unset for the synthetic full suite. The offline
runner disables it regardless. Privacy pattern scans checked the publication
tree; they are not a complete secret-detection or security certification.

# Historical 0.4.3 validation record

Status: **experimental**, with a passed independent review of the bounded
source/synthetic scope. Validation performed on 2026-10-01 using macOS 26.6.2
arm64, Python 3.9.6 and Node 25.8.1. These versions are observations, not a
cross-version support certification.

| Check | Recorded result | Boundary |
| --- | --- | --- |
| Guarded Python regressions | 83 passed; one live test skipped | Synthetic data; subprocess/network/real Mail store access blocked in the offline runner |
| Fake JXA logic | 40 checks passed | Existing Node runtime; fake Mail objects |
| Native Foundation/CommonCrypto protocol | Four cases passed | Actual JXA platform APIs; temporary private state; Mail never loaded |
| Independent additional scenarios | 10 passed | Includes eight concurrent confirmations: one backend invocation, seven blocked attempts |
| Syntax, launcher, JSON capabilities, patch integrity | Passed | CLI/backend/plugin agree on 0.4.3 |
| Native read-only index smoke | Compatible schema; bounded synthetic subject query succeeded with zero rows | Existing authority; no body reads or raw rows retained |

Review found no material P1/P2 findings in that scope. The earlier review's
three campaign findings and one body-read finding had been fixed and rechecked
in 0.4.2. This is a scoped review result, not a guarantee that defects cannot
remain. Publication-only metadata/documentation edits do not change the
reviewed runtime or tests.

The direct-send review exercised exact-intent binding, attachment byte drift,
missing/private record checks, replay rejection, persistence failures,
interruptions, unknown outcomes and concurrent reservation. A protocol fixture's
accepted result means its guard/claim step passed; it does not mean Mail accepted
or delivered a message. The backend success contract separately reports
`submission_accepted: true` and `delivery_confirmed: false`.

Unverified: live account/sender resolution; actual drafts/replies, To/CC/BCC,
body/signature transformations, attachment readback/asynchronous behavior,
Mail send/queue/receipt, native mutations and Send Later UI; provider/localized
mailbox mappings; denial/partial-failure recovery in real Mail; other macOS,
Python or Node versions; CI; loopback HTTP and an external UBD OS deployment.
No new Automation/FDA/Accessibility permission was granted. A read-only backend
ping does not establish Automation authority.

The source candidate, original 24-file base and installed launcher were verified
unchanged. Candidate privacy/provenance scans found no high-confidence secret
or non-example address findings in the allowlisted package. Pattern scans are
not a complete secret or authorship certification. Private mail, credentials,
machine-specific source inventories and review logs are not public fixtures.

Reproduce the supported synthetic checks from the source directory:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_offline.py
node tests/test_backend_quality.js
# Optional macOS-only native protocol fixture; does not load Mail:
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_native_send_protocol.py
```

The project offline runner excludes the HTTP loopback module and disables the
opt-in live module; the stricter no-subprocess/no-socket/no-real-store harness
used during review is private review tooling, not bundled here. The native
protocol fixture launches JXA for synthetic platform checks. Do not enable live
tests or real Mail mutations merely to reproduce this table.
