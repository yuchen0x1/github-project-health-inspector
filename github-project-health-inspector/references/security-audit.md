# 安全审计规程（security-audit）

核心纪律：**命中的是"信号"，不是"漏洞"。** 一个正则命中只能升级为 L2 结论，前提是你读懂了数据流。

---

## 1. 判定流程

```
正则/AST 命中
   ↓
读上下文 10~30 行，确认数据来源
   ↓
污点是否可控？（用户输入 / 环境变量 / 外部 API / 配置文件）
   ↓                      ↓ 否
  是                  降级为"加固建议 (P3)"
   ↓
是否有净化/参数化/白名单？
   ↓ 有            ↓ 无
P3 建议          L2 确认漏洞 → 定级
```

**必须写清 4 要素**：Source（污点源）→ Path（传播路径）→ Sink（危险函数）→ Impact（影响）。

---

## 2. OWASP Top 10 (2021) 检查清单

| 类别 | 代码里长什么样 | 检索线索 |
|---|---|---|
| A01 权限控制失效 | 只校验登录不校验归属；`/api/user/{id}` 直接查库 | `get_object_or_404(...pk=...)` 无 owner 过滤；前端隐藏按钮当权限 |
| A02 加密失败 | MD5/SHA1 存密码；硬编码密钥；`verify=False` | `md5(`, `sha1(`, `verify=False`, `InsecureSkipVerify` |
| A03 注入 | 字符串拼 SQL / shell / 模板 | `f"...{user}"` 进 execute；`shell=True`；`innerHTML` |
| A04 不安全设计 | 无限流、无验证码、重置密码可爆破 | 登录接口无 rate limit；OTP 无尝试次数 |
| A05 配置错误 | DEBUG 上线、CORS `*`、默认口令 | `DEBUG = True`、`Allow-Origin: *`、`admin/admin` |
| A06 易受攻击组件 | 旧版 lodash / log4j / requests | 见第 5 节 |
| A07 认证失败 | JWT 不校验签名 / 不过期 | `jwt.decode(token, options={"verify_signature": False})` |
| A08 数据完整性失败 | 反序列化不可信数据 | `pickle.loads`, `yaml.load`, `ObjectInputStream` |
| A09 日志监控失败 | 记录明文密码/Token | `logger.info(f"...password={pwd}")` |
| A10 SSRF | 用户传 URL 直接请求 | `requests.get(user_url)` 无域名白名单 |

---

## 3. 高危模式速查（按语言）

**Python**
```python
os.system(cmd)                                  # 命令注入
subprocess.run(cmd, shell=True)                 # cmd 含用户输入即 RCE
eval(...) / exec(...)                           # 任意代码
pickle.loads(untrusted)                         # 反序列化 RCE
yaml.load(x)                                    # 缺 Loader 即危险
render_template_string(user_input)              # SSTI
open(os.path.join(base, user_path))             # 路径穿越（需 normpath 校验）
requests.get(user_url)                          # SSRF
hashlib.md5(password)                           # 口令哈希不当
```

**JS/TS**
```js
el.innerHTML = userInput                        // XSS
eval(...) / new Function(...)                   // 代码注入
child_process.exec(`ls ${dir}`)                 // 命令注入
app.use(cors({origin: '*'}))                    // 跨域过宽
jwt.decode(t)                                   // 未验签
fs.readFile(path.join(base, req.query.f))       // 路径穿越
```

**Go**
```go
exec.Command("sh", "-c", userInput)             // 命令注入
db.Query("... WHERE id=" + id)                  // SQL 注入
&http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}
http.Get(req.URL.Query().Get("u"))              // SSRF
```

**Java**
```java
Runtime.getRuntime().exec(userInput)
stmt.executeQuery("... " + userInput)
new ObjectInputStream(in).readObject()          // 反序列化
DocumentBuilderFactory 未禁用外部实体         // XXE
```

---

## 4. 密钥与凭据

**真实泄漏的判断标准**（三者同时满足才算 L2）：
1. 命中高熵特征（AWS `AKIA...`、`ghp_...`、`sk-ant-...`、私钥头）
2. 不在 `.example` / 文档 / 测试 fixture 中
3. 未被 `os.environ` / `process.env` 包裹

**输出格式**
```
[P0] 疑似 AWS Access Key 硬编码
文件：config/settings.py:42
证据：AWS_ACCESS_KEY_ID = "AKIA****************"
判断：非示例文件，未走环境变量，前缀符合 AWS 格式 → 高置信度真实凭据
影响：任何能读仓库的人可直接调用云资源，产生费用与数据泄露
处置：立即轮换密钥 → 改用环境变量/密钥管理 → 用 git filter-repo 清理历史 → 开启 secret scanning
```

**不要**把 `password = os.getenv("DB_PASSWORD")`、`api_key = "your-api-key-here"`、测试里的 `token = "test-token"` 报成漏洞。

---

## 5. 依赖漏洞（必须拿编号）

- 工具：OSV.dev（免费、无需鉴权、覆盖 npm/PyPI/Go/Maven/NuGet/RubyGems/Packagist/crates.io/Pub）
- **只有拿到确切版本（lockfile 或 `==` pin）才能查**；只有范围约束（`^1.2.3`、`>=3.1`）时必须写"无法确定实际版本，未能验证"
- 必须区分 **项目自身依赖** 与 **examples/tests 目录里的依赖**，后者不能算项目本体的漏洞
- 输出必须含：包名、版本、CVE/GHSA 编号、来源文件、修复版本、链接

**判定口径**
- 项目自身依赖命中 ≥1 个 High/Critical → P1
- 仅 devDependencies / 示例目录命中 → P2，并注明"不在生产依赖路径"
- 无 lockfile → 该维度标"未充分验证"，不能给满分

---

## 6. GitHub Actions 专项

| 问题 | 危害 | 判定 |
|---|---|---|
| `permissions: write-all` | Token 可改仓库 | P1 |
| `pull_request_target` + checkout PR head + 执行 | 外部 PR 直接 RCE 并窃取 secrets | **P0** |
| `${{ github.event.issue.title }}` 直接进 `run:` | 脚本注入 | P1 |
| `uses: xxx@main`（非 SHA） | 上游被投毒即失陷 | P2 |
| `echo ${{ secrets.X }}` | 日志泄漏 | P1 |
| 未声明 `permissions` | 默认权限过宽 | P3 |

---

## 7. 分级锚点（避免全标 P0）

- **P0**：无需前提条件即可被外部利用，且造成凭据泄漏 / RCE / 数据全量泄露 / 资金损失。**必须给出攻击路径**。
- **P1**：需要登录或特定配置，但影响核心安全边界。
- **P2**：需要较高前提，或影响面有限（如仅内部工具）。
- **P3**：加固建议、纵深防御、规范问题。

**一个仓库出现 10 个 P0 基本说明定级失控。** 复核：每个 P0 都问一句"外部攻击者拿到 URL 后能在几步内利用？"

---

## 8. 不能做的事

- 不能把"没有参数化查询"直接说成"已被注入"（除非能指出可控输入）
- 不能虚构 CVE 编号或 CVSS 分数
- 不能把依赖列表里存在某包说成"存在漏洞"，必须有 OSV/NVD 返回
- 不能声称"扫描了全部文件"却没读过核心模块
- 不能因为没扫出问题就说"安全状况良好"，要写"在 X 范围、Y 手段内未发现"
