# Security Policy

Use the latest stable release. This project performs static inspection; a clean
scan is not proof that the inspected project has no vulnerabilities. Network
failures, sampling limits, and unsupported manifests must remain visible in the
generated evidence.

For a suspected vulnerability involving sensitive information, use GitHub's
private vulnerability reporting when available, or contact the repository owner
privately before filing a public issue. Never include tokens, credentials, or
private repository contents in public reports.

Report the affected version, operating system, minimal reproduction, expected
behavior, and impact. The scripts use only Python's standard library; GitHub
Actions dependencies are pinned to reviewed commit hashes. Release archives are
built from an explicit source allowlist and include SHA-256 checksums.
