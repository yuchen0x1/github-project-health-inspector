# 体检执行标准（inspection-standards）

本文件定义"什么算查过了"。所有结论必须能回答三个问题：**看的是哪份证据、证据从哪来、有没有可能看漏**。

---

## 1. 四个证据等级

报告里的每一条判断都必须标注等级，禁止把 L3/L4 写成 L1。

| 等级 | 名称 | 含义 | 举例 |
|---|---|---|---|
| L1 | 实测 | 真实运行/执行命令/调用 API 拿到的结果 | `git clone` 成功、OSV 返回 CVE 编号、`gh api` 返回 issue 列表 |
| L2 | 静态确证 | 读到源码原文，逻辑上可直接判定 | `subprocess.run(cmd, shell=True)` 且 `cmd` 来自 `request.args` |
| L3 | 静态推断 | 读到代码但依赖运行期条件才能确认 | 长函数、疑似循环依赖、可能有竞态 |
| L4 | 外部参考 | 来自文档、Issue、同类项目、官方公告 | "上游 issue #1234 报告过该问题" |

**写法要求**
- L1：给出命令输出摘要 + 时间戳
- L2：给出 `文件:行号` + 原文片段
- L3：必须写"推测依据"，并注明"需运行验证"
- L4：必须给链接

---

## 2. 阶段划分（大仓库必须分阶段）

| 阶段 | 动作 | 产出 | 失败怎么办 |
|---|---|---|---|
| P0 解析 | 解析 URL、确认仓库存在、读 meta | owner/repo/默认分支/许可证/归档状态 | 私有仓库→要求授权，不绕过 |
| P1 获取 | `git clone --depth 1`，失败回退 zipball | 本地源码 + commit sha | 两者都失败→降级为 API-only，并在报告注明"无代码层证据" |
| P2 识别 | 读清单文件 + 入口点 + 目录 | 技术栈/项目类型/构建方式 | 识别不出→标记 unknown，不要编 |
| P3 代码 | 按模块分批读核心源码 | 结构/质量/安全问题 | 文件数超限→按模块分批，列出未覆盖模块 |
| P4 仓库 | Issues/PR/Commit/Release/Contributors | 活跃度与维护风险 | 限流→抽样 + 说明抽样范围 |
| P5 竞品 | GitHub 搜索 + 逐个读关键文件 | ≥5 个可比项目 | 搜不到→说明检索词与失败原因，不虚构 |
| P6 评分 | 按 rubric 打分 | 100 分制 + 不确定性 | 证据不足→标"未充分验证"并扣分/降权 |
| P7 交付 | 写报告 + 跑校验器 | Markdown（+HTML） | 校验 error→修复后重跑 |

**"查过了"的最低标准**：核心源码（承担主要业务逻辑的目录）至少有 80% 的文件被读取或采样分析；README、清单文件、CI 配置、入口点必须 100% 读。

---

## 3. 大仓库 / Monorepo 策略

1. 先按顶层目录列出"模块地图"，标注每个模块的职责与代码量。
2. 按代码量降序，优先深审前 30% 的模块（通常覆盖 80% 逻辑）。
3. 每个模块单独给出结论，避免用"整体还行"掩盖某个模块的烂。
4. 明确列出 **未覆盖模块** 及原因（体量/生成代码/第三方 vendored）。
5. Monorepo 必须识别：`workspaces`、`lerna.json`、`pnpm-workspace.yaml`、`go.work`、`[workspace]`、多 `package.json`。

---

## 4. 多语言项目

按语言分别选检查手段，不要用 Python 的标准去看 Go：

| 语言 | 结构与依赖 | 质量 | 安全 |
|---|---|---|---|
| Python | `pyproject.toml` / requirements / poetry.lock | `ast` 分析函数长度、复杂度、裸 except | `eval/exec`、`pickle`、`yaml.load`、`subprocess shell=True` |
| JS/TS | `package.json` + lockfile | 函数长度启发式、`any` 滥用、未处理 Promise | `innerHTML`、`eval`、`child_process`、`postMessage` 无校验 |
| Go | `go.mod` / `go.sum` | 错误被忽略（`_ =`）、goroutine 泄漏、`defer` 在循环内 | `exec.Command` 拼接、`InsecureSkipVerify` |
| Rust | `Cargo.toml` / `Cargo.lock` | `unwrap/expect` 泛滥、`unsafe` 块 | `unsafe`、命令执行、`std::process` |
| Java/Kotlin | `pom.xml` / `build.gradle` | 异常吞掉、资源未 try-with-resources | 反序列化、SpEL 注入、`Runtime.exec` |
| C/C++ | CMake / vcpkg | 内存管理、缓冲区、`strcpy` 家族 | 溢出、UAF、格式化字符串 |
| PHP/Ruby | composer / bundler | 全局状态、魔法方法 | SQL 拼接、`eval`、反序列化 |
| C#/.NET | `.csproj` / NuGet | `async void`、`IDisposable` 未释放 | 反序列化、SQL 拼接、路径穿越 |

---

## 5. 数据完整性声明（必写）

报告第 19 节必须逐条列出：

- 匿名 API 限流导致哪些数据是抽样的，抽样了多少条
- 哪些目录/文件因体量、二进制、权限未读
- 哪些维度只有静态推断（L3），没有运行验证
- 竞品研究用了哪些检索词，是否有检索失败
- 依赖漏洞覆盖到哪些生态、哪些没覆盖（如 devDependencies、传递依赖）

**禁止**用"已全面审计"这类措辞掩盖覆盖不足。

---

## 6. 私有仓库 / 访问受限

- 404 或 403：先判断是"不存在"、"私有"还是"限流"（看 `X-RateLimit-Remaining`）。
- 私有时**不得**尝试绕过：不猜 token、不试镜像站、不要求用户上传源码到第三方。
- 正确做法：说明需要用户在本地配置 `GITHUB_TOKEN`（只读 scope）或提供已授权环境，然后重试。

---

## 7. 只读纪律

- 不 `push`、不建分支、不开 PR、不改文件、不装依赖到用户仓库。
- 需要执行构建/测试时，在临时目录或容器内进行，并在报告注明"在隔离副本中执行"。
- 不上传用户代码到任何第三方服务；OSV 查询只发送"包名+版本"，不发送源码。
