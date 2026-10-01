# Source and attribution

The project owner states that they created this Apple Mail CLI and authorized
publication under the NLINEA project identity. NLINEA is the maintainer and
publisher metadata. It is not an invented legal rights-holder designation.

The supplied source carries the MIT license with
`Copyright (c) 2026 Local developer`. The original LICENSE is retained
byte-for-byte. No copyright notice was removed or replaced.

The canonical local source had no Git metadata or remote. Its exact 24-file
base was pinned by SHA-256 inventory, and the original source and installed
consumers were preserved. This new publication tree does not reconstruct
historical commits or claim independently verified original authorship. The
owner's creator statement supplies the current attribution context; missing
historical Git metadata alone is not treated as a new rights objection.

The lookup was limited to project documentation, license, plugin metadata,
matching installed consumer metadata and available review recaps. Raw agent
sessions, private mail, credentials and unrelated histories were not read.
Machine-specific inventories and private review paths are excluded from this
public tree.

The independently reviewed 0.4.3 candidate was pinned to manifest SHA-256
`6cfada84c8ed59c683271fe193b65f00ff29510b497a63adb88fecab3e95bf02`.
This publication tree changes project metadata and documentation only; all
runtime, test, skill and reference bytes remain identical to that candidate.
[VALIDATION.md](VALIDATION.md) records the tested scope and limits.

Competitors were compared using their official source/documentation. Their
implementations were not copied, installed or executed. No competitor code
or dependencies are bundled. The digest bridge calls the macOS-provided
CommonCrypto `CC_SHA256` API, documented in Apple's
[CommonDigest.h](https://github.com/apple-oss-distributions/CommonCrypto/blob/main/include/CommonDigest.h).
No CommonCrypto implementation is vendored. SHA-256 is specified by
[NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final).
Using the platform primitive and passing fixtures does not establish
cryptographic-module certification.
