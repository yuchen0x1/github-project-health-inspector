import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "github-project-health-inspector" / "scripts" / "report-validator.py"
SPEC = importlib.util.spec_from_file_location("report_validator", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def report():
    sections = {n: "" for n, _ in validator.REQUIRED_SECTIONS}
    sections[1] = ("未发现已知漏洞。\n" +
                   " ".join("source%d.py:%d" % (i, i + 1) for i in range(8)) + "\n" +
                   " ".join("https://example.com/evidence/%d" % i for i in range(5)))
    sections[3] = "综合评分：100/100"
    sections[4] = "| 维度 | 得分 | 满分 |\n| --- | --- | --- |\n" + "\n".join(
        "| %s | %d | %d |" % (name, weight, weight)
        for name, weight in validator.WEIGHTS.items())
    sections[12] = "| 竞品项目 | 仓库 |\n| --- | --- |\n" + "\n".join(
        "| Project %d | https://github.com/owner/repo%d |" % (i, i) for i in range(5))
    sections[15] = "未发现 P0/P1。"
    sections[16] = "### 第一阶段\n修复。\n### 第二阶段\n验证。\n### 第三阶段\n完善。"
    sections[19] = "只读检查，未修改仓库。"
    return "\n\n".join("## %d. %s\n%s" % (n, names[0], sections[n])
                          for n, names in validator.REQUIRED_SECTIONS)


class FenceTests(unittest.TestCase):
    def test_tilde_fences_preserve_offsets_and_hide_headings(self):
        md = "~~~md\n## 1. 假章节\n~~~\n## 2. 真章节\n"
        masked = validator.mask_code_fences(md)
        self.assertEqual(len(md), len(masked))
        self.assertEqual(masked.index("## 2."), md.index("## 2."))
        self.assertEqual(set(validator.split_sections(md)), {2})

    def test_shorter_or_different_fence_does_not_close(self):
        md = "````md\n```\n## 1. 假章节\n~~~\n## 2. 假章节\n````\n## 3. 真章节\n"
        self.assertEqual(set(validator.split_sections(md)), {3})

    def test_section_subheading_does_not_replace_parent(self):
        md = "## 6. 代码质量\n正文\n### 6.2 设计问题\n更多正文\n## 7. 安全\n"
        self.assertIn("正文", validator.split_sections(md)[6]["body"])
        self.assertEqual(validator.split_sections(md)[6]["title"], "代码质量")

    def test_keywords_match_headings_only(self):
        self.assertFalse(validator.keyword_in_headings(["UI", "Bug"], "## build guide\ndebug UI Bug"))


class PriorityTests(unittest.TestCase):
    def test_bracketed_short_title_and_nested_evidence(self):
        md = "### [P1] 越界\n#### 证据\nfile.py:10\n#### 修复\n修复边界。\n### 下一条\n"
        _, _, p1, issues = validator.check_priority_blocks(md)
        self.assertEqual(len(p1), 1)
        self.assertEqual(issues, [])

    def test_evidence_does_not_leak_from_sibling(self):
        md = "### P1-1 缺陷\n无证据\n### P1-2 缺陷\nfile.py:10 修复建议：校验输入。\n"
        _, _, p1, issues = validator.check_priority_blocks(md)
        self.assertEqual(len(p1), 2)
        self.assertEqual(len(issues), 2)

    def test_groups_fenced_examples_and_p10_are_not_findings(self):
        md = "### P0（紧急）\n### [P1]（高）\n### P10 无效等级\n~~~\n### P1-1 代码示例\n~~~\n"
        blocks, p0, p1, issues = validator.check_priority_blocks(md)
        self.assertEqual((blocks, p0, p1, issues), ([], [], [], []))

    def test_heading_or_empty_label_is_not_a_fix(self):
        md = "### P1-1 缺少修复策略\nfile.py:10\n- 修复建议：\n"
        self.assertTrue(any("缺少修复建议" in e
                            for e in validator.check_priority_blocks(md)[3]))

    def test_fix_subheading_with_nonempty_body_is_a_fix(self):
        md = "### P1-1 缺陷\n#### 证据\nfile.py:10\n#### 修复\n校验输入并拒绝越界值。\n"
        self.assertEqual(validator.check_priority_blocks(md)[3], [])

    def test_empty_fix_subheading_cannot_borrow_sibling_content(self):
        md = "### P1-1 缺陷\n#### 修复\n\n#### 证据\nfile.py:10\n"
        self.assertTrue(any("缺少修复建议" in e
                            for e in validator.check_priority_blocks(md)[3]))


class ValidationTests(unittest.TestCase):
    def test_complete_report_passes(self):
        result = validator.validate(report())
        self.assertTrue(result["passed"], result["errors"])

    def test_zero_scores_are_compared_with_overall(self):
        md = report()
        for name, weight in validator.WEIGHTS.items():
            md = md.replace("| %s | %d | %d |" % (name, weight, weight),
                            "| %s | 0 | %d |" % (name, weight))
        self.assertTrue(any("不一致" in e for e in validator.validate(md)["errors"]))

    def test_duplicate_score_dimensions_rejected(self):
        md = report().replace("| 可维护性 | 5 | 5 |", "| 文档质量 | 5 | 5 |")
        self.assertTrue(any("重复维度" in e for e in validator.validate(md)["errors"]))

    def test_out_of_range_scores_rejected(self):
        for got in ("16", "-1"):
            with self.subTest(got=got):
                md = report().replace("| 代码质量 | 15 | 15 |", "| 代码质量 | %s | 15 |" % got)
                self.assertTrue(any("超出" in e for e in validator.validate(md)["errors"]))

    def test_fenced_score_table_is_ignored(self):
        rows = validator.parse_score_table("~~~\n| 代码质量 | 15 | 15 |\n~~~")
        self.assertEqual(rows[:3], ([], 0.0, 0))

    def test_empty_dimension_is_not_scored(self):
        for row in ("| | 15/15 |", "| | 15 | 15 |"):
            with self.subTest(row=row):
                self.assertEqual(validator.parse_score_table(row)[:3], ([], 0.0, 0))

    def test_fenced_overall_score_is_ignored(self):
        self.assertEqual(validator.find_overall_score("```\n综合评分：100/100\n```"), (None, None))

    def test_repeated_first_phase_does_not_pass(self):
        md = report().replace("第二阶段", "第一阶段").replace("第三阶段", "第一阶段")
        self.assertTrue(any("三阶段" in e for e in validator.validate(md)["errors"]))

    def test_phases_outside_roadmap_do_not_pass(self):
        md = report().replace("第二阶段", "长期工作").replace("第三阶段", "其他工作")
        md += "\n第二阶段 第三阶段"
        self.assertTrue(any("三阶段" in e for e in validator.validate(md)["errors"]))

    def test_fenced_competitor_rows_are_ignored(self):
        md = "| 竞品 | 仓库 |\n| --- | --- |\n| Test | https://github.com/a/b |"
        self.assertEqual(validator.count_competitor_rows("~~~\n" + md + "\n~~~", {}), 0)

    def test_negative_vulnerability_claim_does_not_require_id(self):
        result = validator.validate(report())
        self.assertFalse(any("没有任何 CVE" in e for e in result["errors"]))

    def test_positive_vulnerability_claim_requires_id(self):
        md = report().replace("未发现已知漏洞", "发现已知漏洞")
        self.assertTrue(any("没有任何 CVE" in e for e in validator.validate(md)["errors"]))

    def test_existing_sample_still_passes(self):
        sample = ROOT / "体检报告" / "GitHub-Flask-体检报告-2026-11-23.md"
        result = validator.validate(validator.read_report(sample))
        self.assertTrue(result["passed"], result["errors"])


class CliTests(unittest.TestCase):
    def test_version_is_available_without_report(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--version"],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"1.0.1", result.stdout)

    def test_json_is_usable_on_ascii_console(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.md"
            path.write_text(report(), encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="ascii")
            result = subprocess.run([sys.executable, str(SCRIPT), str(path), "--json"],
                                    capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def test_unreadable_file_returns_exit_code_two(self):
        with mock.patch.object(sys, "argv", [str(SCRIPT), str(SCRIPT)]), \
                mock.patch.object(validator, "read_report", side_effect=PermissionError("denied")), \
                mock.patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(validator.main(), 2)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_negative_minimum_is_rejected(self):
        result = subprocess.run([sys.executable, str(SCRIPT), str(SCRIPT), "--min-competitors", "-1"],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
