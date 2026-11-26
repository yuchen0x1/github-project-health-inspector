#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report-validator.py — GitHub 项目体检报告 · 结构与证据校验器

作用：在交付前机械地检查报告是否"像一份可信的审计报告"，
      拦住最常见的忽悠：少章节、分数对不上、问题没有文件位置、竞品凑数、占位符残留。

用法
  python report-validator.py 报告.md
  python report-validator.py 报告.md --json --strict --min-competitors 5

退出码
  0 = 通过（可能有 warning）
  1 = 存在 error，必须修复后再交付
  2 = 文件不可读

只依赖标准库。
"""

import argparse
import json
import re
import sys
from pathlib import Path

# (序号, 关键词候选) —— 命中序号或任一关键词即认为该章节存在
REQUIRED_SECTIONS = [
    (1, ["项目基本信息", "基本信息"]),
    (2, ["项目用途", "架构分析"]),
    (3, ["综合健康评分", "综合评分"]),
    (4, ["各维度评分", "维度评分"]),
    (5, ["项目结构", "结构审计"]),
    (6, ["代码质量", "Bug"]),
    (7, ["安全漏洞", "安全审计"]),
    (8, ["性能"]),
    (9, ["UI/UX", "UI", "交互体验"]),
    (10, ["工程化"]),
    (11, ["仓库健康", "GitHub 仓库"]),
    (12, ["同类项目", "竞品"]),
    (13, ["缺失的功能", "功能缺口"]),
    (14, ["推荐新增功能", "建议新增功能"]),
    (15, ["优先级问题清单", "问题清单"]),
    (16, ["升级路线图", "路线图"]),
    (17, ["修复建议"]),
    (18, ["升级专用提示词", "AI 自动升级", "提示词"]),
    (19, ["检测范围", "未验证"]),
    (20, ["最终审计结论", "审计结论", "结论"]),
]

WEIGHTS = {
    "代码质量": 15, "安全性": 15, "功能完整度": 15, "性能表现": 10, "UI/UX": 10,
    "架构设计": 10, "工程化": 10, "文档质量": 5, "可维护性": 5, "开源生态": 5,
}

FILE_REF_RE = re.compile(
    r"[\w./\\\-]+\.(?:py|pyi|js|jsx|ts|tsx|mjs|vue|svelte|go|rs|java|kt|swift|m|mm|"
    r"c|h|cc|cpp|hpp|cs|rb|php|dart|scala|lua|sh|ps1|yml|yaml|json|toml|ini|cfg|sql|"
    r"html|css|scss|md|xml|gradle|csproj|sln|lock|txt)(?::\d+)?")
URL_RE = re.compile(r"https?://[^\s)>\]\"']+")
VULN_ID_RE = re.compile(r"\b(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}|CVE-\d{4}-\d{4,7}|PYSEC-\d{4}-\d+)\b")
SCORE_FRAC_RE = re.compile(r"(-?\d{1,3}(?:\.\d)?)\s*/\s*(\d{1,3})")
PLACEHOLDERS = [
    (r"\bTBD\b", "TBD"),
    (r"\bTODO:\s", "TODO:"),
    (r"待补充|待填写|待定|稍后补充", "待补充/待填写"),
    (r"(?<!\$)\{\{[^}]{0,40}\}\}", "{{模板变量}}"),
    (r"<your[-_ ].*?>", "<your-...> 占位"),
    (r"\[待验证\]|\[占位\]", "占位标记"),
    (r"lorem ipsum", "lorem ipsum"),
]


def read_report(path):
    for enc in ("utf-8-sig", "gbk"):
        try:
            return Path(path).read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def mask_code_fences(md):
    """把围栏代码块内容替换为空白（保留换行与偏移），避免代码里的 # 标题被当成章节。"""
    out = []
    fence_char, fence_len = None, 0
    for line in md.splitlines(keepends=True):
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence_char is None and opener:
            fence_char, fence_len = opener.group(1)[0], len(opener.group(1))
            out.append(re.sub(r"[^\r\n]", " ", line))
            continue
        if fence_char is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence_char) +
                            r"{%d,}[ \t]*" % fence_len, line.rstrip("\r\n")):
                fence_char, fence_len = None, 0
            out.append(re.sub(r"[^\r\n]", " ", line))
        else:
            out.append(line)
    return "".join(out)


def split_sections(md):
    """返回 {number: text}（基于抹平代码块后的文本定位标题）。

    只认 `## ` 二级标题作为"第 N 章"。旧实现用 `#{1,6}`，会把 `### 6.2 设计层面问题`
    也解析成第 6 章，从而让第 6/7 章的标题与正文被子章节顶替。
    编号必须紧跟 `.` / `、`，以排除 `## 2026 年路线图` 这类以年份开头的标题。
    """
    masked = mask_code_fences(md)
    heads = [(m.start(), m.group(1), m.group(2)) for m in
             re.finditer(r"^##[ \t]+(?:第[ \t]*)?(\d{1,2})[.、][ \t]*(.*)$", masked, re.M)]
    sections = {}
    for i, (pos, num, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(md)
        try:
            n = int(num)
        except ValueError:
            continue
        body = md[pos:end]
        if n not in sections or len(body) > len(sections[n]["body"]):
            sections[n] = {"title": title.strip(), "body": body, "pos": pos}
    return sections


HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.*)$", re.M)


def heading_lines(md):
    """返回所有标题文字（已排除代码块内容）。"""
    return [m.group(1).strip() for m in HEADING_RE.finditer(mask_code_fences(md))]


def keyword_in_headings(keywords, md):
    """在标题中找关键词。

    只在标题里找，而不是全文找：否则 "UI" 会命中正文里的 "build"/"guide"，
    "Bug" 会命中 "debug"，缺章节的报告也能蒙混过关。
    ASCII 关键词要求词边界，中文关键词直接子串匹配。
    """
    heads = heading_lines(md)
    for k in keywords:
        if re.fullmatch(r"[A-Za-z0-9 /&+_-]+", k):
            pat = r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])"
            if any(re.search(pat, h, re.I) for h in heads):
                return True
        elif any(k in h for h in heads):
            return True
    return False


def parse_score_table(md):
    """解析维度评分表：| 维度 | 得分 | 满分 | 或 | 维度 | x/15 |。"""
    rows, total_max, total_got = [], 0, 0.0
    na_dims = set()
    for line in mask_code_fences(md).splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0]
        if not name:
            continue
        got = mx = None
        m = SCORE_FRAC_RE.search(" ".join(cells[1:]))
        if m:
            got, mx = float(m.group(1)), float(m.group(2))
        elif len(cells) >= 3 and re.fullmatch(r"-?\d{1,3}(?:\.\d)?", cells[1]) and \
                re.fullmatch(r"\d{1,3}", cells[2]):
            got, mx = float(cells[1]), float(cells[2])
        elif len(cells) >= 3 and cells[1].strip() in ("N/A", "n/a", "N／A", "不适用", "—", "-"):
            na_dims.add(cells[0])
            continue
        if got is None:
            continue
        if mx <= 0 or mx > 100:
            continue
        key = next((k for k in WEIGHTS if k.lower() in name.lower() or name.lower() in k.lower()
                    or k.replace("/", "") in name.replace("/", "")), None)
        if key is None:
            continue
        rows.append({"dimension": name, "got": got, "max": mx, "key": key})
        total_got += got
        total_max += mx
    return rows, total_got, total_max, na_dims


def find_overall_score(md):
    pats = [
        r"综合(?:健康)?(?:评分|得分)[^\d]{0,12}(\d{1,3}(?:\.\d)?)\s*(?:/\s*100|分)",
        r"总分[^\d]{0,10}(\d{1,3}(?:\.\d)?)\s*(?:/\s*100|分)",
        r"Overall[^\d]{0,12}(\d{1,3}(?:\.\d)?)\s*/\s*100",
        r"\*\*\s*(\d{1,3}(?:\.\d)?)\s*/\s*100\s*\*\*",
    ]
    for p in pats:
        m = re.search(p, mask_code_fences(md), re.I)
        if m:
            return float(m.group(1)), m.group(0)[:60]
    return None, None


def check_priority_blocks(md):
    """按"以 P0/P1 开头的标题"切分问题条目，并检查证据完整性。"""
    issues = []
    blocks = []
    heads = [(m.start(), m.group(2).strip(), len(m.group(1))) for m in
             re.finditer(r"^(#{1,6})[ \t]+(.+)$", mask_code_fences(md), re.M)]
    for i, (pos, title, depth) in enumerate(heads):
        end = next((p for p, _, d in heads[i + 1:] if d <= depth), len(md))
        m = re.match(r"^(?:\[P([0-3])\]|P([0-3])(?!\d))[\s\-–—]*(\d+)?", title)
        if not m:
            continue                      # 标题不是以 P0/P1 开头（如"修复 1：…（对应 P1-1）"）
        level = "P" + (m.group(1) or m.group(2))
        has_id = m.group(3) is not None
        rest = title[m.end():].strip(" :：-—）)")
        if not has_id and (not rest or re.fullmatch(
                r"[（(]?(?:紧急|严重|高|中|低|critical|high|medium|low)[）)]?", rest, re.I)):
            continue                      # "P0（紧急）"这类纯分组标题
        blocks.append((title, md[pos:end], level))

    p0 = [b for b in blocks if b[2] == "P0"]
    p1 = [b for b in blocks if b[2] == "P1"]
    for title, body, level in p0 + p1:
        has_file = bool(FILE_REF_RE.search(body))
        has_url = bool(URL_RE.search(body))
        fix_body = re.sub(r"^#{1,6}[ \t]+.*$", "", body, flags=re.M)
        fix_body = re.sub(r"(?:修复建议|修复方案|解决方案|修复|建议|方案|remediation|fix)"
                          r"[ \t]*[:：]?[ \t]*(?=\n|$)", "", fix_body, flags=re.I)
        has_fix = bool(re.search(r"修复|建议|方案|remediat|fix", fix_body, re.I))
        subheads = list(re.finditer(r"^(#{1,6})[ \t]+(.+)$", mask_code_fences(body), re.M))
        for index, heading in enumerate(subheads[1:], 1):
            if not re.match(r"^(?:修复(?:建议|方案|方法)?|解决方案|建议|remediation|fix(?:es)?)"
                            r"(?:[：:\s]|$)", heading.group(2), re.I):
                continue
            end = next((other.start() for other in subheads[index + 1:]
                        if len(other.group(1)) <= len(heading.group(1))), len(body))
            content = re.sub(r"^#{1,6}[ \t]+.*$", "", body[heading.end():end], flags=re.M)
            if re.search(r"\w", content):
                has_fix = True
                break
        if not (has_file or has_url):
            issues.append("%s 条目缺少文件路径/链接证据：%s" % (level, title[:60]))
        if not has_fix:
            issues.append("%s 条目缺少修复建议：%s" % (level, title[:60]))
    return [(t, b) for t, b, _ in blocks], p0, p1, issues


def _md_tables(md):
    """把 Markdown 切成表格列表，每个表格是 (表头行, 数据行列表)。"""
    tables = []
    cur_head, cur_rows = None, []
    for line in mask_code_fences(md).splitlines():
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if re.match(r"^\|[\s\-:|]+\|$", s) or all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue                      # 分隔行
            if cur_head is None:
                cur_head = s
            else:
                cur_rows.append(s)
        else:
            if cur_head is not None:
                tables.append((cur_head, cur_rows))
            cur_head, cur_rows = None, []
    if cur_head is not None:
        tables.append((cur_head, cur_rows))
    return tables


# 强竞品语义：表头出现这些词，基本可以确认是"同类项目对比表"
COMP_STRONG_RE = re.compile(r"(同类|竞品|对标|替代品|可比项目|competitor|alternative)", re.I)
# 弱语义：表头出现"项目/仓库/框架/库"时，还需该表确实含有仓库链接才算竞品表
COMP_WEAK_RE = re.compile(r"(项目|仓库|框架|库|repo|project|library|tool)", re.I)
# 常见"看起来像 owner/repo"但实际不是仓库的写法
NOT_REPO = {"ui/ux", "http/https", "html/css", "js/ts", "readme/license",
             "src/tests", "ci/cd", "docs/api", "n/a", "and/or"}


def _repo_key(line):
    """从一行表格里取出"仓库标识"，取不到返回 None。"""
    m = re.search(r"github\.com/([^/\s)|]+)/([^/\s)|#?]+)", line, re.I)
    if m:
        repo = m.group(2)
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        return (m.group(1) + "/" + repo).lower()
    for tok in re.findall(r"(?<![\w.\-])([\w.\-]+)/([\w.\-]+)(?![\w.\-])", line):
        pair = (tok[0] + "/" + tok[1]).lower()
        if pair in NOT_REPO:
            continue
        if len(tok[0]) < 2 or len(tok[1]) < 2:
            continue
        if tok[0].isupper() and tok[1].isupper():
            continue                      # UI/UX 之类的缩写，不是仓库
        return pair
    return None


def count_competitor_rows(md, sections):
    """统计竞品对比表行数（需带链接或 owner/repo 形态的仓库名）。

    旧实现是"全文找任何含 / 或 github.com 的表格行"，会把 `| UI/UX | 好 |`、
    `| a/b | x |` 这类普通表格也计成竞品，导致竞品数量虚高、缺章节的报告蒙混过关。
    现在只认"确实是竞品表"的表格里的数据行。
    """
    scopes = []
    for n in (12,):
        if n in sections:
            scopes.append(sections[n]["body"])
    if not scopes:
        scopes = [md]
    rows, seen = 0, set()
    for scope in scopes:
        for head, data_rows in _md_tables(scope):
            strong = bool(COMP_STRONG_RE.search(head))
            weak = bool(COMP_WEAK_RE.search(head))
            if not strong and not weak:
                continue
            table_keys = []
            for line in data_rows:
                key = _repo_key(line)
                if key:
                    table_keys.append((key, line))
            if not strong and not table_keys:
                continue                      # 弱匹配且没有仓库链接 → 不是竞品表
            for key, line in table_keys:
                if key in seen:
                    continue
                seen.add(key)
                rows += 1
    return rows


def validate(md, min_competitors=5, strict=False):
    errors, warnings, info = [], [], {}
    sections = split_sections(md)

    # 1) 章节完整性
    missing = []
    for num, keys in REQUIRED_SECTIONS:
        sec = sections.get(num)
        ok = bool(sec)
        if not ok:
            # 标题没写序号时，允许用关键词兜底——但只在标题里找，
            # 全文找会让 "UI" 命中 "build"、让缺章节的报告蒙混过关。
            ok = keyword_in_headings(keys, md)
        if not ok:
            missing.append("%d.%s" % (num, keys[0]))
    if missing:
        errors.append("缺少必需章节：%s" % "、".join(missing))
    info["sections_found"] = len(sections)

    # 2) 评分表
    rows, got, mx, na_dims = parse_score_table(md)
    overall, overall_raw = find_overall_score(md)
    info["score_rows"] = len(rows)
    info["score_sum"] = round(got, 1)
    info["score_max_sum"] = round(mx, 1)
    info["overall_score"] = overall
    if not rows:
        errors.append("未找到维度评分表（需含 维度 | 得分/满分 的行）")
    else:
        if abs(mx - 100) > 0.01:
            errors.append("各维度满分合计为 %g，应为 100（请核对权重）" % mx)
        if overall is None:
            warnings.append("未找到明确的综合评分（形如「综合评分：82/100」）")
        elif abs(overall - got) > 1.0:
            errors.append("综合评分 %.1f 与各维度得分之和 %.1f 不一致（差值 >1）" % (overall, got))
        for row in rows:
            if row["got"] < 0 or row["got"] > row["max"]:
                errors.append("维度 %s 得分 %g 超出 0-%g 范围" %
                              (row["dimension"], row["got"], row["max"]))
        duplicate_dims = sorted({r["key"] for r in rows
                                 if sum(x["key"] == r["key"] for x in rows) > 1})
        if duplicate_dims:
            errors.append("评分表重复维度：%s" % "、".join(duplicate_dims))
        covered = {r["key"] for r in rows}
        na_keys = {k for k in WEIGHTS if any(k.lower() in d.lower() or d.lower() in k.lower()
                                             for d in na_dims)}
        missing_dims = [k for k in WEIGHTS if k not in covered and k not in na_keys]
        if missing_dims:
            warnings.append("评分表未覆盖维度：%s（如确实不适用需在报告中说明）" % "、".join(missing_dims))
        if na_keys:
            info["na_dimensions"] = sorted(na_keys)
        if overall is not None and not (0 <= overall <= 100):
            errors.append("综合评分 %.1f 超出 0-100 范围" % overall)

    # 3) 问题分级与证据
    blocks, p0, p1, p_issues = check_priority_blocks(md)
    info["p0_count"] = len(p0)
    info["p1_count"] = len(p1)
    if p0 or p1:
        errors.extend(p_issues)
    else:
        warnings.append("未发现 P0/P1 分级条目（若项目确实无高危问题，请在问题清单中明确说明「未发现 P0/P1」）")
    if not re.search(r"\bP0\b", md):
        warnings.append("报告未使用 P0/P1/P2/P3 分级标记")

    # 4) 证据密度
    file_refs = len(set(FILE_REF_RE.findall(md)))
    urls = len(set(URL_RE.findall(md)))
    vuln_ids = len(set(VULN_ID_RE.findall(md)))
    info.update({"unique_file_refs": file_refs, "unique_urls": urls, "vuln_ids": vuln_ids})
    min_files = 8 if strict else 5
    min_urls = 5 if strict else 3
    if file_refs < min_files:
        errors.append("文件级证据过少（仅 %d 个唯一文件引用，至少 %d）。"
                      "结论必须能定位到具体文件。" % (file_refs, min_files))
    if urls < min_urls:
        errors.append("外部链接过少（仅 %d 条，至少 %d）。竞品与 CVE 必须附原始链接。" % (urls, min_urls))

    # 5) CVE 声明必须有编号
    cve_claims = []
    for clause in re.split(r"[\n。；;]", mask_code_fences(md)):
        if not re.search(r"已知漏洞|CVE|存在漏洞", clause):
            continue
        if re.search(r"(?:未发现|未检出|没有|不存在|无已知|未命中|无法验证|无法确认|未验证)", clause):
            continue
        if re.search(r"(?:存在|发现|检出|命中|包含|含有|确认)[^。；\n]{0,40}(?:漏洞|CVE)|"
                     r"(?:漏洞|CVE)[^。；\n]{0,20}(?:存在|已确认|已检出|命中)", clause):
            cve_claims.append(clause)
    if cve_claims and vuln_ids == 0:
        errors.append("提到了漏洞但没有任何 CVE/GHSA/PYSEC 编号作为证据")

    # 6) 竞品数量
    comp_rows = count_competitor_rows(md, sections)
    info["competitor_rows"] = comp_rows
    if comp_rows < min_competitors:
        errors.append("同类项目对比仅 %d 个，要求至少 %d 个（且需带链接/仓库名）"
                      % (comp_rows, min_competitors))

    # 7) 三阶段路线图
    roadmap = sections.get(16, {}).get("body", md)
    phase_labels = re.findall(r"第([一二三])阶段|阶段([一二三])|Phase\s*([123])\b",
                              mask_code_fences(roadmap), re.I)
    phase_ids = {dict(zip("一二三", "123")).get(value, value)
                 for groups in phase_labels for value in groups if value}
    phases = len(phase_ids)
    info["roadmap_phase_mentions"] = phases
    if phases < 3:
        errors.append("升级路线图未明确分为三阶段（第一阶段/第二阶段/第三阶段）")

    # 8) AI 提示词质量
    prompt_sec = sections.get(18, {}).get("body", "")
    if prompt_sec:
        must = {
            "要求先分析": r"(先|首先|before).{0,12}(分析|阅读|理解|review)",
            "要求保留原有功能": r"(保留|不破坏|不改变|backward|regress)",
            "要求测试验证": r"(测试|test|验证|verify|跑通)",
        }
        for label, pat in must.items():
            if not re.search(pat, prompt_sec, re.I):
                warnings.append("第 18 节 AI 提示词缺少「%s」的明确要求" % label)
        if len(prompt_sec) < 400:
            warnings.append("第 18 节 AI 提示词偏短（%d 字符），可能不足以让编程 AI 独立执行" % len(prompt_sec))

    # 9) 占位符
    for pat, label in PLACEHOLDERS:
        hits = re.findall(pat, md, re.I)
        if hits:
            warnings.append("存在疑似占位符 %d 处：%s" % (len(hits), label))

    # 10) 未验证事项与评分不确定性的一致性
    sec19 = sections.get(19, {}).get("body", "")
    if re.search(r"未充分验证|unverified|无法验证", md):
        if not sec19:
            errors.append("评分中声明了「未充分验证」，但第 19 节缺失，必须列出未验证事项")
        elif len(sec19) < 80:
            warnings.append("第 19 节内容过短，未验证事项需要具体到「哪个维度、为什么、缺什么证据」")

    # 11) 只读声明（避免报告让人误以为改了仓库）
    if not re.search(r"(只读|未修改|未对仓库做任何|read[- ]only)", md, re.I):
        warnings.append("未声明本次审计为只读、未修改仓库")

    if strict and (p0 and len(p0) > 0 and info.get("vuln_ids", 0) == 0):
        warnings.append("strict 模式：存在 P0 但无漏洞编号")

    return {"errors": errors, "warnings": warnings, "info": info,
            "passed": not errors}


def _safe_print(text):
    """在 GBK 控制台（Windows 默认）下也能打印 ✓/✗ 等符号。

    旧实现直接 print("✓ ...")，在 cp936 终端上抛 UnicodeEncodeError，
    校验通过的报告反而以异常退出码 1 结束——CI 里会被当成校验失败。
    """
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def main():
    ap = argparse.ArgumentParser(description="体检报告结构与证据校验")
    ap.add_argument("--version", action="version", version="%(prog)s 1.0.1")
    ap.add_argument("report", help="报告 Markdown 文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="更严格的证据数量门槛")
    ap.add_argument("--min-competitors", type=int, default=5)
    args = ap.parse_args()
    if args.min_competitors < 0:
        ap.error("--min-competitors 必须大于或等于 0")

    p = Path(args.report)
    if not p.is_file():
        print(json.dumps({"ok": False, "error": "文件不存在: %s" % p}, ensure_ascii=True))
        return 2
    try:
        md = read_report(p)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": "文件不可读: %s" % exc}, ensure_ascii=True))
        return 2
    res = validate(md, min_competitors=args.min_competitors, strict=args.strict)
    res["file"] = str(p)
    res["chars"] = len(md)
    res["ok"] = res["passed"]

    if args.json:
        print(json.dumps(res, ensure_ascii=True, indent=2))
    else:
        _safe_print("=" * 70)
        _safe_print("体检报告校验：%s" % p.name)
        _safe_print("=" * 70)
        i = res["info"]
        _safe_print("字符数        : %s" % res["chars"])
        _safe_print("识别章节      : %s" % i.get("sections_found"))
        _safe_print("评分表行数    : %s（得分合计 %s / 满分合计 %s）"
                    % (i.get("score_rows"), i.get("score_sum"), i.get("score_max_sum")))
        _safe_print("综合评分      : %s" % i.get("overall_score"))
        _safe_print("P0 / P1 条目  : %s / %s" % (i.get("p0_count"), i.get("p1_count")))
        _safe_print("唯一文件引用  : %s" % i.get("unique_file_refs"))
        _safe_print("唯一外部链接  : %s" % i.get("unique_urls"))
        _safe_print("漏洞编号      : %s" % i.get("vuln_ids"))
        _safe_print("竞品对比行    : %s" % i.get("competitor_rows"))
        _safe_print("-" * 70)
        if res["errors"]:
            _safe_print("✗ ERROR（%d）" % len(res["errors"]))
            for e in res["errors"]:
                _safe_print("  - %s" % e)
        if res["warnings"]:
            _safe_print("! WARN（%d）" % len(res["warnings"]))
            for w in res["warnings"]:
                _safe_print("  - %s" % w)
        if not res["errors"] and not res["warnings"]:
            _safe_print("✓ 全部检查通过")
        elif not res["errors"]:
            _safe_print("✓ 无阻断性问题，warnings 建议处理")
        _safe_print("=" * 70)
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
