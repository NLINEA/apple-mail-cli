# 0.4.3 validation record

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
