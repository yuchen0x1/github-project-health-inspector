#!/usr/bin/env python3
"""Build a reproducible, allowlisted skill archive using only the standard library."""

import argparse
import hashlib
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "github-project-health-inspector"
PACKAGE_FILES = (
    "README.md", "SKILL.md", "VERSION",
    "references/code-quality.md",
    "references/competitor-research.md",
    "references/inspection-standards.md",
    "references/performance-audit.md",
    "references/scoring-system.md",
    "references/security-audit.md",
    "references/ui-ux-audit.md",
    "scripts/check-updates.py",
    "scripts/github-inspector.py",
    "scripts/report-validator.py",
    "templates/executive-summary.md",
    "templates/full-report.md",
    "templates/remediation-roadmap.md",
)


def build_package(output, root=ROOT):
    root = Path(root).resolve()
    skill = root / SKILL_NAME
    version = (skill / "VERSION").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("VERSION must contain a stable semantic version")
    sources = [(relative, skill / relative) for relative in PACKAGE_FILES]
    sources.append(("LICENSE", root / "LICENSE"))
    for relative, source in sources:
        if not source.is_file() or source.is_symlink() or root not in source.resolve().parents:
            raise ValueError("Missing or unsafe package source: %s" % relative)
    output = Path(output).resolve()
    if output == skill.resolve() or skill.resolve() in output.parents:
        raise ValueError("Build output must be outside the skill source directory")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ("%s-%s.zip" % (SKILL_NAME, version))
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative, source in sorted(sources):
            info = zipfile.ZipInfo("%s/%s" % (SKILL_NAME, relative), (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, source.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksums = output / "SHA256SUMS.txt"
    checksums.write_text("%s  %s\n" % (digest, archive.name), encoding="ascii")
    return archive, checksums


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    archive, checksums = build_package(args.output)
    print(archive.name)
    print(checksums.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
