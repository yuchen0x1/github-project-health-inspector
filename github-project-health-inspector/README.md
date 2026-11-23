# GitHub 项目全方位体检专家 · GitHub Project Health Inspector

一个 WorkBuddy Skill：**只给一个 GitHub 链接**，自动完成源码审计、安全与依赖漏洞检查、性能与工程化评估、同类项目研究、100 分制评分，输出带证据的正式报告与三阶段升级路线图。

- 技能标识：`github-project-health-inspector`
- 版本：1.0.0
- 依赖：Python ≥ 3.8（**仅标准库**，无需 pip 安装）、git（可选，缺失时自动回退 zipball）
- 可选：`GITHUB_TOKEN` 环境变量（API 配额 60/h → 5000/h）

---

## 安装

### 方式 1：解压到用户级 skills 目录（推荐）

```bash
# macOS / Linux
mkdir -p ~/.workbuddy/skills
unzip github-project-health-inspector-1.0.0.zip -d ~/.workbuddy/skills/
```

```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.workbuddy\skills" | Out-Null
Expand-Archive .\github-project-health-inspector-1.0.0.zip -DestinationPath "$env:USERPROFILE\.workbuddy\skills"
```

安装后目录应为：

```
~/.workbuddy/skills/github-project-health-inspector/
├── SKILL.md
├── README.md
├── references/   (7 份标准)
├── templates/    (3 份模板)
└── scripts/      (2 个脚本)
```

重启 WorkBuddy（或重新打开会话）使技能生效。

### 方式 2：项目级安装

放到 `{项目目录}/.workbuddy/skills/github-project-health-inspector/`，仅对该项目生效，便于团队共享。

---

## 使用

在对话里直接发链接即可，不需要填任何项目资料：

```
https://github.com/pallets/flask
```

或：

```
帮我体检一下 https://github.com/xxx/yyy
psst 审计这个仓库 owner/repo
```

触发后会自动：克隆 → 识别技术栈 → 审计 → 搜竞品 → 打分 → 写报告 → 自检。

### 产物

```
体检报告/
├── GitHub-<项目名>-体检报告-<YYYY-MM-DD>.md     # 20 章节正式报告
└── GitHub-<项目名>-体检报告-<YYYY-MM-DD>.html   # 深色风格可视化版（可选）
audit-<repo>/
├── inspector-data.json    # 结构化证据（可复现）
├── summary.txt            # 一屏摘要
└── repo/                  # 浅克隆源码
```

---

## 单独使用脚本

脚本可脱离对话直接跑，适合 CI 或批量巡检。

```bash
# 全量体检
python scripts/github-inspector.py https://github.com/owner/repo --out ./audit

# 私有仓库 / 提高配额
export GITHUB_TOKEN=ghp_xxx
python scripts/github-inspector.py owner/repo

# 只做代码层分析（不调 GitHub API，适合本地项目或限流时）
python scripts/github-inspector.py --local /path/to/project --out ./audit

# 大仓库分批
python scripts/github-inspector.py <url> --max-files 3000

# 校验报告是否合格
python scripts/report-validator.py "体检报告/GitHub-xxx-体检报告-2026-11-23.md"
```

`github-inspector.py` 参数：

| 参数 | 说明 |
|---|---|
| `--out DIR` | 输出目录（默认 `./inspector-out`） |
| `--local PATH` | 审计本地目录，离线，不调 API |
| `--token` | GitHub Token（也可用 `GITHUB_TOKEN`/`GH_TOKEN`） |
| `--no-clone` | 只用 API，不下载源码（代码层结论会缺失） |
| `--max-files N` | 扫描文件上限（默认 8000） |
| `--budget N` | API 调用预算（默认 80；匿名限流 60/h） |
| `--skip-osv` | 跳过依赖漏洞查询 |
| `--json-only` | 只打印数据文件路径 |

`report-validator.py` 退出码：`0` 通过 / `1` 有阻断性问题 / `2` 文件不可读。加 `--strict` 提高证据门槛，`--json` 输出机器可读结果。

---

## 它到底查什么

| 维度 | 手段 | 关键点 |
|---|---|---|
| 项目识别 | 解析清单文件 + 入口点 + 目录结构 | 读代码而非只读 README；识别 monorepo/子模块 |
| 结构架构 | 目录映射、重复文件名、遗留文件、深度分布 | 指出耦合与过度设计 |
| 代码质量 | Python 用 `ast`（函数长度/复杂度/裸 except/可变默认参数），JS 用大括号配对启发式 | 6 行滑窗哈希算重复率；区分"确认/疑似/建议" |
| 安全 | 16 类凭据正则 + 16 类危险 API + `tokenize` 抹除 docstring 后匹配 + Actions 规则 | **命中必须复核数据流，不把信号当漏洞** |
| 依赖漏洞 | 解析 11 种锁文件 → OSV.dev `querybatch` 按确切版本查询 | 只有锁文件版本才查；区分项目自身与示例目录 |
| 性能 | 静态识别 N+1、同步阻塞、无分页、打包体积 | **未实测绝不给数字** |
| UI/UX | 四态（loading/empty/error/success）、响应式、a11y | 无界面则标 N/A 并说明权重去向 |
| 工程化 | 15 项标准文件与 CI 逐项打勾 | 每项给证据路径 |
| 仓库健康度 | Issues/PRs/Commits/Releases/Contributors/Bus factor | 区分累计与当前，声明抽样范围 |
| 竞品研究 | 领域词+品类词短语查询 + topic 具体度排序 + 品类校验 | 排除 awesome/教程/下游使用者；许可证兼容矩阵 |

---

## 设计原则（为什么可信）

1. **只读**：不 push、不改文件、不装依赖到用户仓库。
2. **证据分级**：每条结论标 L1 实测 / L2 静态确证 / L3 静态推断 / L4 外部参考。
3. **不伪造**：拿不到版本就不查 CVE，无法运行就不给耗时数字，检索失败就说失败。
4. **不夸大**：报告生成后必须过 `report-validator.py`；P0 过多会被结构性检查暴露。
5. **不追问**：技术栈、用途、范围全部自行判断——这是"一个链接就能跑"的前提。

---

## 常见问题

**Q：`git` 没装能跑吗？** 能。会自动回退 GitHub zipball 下载；都不行则退化为纯 API 分析，并在报告里注明"无代码层证据"。

**Q：一直提示限流？** 匿名 60 次/小时。设置 `GITHUB_TOKEN`（只需 public_repo 读权限）即可提升到 5000/h。

**Q：私有仓库？** 需要你提供只读 Token。技能不会尝试绕过权限，也不会要求你把源码上传到第三方。

**Q：报告太长？** 第 18 节可直接交给 Codex / Claude Code / WorkBuddy 执行修复；需要决策摘要时会让它同时产出 `executive-summary`。

**Q：能审计本地项目吗？** 能：`python scripts/github-inspector.py --local /path/to/project`，代码层审计完整，只缺 Issues/Star 等远端指标。

---

## 已验证

- 端到端实跑 `pallets/flask`（commit `49943b5`）：浅克隆 1.74 MB、84 文件 / 18,483 行、93 个锁定版本查 OSV、5 条 workflow 审计、10 个竞品候选人工复核留 5 个。
- 报告 `GitHub-Flask-体检报告-2026-11-23.md` 通过 `report-validator.py`：20/20 章节、评分 87/100 与维度之和一致、3 条 P1 均带文件行号、58 处文件引用、10 条外链、5 个竞品。

## 许可证

MIT
