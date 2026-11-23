# GitHub 项目全方位体检专家

> 发一个 GitHub 链接，产出一份有证据、可执行的项目体检报告。

一个 WorkBuddy Skill（`github-project-health-inspector`）。它不做"读 README 写读后感"，而是克隆代码、逐文件扫描、查依赖 CVE、核对仓库健康度、检索同类项目，最后交出一份 20 章节的正式报告——**并且报告必须能通过自带校验器**。

---

## 为什么不是又一个提示词

大部分"代码审计 prompt"的通病是：让它找漏洞，它一定会找出漏洞；让它打分，它一定给出好看的分数。这个 Skill 的核心设计是**给自己踩刹车**。

| 常见做法 | 这里的做法 |
|---|---|
| 从 `flask>=3.1.2` 推断"已安装 3.1.2"，查出一堆 CVE | 只查锁文件或 `==` 精确 pin 的版本；范围约束一律标 `unverified`，不猜 |
| 正则命中 `eval(` 就报高危 | 先用 `tokenize` 抹掉注释与 docstring，再结合数据流判定；flask 实测从 40 处降到 3 处 |
| 竞品表按 Star 排序 | 用「领域词 + 品类词」短语查询，做品类校验、排除下游使用者、标注许可证 |
| 给"页面加载 1.2 秒"这类编造指标 | 没有真实运行就只写静态推断，不产出任何性能数字 |
| 报告写完直接交付 | 必须过 `report-validator.py`：20 章节齐全、评分合计 100、P0/P1 必带文件行号与修复建议 |

## 实测效果

对 `pallets/flask`（commit `49943b5`）实跑：浅克隆 1.74 MB、84 个代码文件、18,483 行、93 个锁定版本查询 OSV、5 条 workflow 审计、10 个竞品候选复核后保留 5 个。

报告结论：**87/100**，3 条 P1。

- 40 处正则"风险"经复核只剩 3 处，且**全部被判为非漏洞**：`cli.py:1022` 的 `eval` 只读用户本机 `PYTHONSTARTUP`；`config.py:209` 执行配置是文档化设计；`helpers.py:528` 的"路径穿越"是 Werkzeug 安全调用被误报。
- 真正的问题在流程：锁文件里 `werkzeug 3.1.8` 带 CVE（修复于 3.1.9）、277 天未发版而主干仍在提交、92% 的提交来自同一人。

完整报告见 [`体检报告/`](体检报告/)，原始证据见 [`示例数据/flask/`](示例数据/flask/)。

## 安装

```bash
git clone https://github.com/yuchen0x1/github-project-health-inspector.git
cp -r github-project-health-inspector/github-project-health-inspector ~/.workbuddy/skills/
```

Windows PowerShell：

```powershell
Copy-Item -Recurse .\github-project-health-inspector\github-project-health-inspector "$env:USERPROFILE\.workbuddy\skills\"
```

装好后在 WorkBuddy 里直接发仓库链接即可触发，无需补充任何项目资料。

## 用法

```bash
# 1) 采集（输出 inspector-data.json + summary.txt）
python scripts/github-inspector.py https://github.com/owner/repo --out ./out

# 2) 审本地目录，不消耗 API 配额
python scripts/github-inspector.py --local /path/to/project --out ./out

# 3) 校验报告（20 章节 / 评分 / 证据完整性）
python scripts/report-validator.py "GitHub-项目名-体检报告-2026-11-23.md"
```

可选参数：`--stage repo|code|github|competitors`（分阶段执行）、`--budget N`（限制 API 调用次数）、`--no-clone`（跳过克隆，仅走 API）。

仅依赖 Python 标准库，无需 pip 安装。设置 `GITHUB_TOKEN` 可把匿名 60 次/小时的配额提到 5000 次。

## 目录

```
├── github-project-health-inspector/
│   ├── SKILL.md                  # 触发条件 + 7 步工作流 + 硬性纪律
│   ├── references/               # 7 份判定标准：什么算"查过"
│   ├── templates/                # 3 份模板：20 章节骨架 / 决策摘要 / 路线图
│   └── scripts/                  # 采集器 + 报告校验器（纯标准库）
├── 体检报告/                     # 实跑产出的示例报告（Markdown + HTML）
├── 示例数据/flask/               # 支撑报告结论的原始结构化证据
└── 使用说明.md                   # 完整交付说明
```

## 已知局限

- **性能不实测**：默认只做静态推断，不产出启动耗时、内存占用等数字。需要实测请单独运行基准。
- **测试比例 ≠ 覆盖率**：只统计测试文件与代码文件的比例，不代表真实覆盖率。
- **竞品检索是候选生成器**：输出的是线索，必须逐个打开 README 与依赖清单核实，不能直接当结论。
- **匿名配额有限**：未配置 token 时按抽样计算，报告中会声明抽样范围。
- **静态分析无法覆盖业务逻辑漏洞**：未命中不代表安全。

## 许可证

MIT
