---
name: github-project-health-inspector
description: "用户发送 GitHub 仓库链接（或 owner/repo、本地项目目录）即触发：自动克隆读取源码、识别技术栈、审计安全与依赖漏洞、分析代码质量/架构/性能/UI/工程化、检索同类开源项目对比、按 100 分制打分、生成带证据的正式体检报告与三阶段升级路线图。触发词：GitHub 体检、项目体检、仓库审计、代码审查、仓库健康度、code review my repo、audit this repo、分析这个仓库、看看这个项目怎么样、github.com 链接。"
description_zh: "只要一个 GitHub 链接，全自动完成代码/安全/性能/工程化审计 + 竞品对比 + 评分 + 升级路线图"
description_en: "Paste a GitHub URL and get a full evidence-backed health audit, competitor analysis and upgrade roadmap"
version: 1.0.1
display_name: "GitHub 项目全方位体检专家"
display_name_en: "GitHub Project Health Inspector"
license: MIT
visibility: public
metadata:
  author: lenmo
  category: "engineering-audit"
  entry: scripts/github-inspector.py
---

# GitHub 项目全方位体检专家

用户发来一个仓库地址，你的任务不是"聊一聊这个项目"，而是**产出一份能直接拿去修 bug 的审计报告**。

## 触发判定

出现以下任一情况，立即启动本技能，**不要先问用户项目背景、技术栈、检测范围**：

- 消息里有 `github.com/owner/repo` 链接
- 消息是 `owner/repo` 形式
- 用户说"体检/审计/分析/看看这个项目/这个仓库怎么样/有没有问题"
- 用户要求 review 某个开源项目或自己项目的质量

只有一种情况需要停下来问：**仓库是私有的且当前无访问权限**（API 返回 404/403 且非限流）。此时说明需要只读 Token 授权，不得尝试绕过。

## 前置检查

```bash
SKILL_DIR="${CLAUDE_SKILL_DIR:-$HOME/.workbuddy/skills/github-project-health-inspector}"
python --version || python3 --version
ls "$SKILL_DIR/scripts/github-inspector.py"
```

- Python ≥ 3.8 即可，**脚本只用标准库**，无需 pip 安装任何东西
- 有 `git` 时用浅克隆；没有会自动回退 zipball
- 可选：`GITHUB_TOKEN` 环境变量（把 API 配额从 60/h 提到 5000/h）。没有也能跑，但要在报告里声明抽样限制
- 在线审计开始前运行 `python "$SKILL_DIR/scripts/check-updates.py"`，发现新版应提醒用户；网络失败只说明未验证。用户要求离线时不执行更新检查，`--local` 默认也不会联网。

## 执行流程

### 第 0 步：采集数据（必须实际执行，不能凭印象）

```bash
python "$SKILL_DIR/scripts/github-inspector.py" "<仓库URL>" --out ./audit-<repo名>
```

产出：
- `audit-<repo>/inspector-data.json` — 结构化证据（元数据、技术栈、结构、质量、安全、依赖漏洞、仓库健康度、竞品候选）
- `audit-<repo>/summary.txt` — 一屏摘要，先读这个
- `audit-<repo>/repo/` — 浅克隆源码，后续要读它

大仓库可限制静态扫描规模（只影响下载后的文件遍历，不减少克隆体积，也不提供自动分页）：
```bash
python "$SKILL_DIR/scripts/github-inspector.py" "<URL>" --out ./audit-x --max-files 3000 --stage analyze
```

脚本失败或数据不全时：先读 `summary.txt` 里的「访问限制」段落，按提示降级（如只做 API 层分析），**不要伪造缺失的数据**。

### 第 1 步：确认项目是什么（读代码，不读 README 就下结论）

必读文件（按存在性）：
1. `README*` — 只看宣称，**不作为结论依据**
2. 依赖清单：`package.json` / `pyproject.toml` / `go.mod` / `Cargo.toml` / `pom.xml` / `*.csproj`
3. 入口点：`main.py` / `app.py` / `src/main.rs` / `src/index.ts` / `Program.cs`
4. 核心业务目录的前 1–3 个文件（通常是 `src/` 下最大的那个）
5. `.github/workflows/*` — 判断 CI 是否真的在跑

判断 README 与代码是否一致，不一致要在报告第 2 节点明（这是高价值发现）。

### 第 2 步：深审（按模块，不是按文件）

对照 `references/` 逐项执行，**每项结论都要落到 `文件:行号`**：

| 维度 | 参考文件 | 最低要求 |
|---|---|---|
| 结构与架构 | `inspection-standards.md` | 画出模块地图，指出耦合/重复/死代码 |
| Bug 与代码质量 | `code-quality.md` | 仅报告真实发现并给出行号，区分 [确认]/[疑似]/[建议]；没有发现时明确说明，不凑问题数量 |
| 安全 | `security-audit.md` | 读过脚本给出的高危命中并复核数据流，判断真实性 |
| 性能 | `performance-audit.md` | 只写静态可判定的瓶颈，**未实测不得给数字** |
| UI/UX | `ui-ux-audit.md` | 无界面则写"不适用+权重处理方式" |
| 工程化 | 本文件下方清单 | 14 项逐项打勾并给证据 |
| 仓库健康度 | `inspector-data.json` 的 `repository` 段 | 区分"累计"与"当前"，标明抽样 |

读文件用 `Read`，找模式用 `Grep`，不要用 `cat`/`grep` shell 命令。

### 第 3 步：同类项目研究（≥5 个，这是本技能的核心差异点）

`inspector-data.json` 的 `competitors.candidates` 已给出候选（已按 topic 相关性过滤）。但**不能直接抄这份列表**：

1. 对每个候选，用 `WebFetch` 打开 `https://github.com/<owner>/<repo>`，确认它真的解决同类问题
2. 读它的 README 功能清单 + 依赖清单 + 一个核心文件，核实功能是否真实存在
3. 记下许可证（`references/competitor-research.md` 有兼容矩阵）
4. 剔除不相关的（同类工具、聚合列表、教程），补足到 ≥5 个
5. 候选不足时，补充分搜索：
   ```
   topic:<核心topic>
   "<核心功能>" in:description language:<lang>
   ```

### 第 4 步：评分

严格按 `references/scoring-system.md` 的 10 维权重（合计 100）。硬规则：
- 有 P0 → 安全性 ≤ 8/15
- 未实测性能 → 性能 ≤ 7/10
- UI 未实测 → UI ≤ 6/10
- 证据不足的维度：降权 + 标 `*` + 在第 19 节说明缺什么

**总分必须等于各维度之和**（校验器会算）。

### 第 5 步：写报告

```bash
mkdir -p 体检报告
# 文件名严格为：GitHub-<项目名>-体检报告-<YYYY-MM-DD>.md
```

按 `templates/full-report.md` 的 20 个章节写，一节不少。同时按需产出 `templates/executive-summary.md`（一页摘要）。

写作硬要求：
- 每条问题含：**级别 / 文件路径 / 行号 / 原因 / 影响 / 复现 / 修复 / 优先级理由**
- 不把"建议优化"包装成"安全漏洞"
- 不编造 CVE 编号、性能数字、竞品 star 数
- 第 18 节的 AI 提示词要能直接粘给 Codex/Claude Code 执行（先分析→再改→保留功能→跑测试）

### 第 6 步：自检（不通过不许交付）

```bash
python "$SKILL_DIR/scripts/report-validator.py" "体检报告/GitHub-xxx-体检报告-2026-11-23.md"
```

- 退出码 0：可交付（warning 尽量清掉）
- 退出码 1：按列出项逐条修复后重跑，**不要绕过**
- 常见修复：补章节、补文件行号、补齐评分表、补竞品链接、删掉"待填写"

### 第 7 步：交付

用 `present_files` 展示 Markdown 报告（若同时生成了 HTML，放第一个）。回复里给：
1. 总分与等级
2. 最该先修的实际问题（最多 3 件，带文件名；无问题则明确说明）
3. 报告路径
4. 明确说明哪些维度未验证

## 工程化 14 项速查

README / 安装说明 / 开发环境 / 构建脚本 / 测试 / CI / 自动发版 / 版本规范 / 依赖更新机器人 / Issue 模板 / PR 模板 / CONTRIBUTING / LICENSE / SECURITY.md

每项给"有/无 + 证据路径"，别只写"较好"。

## 硬性纪律

1. **只读**：不 push、不建分支、不改用户仓库
2. **不伪造**：拿不到就说拿不到，未验证就写未验证
3. **不追问**：能自己查的绝不问用户（技术栈、用途、范围都自己判断）
4. **不只看 README**：结论必须来自代码
5. **不夸大**：一个仓库出现 10 个 P0 说明定级失控，复核每个 P0 的利用路径
6. **不传代码**：不上传源码到第三方；OSV 只发送"包名+版本"

## 常见故障处理

| 现象 | 处理 |
|---|---|
| API 403 且 `X-RateLimit-Remaining: 0` | 匿名限流。降低 `--budget`，或让用户设置 `GITHUB_TOKEN`；报告中声明抽样 |
| 仓库 404 | 不存在或私有。私有需用户授权，不绕过 |
| `git clone` 失败 | 脚本自动回退 zipball；都失败则转 API-only，并在报告注明无代码层证据 |
| 仓库过大克隆超时 | 先自行准备本地源码副本，再用 `--local` 按子目录检查；`--max-files` 只限制静态遍历，不减少下载 |
| 依赖无 lockfile | 依赖漏洞维度标"未充分验证"，不得给"安全"结论 |
| 找不到 5 个竞品 | 说明检索词与失败原因，**不许编造项目** |

## 参考文件索引

| 文件 | 什么时候读 |
|---|---|
| `references/inspection-standards.md` | 每次开始前（证据等级、阶段划分、覆盖度标准） |
| `references/security-audit.md` | 第 2 步安全审计时 |
| `references/code-quality.md` | 第 2 步质量审计时 |
| `references/performance-audit.md` | 判断能否实测、怎么写性能结论 |
| `references/ui-ux-audit.md` | 项目有界面时 |
| `references/competitor-research.md` | 第 3 步，含许可证矩阵 |
| `references/scoring-system.md` | 第 4 步打分，含一票否决项 |
| `templates/full-report.md` | 第 5 步写报告 |
| `templates/executive-summary.md` | 需要一页摘要时 |
| `templates/remediation-roadmap.md` | 第 16 节路线图 |
