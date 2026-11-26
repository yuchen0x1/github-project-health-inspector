# Changelog

## 1.0.1

- Preserve existing clone/work directories and isolate temporary source downloads.
- Validate ZIP paths and extraction limits; skip links outside the scan boundary.
- Report failed or incomplete OSV checks as unverified, with actual query counts.
- Keep project/example dependency scope and multiple installed versions separate.
- Correct Python/Poetry dependency parsing and redact credential evidence.
- Make local inspection offline by default; allow explicit OSV access with
  `--allow-network`. Preserve legacy stage aliases and `--keep-clone`.
- Correct report sections, fenced examples, priority evidence, score validation,
  competitor tables, roadmap stages, and Windows console output.
- Add official release checks, version commands, and an explicit upgrade path.
- Add offline regression tests, cross-platform CI, CodeQL, and reproducible
  allowlisted release archives with SHA-256 checksums.
- Correct installation examples and remove misleading local-machine claims.

## 1.0.0

Initial public release with source inspection, repository metadata, static
security analysis, dependency checks, competitor candidates, and report validation.
