import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ReleasePackageTests(unittest.TestCase):
    def test_archive_contains_only_reviewed_sources_and_matching_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, checksums = builder.build_package(temp)
            with zipfile.ZipFile(archive) as bundle:
                expected = {builder.SKILL_NAME + "/" + name for name in builder.PACKAGE_FILES}
                expected.add(builder.SKILL_NAME + "/LICENSE")
                self.assertEqual(set(bundle.namelist()), expected)
                self.assertIsNone(bundle.testzip())
                self.assertEqual(bundle.read(builder.SKILL_NAME + "/VERSION"),
                                 (ROOT / builder.SKILL_NAME / "VERSION").read_bytes())
            self.assertEqual(checksums.read_text(encoding="ascii").split()[0],
                             hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_archive_is_reproducible(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left, _ = builder.build_package(first)
            right, _ = builder.build_package(second)
            self.assertEqual(left.read_bytes(), right.read_bytes())

    def test_version_is_consistent_across_installed_commands(self):
        skill = ROOT / builder.SKILL_NAME
        version = (skill / "VERSION").read_text(encoding="ascii").strip()
        self.assertIn("version: " + version, (skill / "SKILL.md").read_text(encoding="utf-8"))
        for name in ("github-inspector.py", "report-validator.py", "check-updates.py"):
            with self.subTest(script=name):
                result = subprocess.run([sys.executable, str(skill / "scripts" / name), "--version"],
                                        capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(version.encode("ascii"), result.stdout)

    def test_reject_output_inside_skill(self):
        with self.assertRaises(ValueError):
            builder.build_package(ROOT / builder.SKILL_NAME / "dist")


if __name__ == "__main__":
    unittest.main()
