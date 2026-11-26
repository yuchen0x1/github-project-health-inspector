#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
github-inspector.py — GitHub 项目全方位体检 · 数据采集器

设计原则
  1. 只依赖 Python 标准库（Python 3.8+），不假定任何第三方包已安装。
  2. 只读：克隆到临时/工作目录，绝不写回用户仓库。
  3. 诚实：每个结论都带 evidence（文件路径/行号/API 字段），拿不到的数据显式标记为
     unavailable，绝不编造。
  4. 分阶段：meta / clone / analyze / github / competitors 可单独运行，便于大仓库分块审计。

用法
  python github-inspector.py https://github.com/owner/repo
  python github-inspector.py owner/repo --out ./out --stage analyze
  python github-inspector.py <url> --token $GITHUB_TOKEN

输出
  <out>/inspector-data.json   结构化数据（供模型消费）
  <out>/summary.txt           人类可读摘要（供快速掌握全局）
  <out>/repo/                 浅克隆源码（始终保留，供人工复核）
"""

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0.1"
UA = "github-project-health-inspector/%s (+https://workbuddy.cn)" % VERSION
API = "https://api.github.com"

# ---------------------------------------------------------------------------
# 常量表
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor",
    "dist", "build", "out", "target", "bin", "obj", "Debug", "Release",
    ".venv", "venv", "env", "virtualenv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".next", ".nuxt", ".svelte-kit", ".turbo",
    ".idea", ".vscode", ".vs", "coverage", "htmlcov", ".nyc_output",
    "Pods", "Carthage", ".gradle", ".dart_tool", "site-packages",
    "third_party", "3rdparty", "external", ".terraform", ".cache",
    "tmp", "temp", "logs", "models", "weights", "checkpoints",
}

LANG_BY_EXT = {
    ".py": "Python", ".pyi": "Python", ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript",
    ".vue": "Vue", ".svelte": "Svelte",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".scala": "Scala", ".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++",
    ".hh": "C++", ".hxx": "C++", ".ipp": "C++",
    ".cs": "C#", ".fs": "F#", ".vb": "Visual Basic",
    ".rb": "Ruby", ".php": "PHP", ".pl": "Perl", ".lua": "Lua",
    ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang",
    ".hs": "Haskell", ".ml": "OCaml", ".clj": "Clojure", ".r": "R", ".R": "R",
    ".jl": "Julia", ".nim": "Nim", ".zig": "Zig", ".sol": "Solidity",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell",
    ".bat": "Batchfile", ".cmd": "Batchfile",
    ".sql": "SQL", ".html": "HTML", ".htm": "HTML", ".css": "CSS",
    ".scss": "SCSS", ".sass": "SCSS", ".less": "LESS",
    ".md": "Markdown", ".mdx": "Markdown", ".rst": "reStructuredText",
    ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".json": "JSON",
    ".xml": "XML", ".proto": "Protocol Buffer", ".graphql": "GraphQL",
    ".dockerfile": "Dockerfile", ".tf": "HCL", ".hcl": "HCL",
}

# 只对"真正的源码"做质量/安全深度分析
CODE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".swift", ".m", ".mm", ".c", ".h", ".cc",
    ".cpp", ".hpp", ".cs", ".rb", ".php", ".dart", ".scala", ".lua", ".sh",
    ".bash", ".ps1", ".sol",
}
TEXT_EXTS = CODE_EXTS | {
    ".md", ".txt", ".yml", ".yaml", ".toml", ".json", ".ini", ".cfg", ".conf",
    ".env", ".sql", ".html", ".css", ".xml", ".lock", ".gradle", ".properties",
}

MANIFESTS = {
    "package.json": "npm/Node",
    "package-lock.json": "npm lock",
    "yarn.lock": "Yarn lock",
    "pnpm-lock.yaml": "pnpm lock",
    "bun.lockb": "Bun lock",
    "requirements.txt": "pip",
    "pyproject.toml": "Python (PEP 621/Poetry)",
    "setup.py": "setuptools",
    "setup.cfg": "setuptools",
    "Pipfile": "Pipenv",
    "poetry.lock": "Poetry lock",
    "Pipfile.lock": "Pipenv lock",
    "environment.yml": "Conda",
    "go.mod": "Go modules",
    "go.sum": "Go lock",
    "Cargo.toml": "Cargo",
    "Cargo.lock": "Cargo lock",
    "pom.xml": "Maven",
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle (Kotlin DSL)",
    "settings.gradle": "Gradle",
    "composer.json": "Composer",
    "composer.lock": "Composer lock",
    "Gemfile": "Bundler",
    "Gemfile.lock": "Bundler lock",
    "pubspec.yaml": "pub (Dart/Flutter)",
    "pubspec.lock": "pub lock",
    "CMakeLists.txt": "CMake",
    "Makefile": "Make",
    "meson.build": "Meson",
    "vcpkg.json": "vcpkg",
    "conanfile.txt": "Conan",
    "*.csproj": "MSBuild (.NET)",
    "*.sln": "MSBuild solution",
    "packages.config": "NuGet",
    "packages.lock.json": "NuGet lock",
    "Podfile": "CocoaPods",
    "Package.swift": "SwiftPM",
    "mix.exs": "Mix",
    "dune-project": "Dune",
    "stack.yaml": "Stack",
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "compose.yml": "Docker Compose",
}

FRAMEWORK_SIGNATURES = {
    # python
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI", "tornado": "Tornado",
    "aiohttp": "aiohttp", "starlette": "Starlette", "streamlit": "Streamlit",
    "gradio": "Gradio", "celery": "Celery", "scrapy": "Scrapy", "pytorch": "PyTorch",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "transformers": "HuggingFace Transformers",
    "langchain": "LangChain", "llama-index": "LlamaIndex", "openai": "OpenAI SDK",
    "anthropic": "Anthropic SDK", "pydantic": "Pydantic", "sqlalchemy": "SQLAlchemy",
    "pytest": "pytest", "click": "Click", "typer": "Typer", "rich": "Rich",
    # js/ts
    "react": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt",
    "svelte": "Svelte", "@sveltejs/kit": "SvelteKit", "angular": "Angular",
    "@angular/core": "Angular", "express": "Express", "koa": "Koa", "fastify": "Fastify",
    "nestjs": "NestJS", "@nestjs/core": "NestJS", "electron": "Electron",
    "tauri": "Tauri", "@tauri-apps/api": "Tauri", "vite": "Vite",
    "webpack": "Webpack", "rollup": "Rollup", "esbuild": "esbuild", "turbo": "Turborepo",
    "jest": "Jest", "vitest": "Vitest", "mocha": "Mocha", "playwright": "Playwright",
    "@playwright/test": "Playwright", "cypress": "Cypress", "tailwindcss": "Tailwind CSS",
    "prisma": "Prisma", "mongoose": "Mongoose", "sequelize": "Sequelize",
    "typeorm": "TypeORM", "react-native": "React Native", "expo": "Expo",
    "three": "Three.js", "chart.js": "Chart.js", "echarts": "ECharts",
    "@modelcontextprotocol/sdk": "MCP SDK",
    # 注意：langchain 在上面的 python 段已映射为 "LangChain"，此处不能再写一次
    # （同一 dict 里重复 key 会让前一个值被静默覆盖）。langchain.js 的识别
    # 靠 npm 依赖名 "langchain" 命中原 key 的 label，无需单独条目。
    # go / rust / java / php / ruby
    "gin-gonic/gin": "Gin", "labstack/echo": "Echo", "gofiber/fiber": "Fiber",
    "actix-web": "Actix Web", "axum": "Axum", "tokio": "Tokio", "tauri-build": "Tauri",
    "spring-boot": "Spring Boot", "spring-webmvc": "Spring MVC",
    "laravel/framework": "Laravel", "symfony/": "Symfony", "rails": "Ruby on Rails",
}

PROJECT_TYPE_RULES = [
    ("browser-extension", ["manifest.json:manifest_version"]),
    ("mobile-android", ["android/app/build.gradle", "AndroidManifest.xml"]),
    ("mobile-ios", ["*.xcodeproj", "Info.plist", "Podfile"]),
    ("mobile-flutter", ["pubspec.yaml:flutter"]),
    ("mobile-react-native", ["package.json:react-native"]),
    ("desktop-electron", ["package.json:electron"]),
    ("desktop-tauri", ["src-tauri/tauri.conf.json", "Cargo.toml:tauri"]),
    ("desktop-qt", ["*.pro", "CMakeLists.txt:Qt5", "CMakeLists.txt:Qt6"]),
    ("desktop-dotnet", ["*.csproj:UseWPF", "*.csproj:UseWindowsForms"]),
    ("desktop-java", ["pom.xml:javafx", "build.gradle:javafx"]),
    ("ai-application", ["requirements.txt:torch", "requirements.txt:transformers",
                        "pyproject.toml:openai", "package.json:openai",
                        "package.json:@modelcontextprotocol/sdk"]),
    ("web-fullstack", ["package.json:next", "package.json:nuxt", "package.json:@sveltejs/kit"]),
    ("web-frontend", ["package.json:react", "package.json:vue", "package.json:svelte",
                      "package.json:angular"]),
    ("web-backend", ["package.json:express", "package.json:fastify", "requirements.txt:flask",
                     "requirements.txt:fastapi", "requirements.txt:django", "go.mod:gin",
                     "pom.xml:spring-boot"]),
    ("cli", ["setup.py:entry_points", "pyproject.toml:scripts", "package.json:bin"]),
    ("library", ["setup.py", "pyproject.toml", "package.json:main"]),
]

STANDARD_FILES = {
    "readme": ["README.md", "README.rst", "README.txt", "readme.md", "README"],
    "license": ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "LICENSE-APACHE"],
    "changelog": ["CHANGELOG.md", "CHANGELOG.rst", "CHANGES.md", "CHANGES.rst", "CHANGES",
                  "HISTORY.md", "HISTORY.rst", "RELEASE_NOTES.md", "NEWS.md"],
    "contributing": ["CONTRIBUTING.md", "CONTRIBUTING.rst", ".github/CONTRIBUTING.md",
                     "docs/CONTRIBUTING.md", "docs/contributing.rst", "docs/contributing.md"],
    "code_of_conduct": ["CODE_OF_CONDUCT.md", ".github/CODE_OF_CONDUCT.md"],
    "security_policy": ["SECURITY.md", ".github/SECURITY.md"],
    "gitignore": [".gitignore"],
    "editorconfig": [".editorconfig"],
    "dockerfile": ["Dockerfile", "docker/Dockerfile", "dockerfile"],
    "makefile": ["Makefile", "makefile"],
}

# --- 密钥 / 凭据特征（只做正则初筛，命中后必须人工确认上下文） ---
SECRET_PATTERNS = [
    ("AWS Access Key ID", r"\bAKIA[0-9A-Z]{16}\b", "critical"),
    ("AWS Secret Access Key", r"(?i)aws(.{0,20})?(secret|private).{0,20}?['\"][0-9A-Za-z/+=]{40}['\"]", "critical"),
    ("GitHub Token (classic)", r"\bghp_[0-9A-Za-z]{36}\b", "critical"),
    ("GitHub Token (fine-grained)", r"\bgithub_pat_[0-9A-Za-z_]{60,}\b", "critical"),
    ("GitHub OAuth", r"\bgho_[0-9A-Za-z]{36}\b", "critical"),
    ("GitLab Token", r"\bglpat-[0-9A-Za-z_\-]{20,}\b", "critical"),
    ("Slack Token", r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b", "critical"),
    ("Stripe Live Key", r"\bsk_live_[0-9A-Za-z]{20,}\b", "critical"),
    ("Google API Key", r"\bAIza[0-9A-Za-z_\-]{35}\b", "high"),
    ("OpenAI Key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b", "high"),
    ("Anthropic Key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b", "high"),
    ("Private Key Block", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----", "critical"),
    ("JWT Token", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b", "medium"),
    ("Hardcoded Password", r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"][^'\"\s]{6,}['\"]", "high"),
    ("Hardcoded API Key", r"(?i)\b(api[_-]?key|apikey|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{12,}['\"]", "high"),
    ("DB Connection String", r"(?i)\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:'\"]+:[^\s@'\"\/]+@", "critical"),
    ("Alibaba Cloud AK", r"\bLTAI[0-9A-Za-z]{12,24}\b", "high"),
    ("Tencent Cloud SecretId", r"\bAKID[0-9A-Za-z]{32}\b", "high"),
]

# --- 危险代码特征（语言无关的正则初筛，需结合上下文判定） ---
RISK_PATTERNS = [
    # 注入 / RCE
    ("命令执行 (shell)", r"(?<![\w.])(os\.system|os\.popen|subprocess\.(?:call|run|Popen|check_output)\([^)]*shell\s*=\s*True)", "high", "命令注入 / RCE"),
    ("动态代码执行", r"(?<![\w.])(eval|exec)\s*\(|new\s+Function\s*\(", "high", "代码注入"),
    ("反序列化", r"(pickle\.loads?|cPickle\.loads?|yaml\.load\((?!.*Loader)|Marshal\.load|unserialize\()", "high", "反序列化 RCE"),
    ("SQL 字符串拼接", r"(?i)(execute|query|executemany)\s*\(\s*(f['\"]|['\"].*?%s.*?['\"]\s*%|['\"].*?\+)", "high", "SQL 注入"),
    ("SQL 拼接 (JS)", r"(?i)(query|execute)\s*\(\s*`[^`]*\$\{", "high", "SQL 注入"),
    ("DOM XSS", r"(\.innerHTML\s*=|dangerouslySetInnerHTML|document\.write\s*\(|v-html\s*=|\[innerHTML\]\s*=)", "medium", "XSS"),
    ("禁用 TLS 校验", r"(verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true|CURLOPT_SSL_VERIFYPEER\s*,\s*0)", "high", "中间人攻击"),
    ("弱哈希用于口令", r"(?i)(md5|sha1)\s*\(\s*(password|passwd|pwd|secret)", "high", "口令存储不安全"),
    ("不安全随机数", r"(?i)\brandom\.(random|randint|choice)\b(?=.*(?:token|password|secret|key|salt))", "medium", "可预测随机数"),
    ("路径穿越风险", r"(os\.path\.join\([^)]*request|open\(\s*[^)]*request\.|send_file\([^)]*request\.)", "medium", "路径穿越"),
    ("SSRF 风险", r"(requests\.(?:get|post)\(\s*(?:url|request\.)|fetch\(\s*(?:url|req\.|request\.)|axios\.(?:get|post)\(\s*(?:url|req\.))", "medium", "SSRF"),
    ("危险反序列化 (Java)", r"(ObjectInputStream|readObject\s*\(\)|XMLDecoder)", "high", "反序列化 RCE"),
    ("调试模式开启", r"(?i)(DEBUG\s*=\s*True|debug\s*:\s*true)", "medium", "信息泄露"),
    ("CORS 通配", r"(?i)(Access-Control-Allow-Origin['\"]?\s*[:,]\s*['\"]\*|cors\(\s*\{\s*origin\s*:\s*['\"]\*['\"])", "medium", "跨域策略过宽"),
    ("禁用 CSRF", r"(?i)(csrf_exempt|@csrf\.exempt|csrfProtection\s*:\s*false)", "medium", "CSRF 防护缺失"),
    ("明文 HTTP", r"http://(?!localhost|127\.0\.0\.1|0\.0\.0\.0|schemas\.|www\.w3\.org|xmlns)[a-zA-Z0-9.\-]+", "low", "传输未加密"),
]

TEST_DIR_HINTS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "testing", "testdata"}
TEST_FILE_RE = re.compile(r"(_test\.go|\.test\.[jt]sx?|\.spec\.[jt]sx?|^test_.*\.py$|.*_test\.py$|Test[A-Z]\w*\.(java|kt|cs)|.*Tests?\.swift)$")

TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG|OPTIMIZE|REFACTOR)\b[:\s]*(.{0,120})", re.I)
COMMENTED_CODE_RE = re.compile(r"^\s*(#|//)\s*(if|for|while|return|def|function|const|let|var|class|import|from|public|private)\b")

# 跳过"文档/示例/测试夹具"里的凭据扫描。必须按路径段匹配：
# 旧写法用裸 `test|spec|mock` 子串，会把 inspector.py（含 "spec"）、latest.py（含 "test"）
# 这类真实源码整体跳过，等于对它们关闭了密钥扫描。
IGNORE_SECRET_FILES = re.compile(
    r"(\.example$|\.sample$|\.template$|\.dist$|\.md$|\.rst$|\.txt$"
    r"|(^|/)(docs?|examples?|samples?|demos?|fixtures?|__tests__|tests?|specs?|testdata|benchmarks?)(/|$)"
    r"|(^|/)[^/]*(_test|\.test|_spec|\.spec)\.[A-Za-z0-9]+$)", re.I)

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

LOG_LINES = []


def log(msg, level="INFO"):
    line = "[%s] %s" % (level, msg)
    LOG_LINES.append(line)
    safe_print(line, file=sys.stderr)


def safe_print(text, file=None):
    stream = file or sys.stdout
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(enc, "backslashreplace").decode(enc), file=stream)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path, limit=2_000_000):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(limit)
    except Exception:
        return None
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def run_cmd(cmd, cwd=None, timeout=300):
    """执行外部命令，返回 (rc, stdout, stderr)。命令不存在时 rc=-1。"""
    try:
        p = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, shell=isinstance(cmd, str),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return p.returncode, p.stdout.decode("utf-8", "ignore"), p.stderr.decode("utf-8", "ignore")
    except FileNotFoundError:
        return -1, "", "command not found: %s" % (cmd if isinstance(cmd, str) else cmd[0])
    except subprocess.TimeoutExpired:
        return -2, "", "timeout after %ss" % timeout
    except Exception as e:  # noqa: BLE001
        return -3, "", str(e)


def have_cmd(name):
    return shutil.which(name) is not None


# ---------------------------------------------------------------------------
# GitHub API 访问（带预算与限流感知，匿名可用，可选 token）
# ---------------------------------------------------------------------------

class GitHubAPI:
    def __init__(self, token=None, budget=80, timeout=25):
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.budget = budget
        self.calls = 0
        self.timeout = timeout
        self.cache = {}
        self.notes = []
        self.rate = {"remaining": None, "limit": None, "reset": None, "authenticated": bool(self.token)}

    def _headers(self):
        h = {"User-Agent": UA, "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def get(self, path, params=None):
        """GET api.github.com/<path>。失败返回 dict(status, error)，不抛异常。"""
        url = path if path.startswith("http") else API + path
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme != "https" or parsed_url.netloc != "api.github.com":
            return {"status": -1, "data": None, "error": "refusing non-GitHub API URL"}
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if url in self.cache:
            return self.cache[url]
        if self.calls >= self.budget:
            out = {"status": 0, "error": "budget exhausted (local)", "data": None}
            self.cache[url] = out
            return out
        self.calls += 1
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8", "ignore") or "null")
                hdr = r.headers
                if hdr.get("X-RateLimit-Remaining"):
                    self.rate["remaining"] = int(hdr["X-RateLimit-Remaining"])
                    self.rate["limit"] = int(hdr.get("X-RateLimit-Limit", 0))
                    self.rate["reset"] = int(hdr.get("X-RateLimit-Reset", 0))
                out = {"status": r.status, "data": body, "error": None}
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read().decode("utf-8", "ignore")[:400]
            except Exception:
                pass
            remain = e.headers.get("X-RateLimit-Remaining") if e.headers else None
            reset = e.headers.get("X-RateLimit-Reset") if e.headers else None
            if remain is not None:
                self.rate["remaining"] = int(remain)
            if reset is not None:
                self.rate["reset"] = int(reset)
            hint = ""
            if e.code == 403 and remain == "0":
                reset_ts = int(reset) if reset else 0
                hint = "rate limited; resets at %s" % (
                    datetime.fromtimestamp(reset_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
                    if reset_ts else "unknown")
                self.notes.append("GitHub API 触发匿名限流（60 次/小时）。" + hint +
                                  " 建议提供 GITHUB_TOKEN 或等待重置。")
            elif e.code == 404:
                hint = "not found or no permission (private repo requires authorization)"
            elif e.code in (401,):
                hint = "unauthorized: token invalid or expired"
            out = {"status": e.code, "data": None, "error": hint or txt, "raw": txt}
        except Exception as e:  # noqa: BLE001
            out = {"status": -1, "data": None, "error": str(e)[:200]}
        self.cache[url] = out
        return out

    def get_all(self, path, params=None, max_pages=3, per_page=100):
        """翻页拉取列表接口。"""
        items, page = [], 1
        p = dict(params or {})
        p["per_page"] = per_page
        while page <= max_pages:
            p["page"] = page
            r = self.get(path, p)
            data = r.get("data")
            if not isinstance(data, list) or not data:
                break
            items.extend(data)
            if len(data) < per_page:
                break
            page += 1
        return items


# ---------------------------------------------------------------------------
# URL 解析
# ---------------------------------------------------------------------------

def parse_repo_url(raw):
    """解析各种 GitHub 仓库输入形式，返回 dict(owner, repo, ref, host)。"""
    s = (raw or "").strip().strip("<>\"'")
    if not s:
        raise ValueError("empty input")
    # owner/repo
    if re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", s):
        owner, repo = s.split("/")
        # 只能用 re.sub 去掉结尾 ".git"；rstrip(".git") 会把 latest/testing 之类
        # 以 g/t/i/. 结尾的仓库名截断成 lates/testin。
        repo = re.sub(r"\.git$", "", repo)
        if owner in (".", "..") or repo in ("", ".", ".."):
            raise ValueError("invalid owner/repo")
        return {"owner": owner, "repo": repo, "ref": None,
                "host": "github.com", "input": raw}
    if not re.match(r"^https?://", s):
        s = "https://" + s
    u = urllib.parse.urlparse(s)
    host = (u.netloc or "").lower()
    if host not in ("github.com", "www.github.com"):
        # 之前会带着 gitlab.com 的 owner/repo 去 clone github.com/owner/repo，
        # 悄悄分析成另一个仓库。宁可报错也不给错仓库的结论。
        raise ValueError("仅支持 github.com 仓库，收到 host=%s" % (host or "?"))
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("cannot parse owner/repo from URL: %s" % raw)
    owner, repo = [urllib.parse.unquote(part) for part in parts[:2]]
    repo = re.sub(r"\.git$", "", repo)
    if not all(re.fullmatch(r"[A-Za-z0-9_.\-]+", part) and part not in (".", "..")
               for part in (owner, repo)):
        raise ValueError("invalid owner/repo in URL")
    ref = None
    if len(parts) >= 4 and parts[2] in ("tree", "blob", "commit"):
        ref = urllib.parse.unquote("/".join(parts[3:]) if parts[2] != "commit" else parts[3])
        if any(ord(c) < 32 for c in ref):
            raise ValueError("invalid control character in ref")
    return {"owner": owner, "repo": repo, "ref": ref,
            "host": host, "input": raw}


def resolve_repo_ref(parsed, api):
    """Resolve branch names containing slashes before stripping a URL's subpath."""
    ref = parsed.get("ref")
    if not ref or "/" not in ref:
        return ref
    parts = ref.split("/")
    for length in range(min(len(parts), 16), 0, -1):
        candidate = "/".join(parts[:length])
        result = api.get("/repos/%s/%s/commits/%s" % (
            parsed["owner"], parsed["repo"], urllib.parse.quote(candidate, safe="")))
        if result.get("status") == 200:
            return candidate
        if result.get("status") != 404:
            break
    raise ValueError("无法确认 URL 的分支/提交（含斜杠的 ref 或源码路径）：%s" % ref)


# ---------------------------------------------------------------------------
# 获取源码：git clone 优先，失败回退 zipball
# ---------------------------------------------------------------------------

def fetch_source(parsed, dest, ref=None, timeout=600):
    dest = Path(dest)
    info = {"method": None, "ok": False, "error": None, "path": str(dest), "ref": ref,
            "size_mb": None, "commit": None}
    if dest.exists() or dest.is_symlink():
        info["error"] = "拒绝覆盖现有源码目录：%s" % dest
        return info
    try:
        import io
        import zipfile
        dest.parent.mkdir(parents=True, exist_ok=True)
        # All cleanup is confined to a directory created by this invocation.
        with tempfile.TemporaryDirectory(prefix=".inspector-source-", dir=str(dest.parent)) as work:
            staging = Path(work) / "repo"
            url = "https://github.com/%s/%s.git" % (parsed["owner"], parsed["repo"])
            if have_cmd("git"):
                cmd = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
                if ref:
                    cmd += ["--branch", ref]
                cmd += ["--", url, str(staging)]
                t0 = time.time()
                rc, out, err = run_cmd(cmd, timeout=timeout)
                info["elapsed_s"] = round(time.time() - t0, 1)
                if rc == 0:
                    rc2, out2, _ = run_cmd(["git", "rev-parse", "HEAD"], cwd=str(staging), timeout=30)
                    if rc2 == 0:
                        info["commit"] = out2.strip()
                    rc3, out3, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(staging), timeout=30)
                    if rc3 == 0:
                        info["branch"] = out3.strip()
                    if dest.exists() or dest.is_symlink():
                        raise ValueError("目标目录已存在，已保留：%s" % dest)
                    staging.rename(dest)
                    info.update(method="git-clone", ok=True,
                                size_mb=round(dir_size(dest) / 1048576, 2))
                    return info
                info["error"] = (err or out or "").strip()[:400]
                log("git clone 失败：%s（尝试 zipball 回退）" % info["error"][:120], "WARN")

            zb_url = "https://codeload.github.com/%s/%s/zip/%s" % (
                parsed["owner"], parsed["repo"], urllib.parse.quote(ref or "HEAD", safe=""))
            req = urllib.request.Request(zb_url, headers={"User-Agent": UA})
            max_download = 128 * 1024 * 1024
            with urllib.request.urlopen(req, timeout=timeout) as r:
                blob = r.read(max_download + 1)
            if len(blob) > max_download:
                raise ValueError("源码 ZIP 超过 128 MiB 下载上限")
            tmp = Path(work) / "archive"
            tmp.mkdir()
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                members = zf.infolist()
                if len(members) > 100_000 or sum(m.file_size for m in members) > 512 * 1024 * 1024:
                    raise ValueError("源码 ZIP 超过文件数或 512 MiB 展开上限")
                for member in members:
                    path = member.filename.replace("\\", "/")
                    pieces = path.rstrip("/").split("/")
                    mode = member.external_attr >> 16
                    if (not path or path.startswith("/") or any(p in ("", ".", "..") for p in pieces)
                            or any(":" in p for p in pieces) or stat.S_ISLNK(mode)):
                        raise ValueError("源码 ZIP 包含不安全路径或符号链接")
                zf.extractall(str(tmp))
            entries = list(tmp.iterdir())
            src = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp
            if dest.exists() or dest.is_symlink():
                raise ValueError("目标目录已存在，已保留：%s" % dest)
            src.rename(dest)
            info.update(method="zipball", ok=True, size_mb=round(dir_size(dest) / 1048576, 2))
        return info
    except Exception as e:  # noqa: BLE001
        info["error"] = (info.get("error") or "") + " | zipball: %s" % str(e)[:200]
        return info


def dir_size(path):
    total = 0
    for root, dirs, files in os.walk(str(path)):
        dirs[:] = [d for d in dirs if d != ".git" and not _is_link(Path(root) / d)]
        for f in files:
            try:
                if _is_link(Path(root) / f):
                    continue
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# 文件遍历
# ---------------------------------------------------------------------------

def _is_link(path):
    try:
        st = Path(path).lstat()
        return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def walk_files(root, max_files=8000, max_bytes_total=120 * 1024 * 1024, exclude_paths=()):
    """返回 (files[list of dict], stats)。跳过 .git/依赖/构建目录。"""
    root = Path(root)
    files = []
    skipped_dirs = []
    size_total = 0
    truncated = False
    excluded = {Path(path).resolve() for path in exclude_paths}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        rel_dir = os.path.relpath(dirpath, str(root))
        keep = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS or _is_link(Path(dirpath) / d) or (Path(dirpath) / d).resolve() in excluded:
                skipped_dirs.append(os.path.join(rel_dir, d).replace("\\", "/") if rel_dir != "." else d)
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            if _is_link(fp) or fp.resolve() in excluded:
                continue
            if len(files) >= max_files or size_total >= max_bytes_total:
                truncated = True
                break
            rel = os.path.relpath(str(fp), str(root)).replace("\\", "/")
            try:
                st = fp.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            ext = fp.suffix.lower()
            if ext in (".pyc", ".pyo", ".o", ".obj", ".class", ".jar", ".so", ".dll",
                       ".exe", ".dylib", ".a", ".lib", ".png", ".jpg", ".jpeg", ".gif",
                       ".ico", ".webp", ".svg", ".woff", ".woff2", ".ttf", ".eot",
                       ".mp4", ".mp3", ".zip", ".gz", ".7z", ".pdf", ".bin", ".onnx",
                       ".pt", ".pth", ".h5", ".pkl", ".npy", ".npz", ".parquet"):
                files.append({"path": rel, "size": st.st_size, "ext": ext, "binary": True})
                continue
            files.append({"path": rel, "size": st.st_size, "ext": ext, "binary": False})
            size_total += st.st_size
            if len(files) >= max_files or size_total > max_bytes_total:
                truncated = True
                break
        if truncated:
            break
    stats = {"file_count": len(files), "bytes_scanned": size_total,
             "skipped_dirs": skipped_dirs[:40], "truncated": truncated}
    return files, stats


# ---------------------------------------------------------------------------
# 技术栈识别
# ---------------------------------------------------------------------------

def load_json_safe(path):
    txt = read_text(path, 4_000_000)
    if txt is None:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


# --- 依赖解析（重写版）：只有锁文件/精确 pin 才算"已安装版本" ---

GENERIC_TOPICS = {
    "python", "javascript", "typescript", "java", "go", "golang", "rust", "cpp", "c",
    "php", "ruby", "swift", "kotlin", "hacktoberfest", "api", "web", "cli", "library",
    "tool", "tools", "opensource", "open-source", "framework", "app", "application",
    "machine-learning", "deep-learning", "ai", "automation", "productivity",
}


def _pep508(spec):
    """解析一条依赖声明 → (name, version_if_pinned, pinned, constraint)。

    允许没有版本号（裸包名也是一条依赖，只是无法查 CVE）；
    允许 Poetry 的 `^1.2` / `~1.2` 前缀（旧实现只认 PEP 440 操作符，
    会把 `httpx = "^0.27"` 整条丢掉）。
    """
    if not isinstance(spec, str):
        return None
    s = spec.strip().strip("\"'").strip()
    # 用 scheme 前缀判定而非 startswith("http")：后者会把 httpx / httpcore / httpie
    # 这些合法包名当成 URL 整条丢掉。
    if not s or s.startswith(("#", "-")) or re.match(r"^(git\+|https?://|file:|\.{1,2}/)", s):
        return None
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*(?:\[[^\]]*\])?\s*(?:\(\s*)?"
                 r"(\^|~=|~|===|==|>=|<=|!=|>|<)?\s*([0-9][A-Za-z0-9_.*!\-+]*)?", s)
    if not m:
        return None
    name, op, ver = m.group(1), m.group(2), m.group(3) or ""
    if name.lower() in PYPROJECT_NON_DEPS:
        return None
    pinned = op in ("==", "===") and bool(ver) and "*" not in ver
    constraint = ("%s%s" % (op, ver)) if op else ""
    return name, (ver if pinned else ""), pinned, constraint


# pyproject.toml 里哪些表装的是"依赖"，以及是数组还是 name=constraint 映射
PYPROJECT_ARRAY_TABLES = {
    "project": ("dependencies",),                 # PEP 621 主依赖
    "project.optional-dependencies": None,        # 任意 key 都是数组
    "dependency-groups": None,                    # PEP 735
    "tool.pdm.dev-dependencies": None,
    "build-system": ("requires",),
}
PYPROJECT_MAP_TABLES = (
    "tool.poetry.dependencies",
    "tool.poetry.dev-dependencies",
)
PYPROJECT_NON_DEPS = {"python", "requires-python", "version", "name", "description",
                      "readme", "license", "authors", "keywords", "classifiers",
                      "include", "exclude", "type", "requires"}


def _pyproject_map_entry(value):
    """Poetry 映射表的一行 → 约束字符串（str / list / {version=...}）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        v = value.get("version")
        return v if isinstance(v, str) else ""
    if isinstance(value, list):
        return _pyproject_map_entry(value[0]) if value else ""
    return ""


def _poetry_spec(name, value):
    constraint = _pyproject_map_entry(value).strip()
    # A bare Poetry version is exact, unlike a bare PEP 508 name.
    if re.fullmatch(r"\d+[A-Za-z0-9_.!+\-]*", constraint):
        constraint = "==" + constraint
    return name + constraint


def _pyproject_add(out, spec, table):
    p = _pep508(spec)
    if p and p[0].lower() not in PYPROJECT_NON_DEPS:
        out.append({"name": p[0], "version": p[1], "pinned": p[2],
                    "constraint": p[3], "table": table})


def _pyproject_deps_tomllib(txt):
    import tomllib
    data = tomllib.loads(txt)
    out = []
    project = data.get("project") or {}
    for spec in project.get("dependencies") or []:
        _pyproject_add(out, spec, "project.dependencies")
    for group, specs in (project.get("optional-dependencies") or {}).items():
        for spec in specs or []:
            _pyproject_add(out, spec, "project.optional-dependencies.%s" % group)
    for group, specs in (data.get("dependency-groups") or {}).items():
        for spec in specs or []:
            if isinstance(spec, str):
                _pyproject_add(out, spec, "dependency-groups.%s" % group)
    pdm_dev = (((data.get("tool") or {}).get("pdm") or {}).get("dev-dependencies") or {})
    for group, specs in pdm_dev.items():
        for spec in specs or []:
            if isinstance(spec, str):
                _pyproject_add(out, spec, "tool.pdm.dev-dependencies.%s" % group)
    for spec in (data.get("build-system") or {}).get("requires") or []:
        _pyproject_add(out, spec, "build-system.requires")
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    tables = [("dependencies", poetry.get("dependencies")),
              ("dev-dependencies", poetry.get("dev-dependencies"))]
    for group, body in (poetry.get("group") or {}).items():
        tables.append(("group.%s.dependencies" % group, (body or {}).get("dependencies")))
    for table, body in tables:
        for name, value in (body or {}).items():
            if name.lower() in PYPROJECT_NON_DEPS:
                continue
            _pyproject_add(out, _poetry_spec(name, value),
                           "tool.poetry." + table)
    return out


def _pyproject_deps_fallback(txt):
    """Python < 3.11 无 tomllib 时的行级解析：只在依赖表/依赖数组内取包名。

    旧实现是对整份文件跑正则，会把 requires-python / classifiers / keywords
    当成依赖（如包名 `requires-python`），污染依赖清单和 OSV 查询。
    """
    def strip_comment(line):
        return re.sub(r'''"(?:\\.|[^"\\])*"|'[^']*'|\#.*''',
                      lambda match: "" if match.group(0).startswith("#") else match.group(0), line)

    def array_strings(text):
        depth, objects, values = 0, 0, []
        for match in re.finditer(r'''"(?:\\.|[^"\\])*"|'[^']*'|[\[\]{}]''', text):
            token = match.group(0)
            if token == "[":
                depth += 1
            elif token == "]":
                depth -= 1
                if depth == 0:
                    return values
            elif token == "{":
                objects += 1
            elif token == "}":
                objects -= 1
            elif depth == 1 and objects == 0:
                try:
                    values.append(json.loads(token) if token.startswith('"') else token[1:-1])
                except ValueError:
                    pass
        return None

    out, sec, buf, in_array, array_table = [], "", [], False, ""
    for raw in txt.splitlines():
        line = strip_comment(raw)
        if in_array:
            buf.append(line)
            specs = array_strings(" ".join(buf))
            if specs is not None:
                for s in specs:
                    _pyproject_add(out, s, array_table)
                buf, in_array = [], False
            continue
        m = re.match(r"^\s*\[\[?([^\]]+)\]\]?\s*$", line)
        if m:
            sec = m.group(1).strip()
            continue
        if sec in PYPROJECT_ARRAY_TABLES:
            allowed = PYPROJECT_ARRAY_TABLES[sec]
            m = re.match(r"^\s*([A-Za-z0-9_.\-\"]+)\s*=\s*\[(.*)$", line)
            if not m:
                continue
            key = m.group(1).strip("\"'")
            if allowed is not None and key not in allowed:
                continue
            rest = m.group(2)
            specs = array_strings("[" + rest)
            if specs is not None:
                for s in specs:
                    _pyproject_add(out, s, "%s.%s" % (sec, key))
            else:
                buf, in_array, array_table = ["[" + rest], True, "%s.%s" % (sec, key)
        elif sec in PYPROJECT_MAP_TABLES or re.match(r"^tool\.poetry\.group\.[^.]+\.dependencies$", sec):
            m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*=\s*(.+)$", line)
            if not m or m.group(1).lower() in PYPROJECT_NON_DEPS:
                continue
            raw_val = m.group(2).strip()
            # 内联表 {version = "^1.8", optional = true} 要单独取 version
            inner = re.search(r"version\s*=\s*[\"']([^\"']+)[\"']", raw_val)
            val = inner.group(1) if inner else raw_val.strip("\"',")
            _pyproject_add(out, _poetry_spec(m.group(1), val), sec)
    return out


def parse_pyproject_deps(txt):
    try:
        return _pyproject_deps_tomllib(txt)
    except Exception:
        return _pyproject_deps_fallback(txt)


def _dep_scope(src):
    """区分"项目自身依赖"与"示例/测试目录里的依赖"。

    二者混在一起会把 examples 里锁定的旧版本 CVE 算到项目头上，属于误导。
    """
    low = (src or "").lower()
    if re.match(r"^(examples?|samples?|demo|docs?|tests?|benchmarks?|tutorials?)/", low) or \
       re.search(r"/(examples?|samples?|demo|tests?|__tests__|fixtures?)/", low):
        return "example_or_test"
    return "project"


def collect_manifest_deps(root, files):
    """解析各生态依赖。

    pinned=True 仅当版本来自锁文件或精确 '==' pin —— 这是可以拿去查 CVE 的唯一依据。
    声明式清单里的范围约束（^1.2.3 / >=3.1）只记录 constraint，绝不当作已安装版本。
    """
    raw = []

    def add(eco, name, ver, src, pinned, constraint="", direct=True):
        if not name or not isinstance(name, str) or len(name) > 120:
            return
        if not isinstance(ver, str):
            ver = ""
        raw.append({"ecosystem": eco, "name": name.strip(), "version": (ver or "").strip(),
                    "source_file": src, "pinned": bool(pinned and ver), "constraint": constraint,
                    "scope": _dep_scope(src), "direct": direct})

    rel_list = []
    for f in files:
        if not f.get("binary"):
            rel_list.append(f["path"])

    def base_of(rel):
        return rel.split("/")[-1]

    # ---------- 1. 锁文件优先 ----------
    lock_start = len(raw)
    for rel in rel_list:
        b = base_of(rel)
        if b in ("package-lock.json", "npm-shrinkwrap.json"):
            j = load_json_safe(root / rel)
            if isinstance(j, dict) and isinstance(j.get("packages"), dict):
                for p, meta in j["packages"].items():
                    if not p or "node_modules/" not in p:
                        continue
                    nm = p.split("node_modules/")[-1]
                    v = meta.get("version") if isinstance(meta, dict) else None
                    if v:
                        add("npm", nm, v, rel, True)
            elif isinstance(j, dict) and isinstance(j.get("dependencies"), dict):
                def walk(deps):
                    for n, meta in deps.items():
                        if isinstance(meta, dict):
                            add("npm", n, meta.get("version", ""), rel, bool(meta.get("version")))
                            if isinstance(meta.get("dependencies"), dict):
                                walk(meta["dependencies"])
                walk(j["dependencies"])
        elif b == "yarn.lock":
            txt = read_text(root / rel) or ""
            for m in re.finditer(r'^(?:"?)([^\s":,]+)@[^\n]*:\n(?:[^\n]*\n)*?\s{2}version\s+"?([\w.\-+]+)"?',
                                 txt, re.M):
                add("npm", m.group(1), m.group(2), rel, True)
        elif b == "pnpm-lock.yaml":
            txt = read_text(root / rel) or ""
            for m in re.finditer(r"^\s{2}(/?[^\s:]+):\s*\n(?:\s+.*\n)*?\s{4}version:\s*([\w.\-+]+)", txt, re.M):
                nm = m.group(1).replace("'", "").strip("/")
                if "/" in nm:
                    nm = nm.split("/")[-1] if not nm.startswith("@") else nm
                add("npm", nm, m.group(2), rel, True)
        elif b == "Pipfile.lock":
            j = load_json_safe(root / rel)
            if isinstance(j, dict):
                for sec in ("default", "develop"):
                    for n, meta in (j.get(sec) or {}).items():
                        spec = _pep508(n + str(meta.get("version", ""))) if isinstance(meta, dict) else None
                        if spec:
                            add("PyPI", n, spec[1], rel, spec[2], spec[3])
        elif b == "poetry.lock":
            txt = read_text(root / rel) or ""
            for blk in txt.split("[[package]]")[1:]:
                nm = re.search(r'^name\s*=\s*"([^"]+)"', blk, re.M)
                vr = re.search(r'^version\s*=\s*"([^"]+)"', blk, re.M)
                if nm and vr:
                    add("PyPI", nm.group(1), vr.group(1), rel, True)
        elif b in ("uv.lock", "pdm.lock"):
            # uv/pdm 的 lock 与 poetry 同为 TOML，[[package]] 块结构一致
            txt = read_text(root / rel) or ""
            for blk in txt.split("[[package]]")[1:]:
                nm = re.search(r'^name\s*=\s*"([^"]+)"', blk, re.M)
                vr = re.search(r'^version\s*=\s*"([^"]+)"', blk, re.M)
                if nm and vr:
                    add("PyPI", nm.group(1), vr.group(1), rel, True)
        elif b == "Cargo.lock":
            txt = read_text(root / rel) or ""
            for blk in txt.split("[[package]]")[1:]:
                nm = re.search(r'name\s*=\s*"([^"]+)"', blk)
                vr = re.search(r'version\s*=\s*"([^"]+)"', blk)
                if nm and vr:
                    add("crates.io", nm.group(1), vr.group(1), rel, True)
        elif b == "Gemfile.lock":
            txt = read_text(root / rel) or ""
            for m in re.finditer(r"^\s{4}([A-Za-z0-9_.\-]+) \(([\d][\w.\-]*)\)", txt, re.M):
                add("RubyGems", m.group(1), m.group(2), rel, True)
        elif b == "composer.lock":
            j = load_json_safe(root / rel)
            if isinstance(j, dict):
                for sec in ("packages", "packages-dev"):
                    for p in (j.get(sec) or []):
                        add("Packagist", p.get("name", ""), str(p.get("version", "")).lstrip("v"), rel, True)
        elif b == "pubspec.lock":
            txt = read_text(root / rel) or ""
            cur = None
            for line in txt.splitlines():
                m0 = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
                if m0:
                    cur = m0.group(1)
                m1 = re.match(r'^\s{4}version:\s*"?([\w.\-+]+)"?', line)
                if cur and m1:
                    add("Pub", cur, m1.group(1), rel, True)
                    cur = None
        elif b == "packages.lock.json":
            j = load_json_safe(root / rel)
            if isinstance(j, dict):
                for fw, pkgs in (j.get("dependencies") or {}).items():
                    for n, meta in (pkgs or {}).items():
                        if isinstance(meta, dict) and meta.get("resolved"):
                            add("NuGet", n, str(meta["resolved"]), rel, True)

    # 锁文件里的是"实际安装版本"，但多数是传递依赖；标记 direct=False，
    # 避免把传递依赖当成项目主框架（例如把 starlette 认成 Flask 的框架）。
    for e in raw[lock_start:]:
        e["direct"] = False

    # ---------- 2. 精确 pin 的 requirements ----------
    for rel in rel_list:
        b = base_of(rel)
        if b.endswith(".txt") and ("requirement" in b.lower() or b == "constraints.txt"):
            txt = read_text(root / rel) or ""
            for line in txt.splitlines():
                line = line.split("#")[0].strip()
                if not line or line.startswith("-"):
                    continue
                p = _pep508(line)
                if p:
                    add("PyPI", p[0], p[1], rel, p[2], p[3])
        elif b == "environment.yml":
            txt = read_text(root / rel) or ""
            for line in txt.splitlines():
                m = re.match(r"^\s*-\s*([A-Za-z0-9_.\-]+)=([\w.\-]+)", line)
                if m:
                    # Conda build versions are not PyPI versions; keep them unverified.
                    add("Conda", m.group(1), "", rel, False, m.group(2))

    # ---------- 3. go.mod（MVS 选出的最小版本，等价于实际构建版本） ----------
    for rel in rel_list:
        if base_of(rel) == "go.mod":
            txt = read_text(root / rel) or ""
            for m in re.finditer(r"^\s*(?:require\s+)?([\w.\-]+\.[\w.\-]+/[\w.\-/]+)\s+v([\w.\-+]+)",
                                 txt, re.M):
                add("Go", m.group(1), m.group(2), rel, True, "go MVS")

    # ---------- 4. 声明式清单：只记约束，不当已安装版本 ----------
    for rel in rel_list:
        b = base_of(rel)
        if b == "package.json" and "/node_modules/" not in rel:
            j = load_json_safe(root / rel)
            if not isinstance(j, dict):
                continue
            for key in ("dependencies", "devDependencies", "peerDependencies",
                        "optionalDependencies"):
                for n, v in (j.get(key) or {}).items():
                    v = str(v)
                    exact = bool(re.fullmatch(r"\d+\.\d+\.\d+[\w.\-+]*", v))
                    add("npm", n, v if exact else "", rel, exact, v)
        elif b == "pyproject.toml":
            txt = read_text(root / rel) or ""
            for d in parse_pyproject_deps(txt):
                add("PyPI", d["name"], d["version"], rel, d["pinned"], d["constraint"])
        elif b == "Cargo.toml":
            txt = read_text(root / rel) or ""
            for m in re.finditer(r"^\s*([A-Za-z0-9_\-]+)\s*=\s*\{?[^\n]*?version\s*=\s*\"([^\"]+)\"", txt, re.M):
                add("crates.io", m.group(1), "", rel, False, m.group(2))
            for m in re.finditer(r"^\[dependencies\.([A-Za-z0-9_\-]+)\]\s*\n(?:.*\n)*?version\s*=\s*\"([^\"]+)\"",
                                 txt, re.M):
                add("crates.io", m.group(1), "", rel, False, m.group(2))
        elif b == "Gemfile":
            txt = read_text(root / rel) or ""
            for m in re.finditer(r"^\s*gem\s+['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]+)['\"])?", txt, re.M):
                c = m.group(2) or ""
                exact = bool(re.fullmatch(r"=?\s*\d+\.\d+\.\d+", c))
                add("RubyGems", m.group(1), c.lstrip("= ") if exact else "", rel, exact, c)
        elif b == "composer.json":
            j = load_json_safe(root / rel)
            if isinstance(j, dict):
                for key in ("require", "require-dev"):
                    for n, v in (j.get(key) or {}).items():
                        if "/" not in n:
                            continue
                        exact = bool(re.fullmatch(r"v?\d+\.\d+\.\d+", str(v)))
                        add("Packagist", n, str(v).lstrip("v") if exact else "", rel, exact, str(v))
        elif b == "pubspec.yaml":
            txt = read_text(root / rel) or ""
            for m in re.finditer(r"^\s{2}([a-z_][a-z0-9_]*):\s*[\^~>=<]*\s*([\d][\w.\-+]*)", txt, re.M):
                exact = not re.search(r"[\^~><=]", m.group(0).split(":", 1)[1])
                add("Pub", m.group(1), m.group(2) if exact else "", rel, exact, m.group(2))

    # Never promote example versions into project evidence or discard a second
    # installed version: either would produce incorrect vulnerability results.
    groups = {}
    for e in raw:
        name = e["name"].lower()
        if e["ecosystem"] == "PyPI":
            name = re.sub(r"[-_.]+", "-", name)
        groups.setdefault((e["ecosystem"], name, e["scope"]), []).append(e)
    merged = []
    for entries in groups.values():
        pinned = {}
        for entry in entries:
            if entry["pinned"]:
                pinned.setdefault(entry["version"], entry)
        selected = list(pinned.values()) or [entries[0]]
        for entry in selected:
            related = [e for e in entries if not e["pinned"] or e["version"] == entry["version"]]
            entry["direct"] = any(e.get("direct") for e in related)
            if not entry.get("constraint"):
                entry["constraint"] = next((e["constraint"] for e in related if e.get("constraint")), "")
            merged.append(entry)
    return merged


def detect_stack(root, files):
    """技术栈 + 项目类型 + 单仓多项目识别。"""
    root = Path(root)
    rel_set = {f["path"] for f in files}
    names = [p.split("/")[-1] for p in rel_set]

    # 语言统计：源码语言占比只按"代码文件字节"算，避免被文档/配置稀释
    lang_files, lang_bytes, lang_code = Counter(), Counter(), Counter()
    code_bytes = 0
    for f in files:
        if f.get("binary"):
            continue
        lang = LANG_BY_EXT.get(f["ext"])
        if lang:
            lang_files[lang] += 1
            lang_bytes[lang] += f["size"]
            if f["ext"] in CODE_EXTS:
                lang_code[lang] += f["size"]
                code_bytes += f["size"]
    langs = [{"language": k, "files": lang_files[k], "bytes": lang_bytes[k],
              "pct_code": round(lang_code[k] / max(1, code_bytes) * 100, 1),
              "pct_all_files": round(lang_files[k] / max(1, len(files)) * 100, 1)}
             for k in sorted(lang_bytes, key=lambda x: (-lang_code[x], -lang_bytes[x]))[:12]]
    langs = [l for l in langs if l["pct_code"] > 0] or langs

    # 构建/包管理
    build = []
    for m, label in MANIFESTS.items():
        if m.startswith("*"):
            if any(n.endswith(m[1:]) for n in names):
                build.append({"file": m[1:], "system": label})
        elif m in names or any(p.endswith("/" + m) for p in rel_set):
            build.append({"file": m, "system": label})

    # 框架识别（读清单内容）
    deps = collect_manifest_deps(root, files)
    # 框架识别只看"直接依赖"，否则 lockfile 里的传递依赖会把无关框架也算进来
    dep_names = {d["name"].lower() for d in deps if d.get("direct", True)}
    blob = ""
    for cand in ("package.json", "requirements.txt", "pyproject.toml", "go.mod",
                 "Cargo.toml", "composer.json", "Gemfile", "pubspec.yaml"):
        for r in rel_set:
            if r.split("/")[-1] == cand and r.count("/") <= 1:
                blob += (read_text(root / r, 300_000) or "")[:60_000]
    blob_l = blob.lower()
    frameworks = []
    for sig, label in FRAMEWORK_SIGNATURES.items():
        hit_dep = sig.lower() in dep_names
        hit_blob = bool(re.search(r"(?<![a-z0-9])" + re.escape(sig.lower()) + r"(?![a-z0-9])", blob_l))
        if hit_dep or hit_blob:
            if label not in [f["name"] for f in frameworks]:
                frameworks.append({"name": label,
                                   "signal": sig,
                                   "confidence": "high" if hit_dep else "medium"})

    # 项目类型
    ptypes = []
    def sig_present(sig):
        if ":" in sig:
            f, token = sig.split(":", 1)
            for r in rel_set:
                if r.split("/")[-1] == f or (f.startswith("*") and r.endswith(f[1:])):
                    txt = (read_text(root / r, 400_000) or "").lower()
                    if token.lower() in txt:
                        return True
            return False
        return any(r == sig or r.endswith("/" + sig) or (sig.startswith("*") and r.endswith(sig[1:]))
                   for r in rel_set)

    for ptype, sigs in PROJECT_TYPE_RULES:
        if any(sig_present(s) for s in sigs):
            ptypes.append(ptype)

    # 单仓多项目（monorepo）
    pkg_jsons = [p for p in rel_set if p.split("/")[-1] == "package.json" and "node_modules" not in p]
    monorepo = {"detected": False, "signals": [], "packages": []}
    for p in rel_set:
        if p.split("/")[-1] in ("lerna.json", "pnpm-workspace.yaml", "nx.json", "turbo.json", "go.work"):
            monorepo["signals"].append(p)
    root_pkg = load_json_safe(root / "package.json") if "package.json" in rel_set else None
    if isinstance(root_pkg, dict) and root_pkg.get("workspaces"):
        monorepo["signals"].append("package.json#workspaces")
    if len(pkg_jsons) > 2:
        monorepo["signals"].append("%d package.json files" % len(pkg_jsons))
    if any("Cargo.toml" == p.split("/")[-1] for p in rel_set) and \
       any("[workspace]" in (read_text(root / p, 200_000) or "") for p in rel_set if p.endswith("Cargo.toml")):
        monorepo["signals"].append("Cargo workspace")
    if monorepo["signals"]:
        monorepo["detected"] = True
        monorepo["packages"] = sorted({p.rsplit("/", 1)[0] for p in pkg_jsons if "/" in p})[:40]

    # 入口点
    entry_hints = ["main.py", "app.py", "manage.py", "index.js", "server.js", "main.go",
                   "src/main.rs", "src/main.ts", "Program.cs", "Main.java", "main.ts",
                   "index.tsx", "App.tsx", "electron/main.js", "src-tauri/src/main.rs"]
    entrypoints = [p for p in rel_set if p.split("/")[-1] in entry_hints or p in entry_hints][:15]

    # 标准文件核查
    standard = {}
    for key, cands in STANDARD_FILES.items():
        hit = next((c for c in cands if c in rel_set or any(p.endswith("/" + c) for p in rel_set)), None)
        standard[key] = hit
    gh_dir = [p for p in rel_set if p.startswith(".github/")]
    standard["github_workflows"] = len([p for p in gh_dir if p.startswith(".github/workflows/")])
    standard["issue_templates"] = len([p for p in gh_dir if "ISSUE_TEMPLATE" in p or p.lower().endswith("issue_template.md")])
    standard["pr_template"] = any("PULL_REQUEST_TEMPLATE" in p.upper() for p in rel_set)
    standard["dependabot"] = any("dependabot" in p.lower() for p in rel_set)
    standard["renovate"] = any("renovate" in p.lower() for p in rel_set)

    # 许可证识别
    license_name = None
    for cand in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "LICENSE-MIT",
                 "LICENSE-APACHE"):
        if cand in rel_set or any(p.split("/")[-1] == cand for p in rel_set):
            real = cand if cand in rel_set else next(p for p in rel_set if p.split("/")[-1] == cand)
            head = (read_text(root / real, 8000) or "")[:6000]
            low = head.lower()
            # 许可证文件里通常不写 SPDX 名称，必须按条款特征判断
            if "gnu affero general public" in low:
                license_name = "AGPL-3.0"
            elif "gnu lesser general public" in low:
                license_name = "LGPL"
            elif "gnu general public license" in low:
                license_name = "GPL-3.0" if "version 3" in low else "GPL-2.0"
            elif "apache license" in low and "version 2.0" in low:
                license_name = "Apache-2.0"
            elif "mozilla public license" in low:
                license_name = "MPL-2.0"
            elif "redistribution and use in source and binary forms" in low:
                # BSD 三条款有"不得用作者名背书"，二条款没有
                license_name = "BSD-3-Clause" if "neither the name" in low else "BSD-2-Clause"
            elif "permission is hereby granted, free of charge" in low:
                license_name = "MIT"
            elif "isc license" in low or "permission to use, copy, modify, and/or distribute" in low:
                license_name = "ISC"
            elif "unlicense" in low or "free and unencumbered software released into the public domain" in low:
                license_name = "Unlicense"
            elif "creative commons" in low:
                license_name = "CC"
            break

    return {
        "languages": langs,
        "build_systems": build,
        "frameworks": frameworks[:18],
        "project_types": ptypes or ["unknown"],
        "monorepo": monorepo,
        "entrypoints": entrypoints,
        "standard_files": standard,
        "license_detected": license_name,
        "dependency_count": len(deps),
        "dependencies": deps[:600],
        "top_dirs": sorted({p.split("/")[0] for p in rel_set if "/" in p})[:40],
    }


# ---------------------------------------------------------------------------
# 结构分析
# ---------------------------------------------------------------------------

def analyze_structure(root, files, stack):
    root = Path(root)
    src_files = [f for f in files if not f.get("binary")]
    depth_max, depth_hist = 0, Counter()
    for f in src_files:
        d = f["path"].count("/")
        depth_max = max(depth_max, d)
        depth_hist[d] += 1

    large = sorted([f for f in src_files if f["size"] > 300_000],
                   key=lambda x: -x["size"])[:15]

    # 顶层目录职责一览
    top = defaultdict(lambda: {"files": 0, "bytes": 0})
    for f in src_files:
        if "/" in f["path"]:
            k = f["path"].split("/")[0]
            top[k]["files"] += 1
            top[k]["bytes"] += f["size"]
    top_layout = [{"dir": k, **v} for k, v in sorted(top.items(), key=lambda x: -x[1]["files"])[:25]]

    # 疑似重复实现：同名文件出现在多个目录（可能是复制粘贴的模块）
    base_map = defaultdict(list)
    for f in src_files:
        if f["ext"] in CODE_EXTS:
            base_map[f["path"].split("/")[-1]].append(f["path"])
    dup_names = {k: v for k, v in base_map.items() if len(v) > 2 and
                 k not in ("__init__.py", "index.js", "index.ts", "mod.rs", "main.rs",
                           "setup.py", "conftest.py", "package.json", "tsconfig.json")}
    dup_samples = [{"name": k, "copies": v[:5]} for k, v in list(dup_names.items())[:12]]

    # 疑似未使用/遗留文件（启发式，仅提示）
    stale_hints = []
    for f in src_files:
        low = f["path"].lower()
        for marker in ("_old", "_backup", "_bak", ".orig", "_deprecated", "copy", "_copy", ".rej", ".old"):
            if marker in low:
                stale_hints.append(f["path"])
                break
    stale_hints = stale_hints[:20]

    # 测试覆盖广度（按目录/文件存在性估算，不代表真实覆盖率）
    test_files = [f["path"] for f in src_files
                  if TEST_FILE_RE.search(f["path"].split("/")[-1])
                  or any(part in TEST_DIR_HINTS for part in f["path"].lower().split("/")[:-1])]
    code_files = [f for f in src_files if f["ext"] in CODE_EXTS
                  and not any(part in TEST_DIR_HINTS for part in f["path"].lower().split("/")[:-1])]
    ratio = round(len(test_files) / max(1, len(code_files)) * 100, 1)

    return {
        "depth_max": depth_max,
        "depth_histogram": dict(sorted(depth_hist.items())),
        "top_layout": top_layout,
        "large_files": [{"path": f["path"], "kb": round(f["size"] / 1024)} for f in large],
        "duplicate_filename_groups": dup_samples,
        "stale_file_hints": stale_hints,
        "test_file_count": len(test_files),
        "code_file_count": len(code_files),
        "test_to_code_ratio_pct": ratio,
        "test_frameworks": sorted({d["name"] for d in stack["dependencies"]
                                   if d["name"].lower() in ("pytest", "jest", "vitest", "mocha",
                                                            "junit", "nunit", "xunit", "rspec",
                                                            "phpunit", "testify", "gtest")}),
        "ci_workflows": stack["standard_files"].get("github_workflows", 0),
    }


# ---------------------------------------------------------------------------
# 代码质量分析
# ---------------------------------------------------------------------------

def analyze_python_ast(root, path, rel):
    out = {"functions": [], "complex_functions": [], "bare_excepts": [], "mutable_defaults": [],
           "broad_except": 0, "lines": 0, "classes": 0}
    txt = read_text(root / rel)
    if not txt:
        return out
    out["lines"] = txt.count("\n") + 1
    try:
        tree = ast.parse(txt)
    except SyntaxError as e:
        out["parse_error"] = "%s (line %s)" % (e.msg, e.lineno)
        return out

    class CxVisitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _score(self, node):
            cx = 1
            for child in ast.walk(node):
                if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                                      ast.With, ast.AsyncWith, ast.Assert, ast.IfExp)):
                    cx += 1
                elif isinstance(child, ast.BoolOp):
                    cx += len(child.values) - 1
            return cx

        def visit_FunctionDef(self, node):
            self._handle(node)

        def visit_AsyncFunctionDef(self, node):
            self._handle(node)

        def _handle(self, node):
            end = getattr(node, "end_lineno", node.lineno)
            length = end - node.lineno + 1
            cx = self._score(node)
            args = len(node.args.args) + len(node.args.kwonlyargs) + len(node.args.posonlyargs)
            rec = {"name": node.name, "line": node.lineno, "length": length,
                   "complexity": cx, "args": args}
            out["functions"].append(rec)
            if length > 80 or cx > 15 or args > 7:
                out["complex_functions"].append(rec)
            # mutable default args
            for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    out["mutable_defaults"].append({"func": node.name, "line": d.lineno})
            self.generic_visit(node)

    try:
        CxVisitor().visit(tree)
    except RecursionError:
        pass

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out["classes"] += 1
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                out["bare_excepts"].append({"line": node.lineno, "code": "except:"})
            elif isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
                out["broad_except"] += 1
    return out


JS_FUNC_RE = re.compile(
    r"(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(|"
    r"(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_$]+)\s*=>")


def analyze_js_funcs(root, rel):
    """基于大括号配对的函数长度启发式分析（非 AST，精度有限，标记为 heuristic）。"""
    txt = read_text(root / rel)
    if not txt:
        return []
    lines = txt.splitlines()
    results = []
    for i, line in enumerate(lines):
        m = JS_FUNC_RE.search(line)
        if not m:
            continue
        name = m.group(1) or m.group(2) or "<anonymous>"
        depth, started, end = 0, False, None
        for j in range(i, min(i + 400, len(lines))):
            seg = lines[j]
            depth += seg.count("{") - seg.count("}")
            if not started and "{" in seg:
                started = True
            if started and depth <= 0 and j > i:
                end = j
                break
        if end:
            results.append({"name": name, "line": i + 1, "length": end - i + 1})
    return results


def analyze_quality(root, files, stack):
    root = Path(root)
    code_files = [f for f in files if f["ext"] in CODE_EXTS and not f.get("binary")]
    loc_by_lang = Counter()
    total_lines = 0
    long_funcs = []
    complex_funcs = []
    bare_excepts = []
    mutable_defaults = []
    parse_errors = []
    todos = []
    commented = []
    big_func_count = 0
    func_count = 0

    # 限制深度分析规模，保证大仓库也能跑完
    scan_cap = 1500
    scanned = 0
    for f in code_files:
        if scanned >= scan_cap:
            break
        rel = f["path"]
        ext = f["ext"]
        txt = read_text(root / rel, 1_500_000)
        if txt is None:
            continue
        scanned += 1
        n = txt.count("\n") + 1
        total_lines += n
        lang = LANG_BY_EXT.get(ext, ext)
        loc_by_lang[lang] += n

        for m in TODO_RE.finditer(txt):
            todos.append({"file": rel, "line": txt[:m.start()].count("\n") + 1,
                          "kind": m.group(1).upper(), "text": m.group(2).strip()[:100]})
        for idx, line in enumerate(txt.splitlines()[:4000]):
            if COMMENTED_CODE_RE.match(line):
                commented.append({"file": rel, "line": idx + 1, "text": line.strip()[:80]})

        if ext == ".py":
            r = analyze_python_ast(root, None, rel)
            if r.get("parse_error"):
                parse_errors.append({"file": rel, "error": r["parse_error"]})
            func_count += len(r["functions"])
            for fn in r["functions"]:
                if fn["length"] > 80:
                    big_func_count += 1
            for fn in r["complex_functions"]:
                if len(complex_funcs) < 40:
                    complex_funcs.append({"file": rel, **fn})
                if fn["length"] > 120 and len(long_funcs) < 40:
                    long_funcs.append({"file": rel, **fn})
            for b in r["bare_excepts"][:5]:
                bare_excepts.append({"file": rel, **b})
            for md in r["mutable_defaults"][:5]:
                mutable_defaults.append({"file": rel, **md})
        elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".vue", ".svelte"):
            fns = analyze_js_funcs(root, rel)
            func_count += len(fns)
            for fn in fns:
                if fn["length"] > 100:
                    big_func_count += 1
                    if len(long_funcs) < 40:
                        long_funcs.append({"file": rel, **fn, "heuristic": True})

    # 重复代码检测：6 行窗口哈希
    dup_blocks = detect_duplication(root, code_files)

    return {
        "loc_total": total_lines,
        "loc_by_language": dict(loc_by_lang.most_common(12)),
        "files_scanned_for_quality": scanned,
        "function_count_sampled": func_count,
        "long_functions": long_funcs[:30],
        "complex_functions": complex_funcs[:30],
        "long_function_ratio_pct": round(big_func_count / max(1, func_count) * 100, 1),
        "bare_except_count": len(bare_excepts),
        "bare_except_samples": bare_excepts[:15],
        "mutable_default_args": mutable_defaults[:15],
        "parse_errors": parse_errors[:15],
        "todo_count": len(todos),
        "todo_by_kind": dict(Counter(t["kind"] for t in todos)),
        "todo_samples": todos[:25],
        "commented_out_code_count": len(commented),
        "commented_out_samples": commented[:15],
        "duplication": dup_blocks,
        "lint_tools_available": [t for t in ("ruff", "flake8", "eslint", "golangci-lint",
                                             "clippy", "pylint", "mypy", "tsc", "semgrep", "bandit")
                                 if have_cmd(t)],
    }


def detect_duplication(root, code_files, window=6, max_files=800):
    """归一化行窗口哈希找重复块。返回百分比与样例。"""
    hashes = defaultdict(list)
    norm_line = lambda s: re.sub(r"\s+", " ", s.strip())  # noqa: E731
    scanned = 0
    total_windows = 0
    for f in code_files[:max_files]:
        txt = read_text(root / f["path"], 800_000)
        if not txt:
            continue
        scanned += 1
        lines = []
        for i, raw in enumerate(txt.splitlines(), 1):
            s = norm_line(raw)
            if not s or len(s) < 4 or s in ("{", "}", "});", ");", "]", "end", "pass", "return"):
                continue
            lines.append((i, s))
        if len(lines) < window:
            continue
        for i in range(len(lines) - window + 1):
            chunk = "\n".join(x[1] for x in lines[i:i + window])
            if len(chunk) < 80:
                continue
            h = hashlib.md5(chunk.encode("utf-8", "ignore")).hexdigest()
            hashes[h].append((f["path"], lines[i][0]))
            total_windows += 1
    dup = {h: locs for h, locs in hashes.items()
           if len(locs) > 1 and len({l[0] for l in locs}) > 1}
    dup_ratio = round(len(dup) / max(1, total_windows) * 100, 2)
    samples = []
    for h, locs in sorted(dup.items(), key=lambda x: -len(x[1]))[:10]:
        samples.append({"occurrences": len(locs), "locations": [{"file": a, "line": b} for a, b in locs[:4]]})
    return {"duplicate_window_ratio_pct": dup_ratio, "files_scanned": scanned,
            "sample_blocks": samples, "method": "normalized 6-line window hashing"}


# ---------------------------------------------------------------------------
# 安全审计
# ---------------------------------------------------------------------------

def mask_python_noise(txt):
    """把 Python 注释与多行字符串（docstring）抹成空白，保留行号。

    目的：文档里的示例代码（如 `DEBUG = True` 的解释、`http://img.website.com`）
    不应被当作真实风险。单行字符串保留，因为硬编码凭据通常就是单行赋值。
    """
    try:
        import io
        import tokenize
        toks = list(tokenize.generate_tokens(io.StringIO(txt).readline))
    except Exception:
        return txt
    starts = [0]
    for line in txt.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    out = list(txt)
    for tok in toks:
        is_docstring = (tok.type == tokenize.STRING and tok.start[0] != tok.end[0])
        if tok.type != tokenize.COMMENT and not is_docstring:
            continue
        a = starts[tok.start[0] - 1] + tok.start[1]
        b = starts[min(tok.end[0], len(starts)) - 1] + tok.end[1]
        for i in range(a, min(b, len(out))):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


# 示例/文档/测试目录中的"危险代码"多数是教学样例与断言，不计入风险清单
NOISE_PATH_RE = re.compile(
    r"^(docs?|examples?|samples?|demo|benchmarks?|tutorials?|cookbook|tests?|specs?|"
    r"__tests__|fixtures?|e2e)/"
    r"|/(docs?|examples?|samples?|demo|tutorials?|__tests__|tests?|specs?|fixtures?)/", re.I)

# 保留域名/RFC 2606 示例域名，不应算作"明文传输风险"
EXAMPLE_HOST_RE = re.compile(
    r"(example\.(com|org|net)|localhost|127\.0\.0\.1|0\.0\.0\.0|\.test\b|\.invalid\b|\.example\b)", re.I)


def analyze_security(root, files, stack):
    root = Path(root)
    findings = {"secrets": [], "risks": [], "workflow_issues": [], "config_issues": [],
                "env_committed": [], "dependency_audit": None, "scan_scope": {}}

    text_files = [f for f in files if not f.get("binary") and
                  (f["ext"] in TEXT_EXTS or f["path"].split("/")[-1].startswith(".env")) and
                  f["size"] < 800_000]
    scanned = 0
    noise_skipped = 0
    for f in text_files[:2500]:
        rel = f["path"]
        txt = read_text(root / rel, 900_000)
        if not txt:
            continue
        scanned += 1
        masked = mask_python_noise(txt) if f["ext"] == ".py" else txt
        lines = txt.splitlines()
        # --- 密钥 ---
        if not IGNORE_SECRET_FILES.search(rel):
            for name, pattern, sev in SECRET_PATTERNS:
                for m in re.finditer(pattern, txt):
                    ln = txt[:m.start()].count("\n") + 1
                    snippet = lines[ln - 1].strip()[:140] if ln - 1 < len(lines) else ""
                    # 过滤明显的占位符与环境变量读取
                    if re.search(r"(os\.environ|process\.env|getenv|your[_-]?|xxx|\*\*\*|<.*>|\$\{)", snippet, re.I):
                        continue
                    if re.search(r"(example|sample|dummy|placeholder|changeme|redacted)", snippet, re.I):
                        continue
                    findings["secrets"].append({
                        "type": name, "severity": sev, "file": rel, "line": ln,
                        "evidence": "[REDACTED: credential-like value]",
                        "note": "正则初筛命中，需人工确认是否为真实凭据（可能是测试样例/占位符）",
                    })
                    break
        # --- 危险代码 ---
        if f["ext"] in CODE_EXTS:
            if NOISE_PATH_RE.search(rel):
                noise_skipped += 1
            else:
                for name, pattern, sev, cat in RISK_PATTERNS:
                    for m in re.finditer(pattern, masked):
                        ln = masked[:m.start()].count("\n") + 1
                        ev = (lines[ln - 1].strip()[:130] if ln - 1 < len(lines) else "")
                        if cat == "传输未加密" and EXAMPLE_HOST_RE.search(ev):
                            continue
                        findings["risks"].append({
                            "pattern": name, "category": cat, "severity": sev,
                            "file": rel, "line": ln,
                            "evidence": (lines[ln - 1].strip()[:130] if ln - 1 < len(lines) else ""),
                        })
                        if len(findings["risks"]) > 400:
                            break
        # --- 提交到仓库的 .env ---
        base = rel.split("/")[-1]
        if base in (".env", ".env.local", ".env.production", ".env.development") or \
           (base.startswith(".env") and not any(x in base for x in ("example", "sample", "template"))):
            findings["env_committed"].append({"file": rel, "size": f["size"]})

    # 去重 + 限流输出
    seen = set()
    uniq_risks = []
    for r in findings["risks"]:
        k = (r["category"], r["file"], r["line"])
        if k in seen:
            continue
        seen.add(k)
        uniq_risks.append(r)
    findings["risks"] = uniq_risks[:200]

    seen_s = set()
    uniq_sec = []
    for s in findings["secrets"]:
        k = (s["type"], s["file"], s["line"])
        if k in seen_s:
            continue
        seen_s.add(k)
        uniq_sec.append(s)
    findings["secrets"] = uniq_sec[:80]

    # --- GitHub Actions 审计 ---
    for f in files:
        p = f["path"]
        if not p.startswith(".github/workflows/") or not p.endswith((".yml", ".yaml")):
            continue
        txt = read_text(root / p, 300_000) or ""
        issues = []
        if re.search(r"permissions\s*:\s*write-all", txt):
            issues.append({"type": "GITHUB_TOKEN 权限过大 (write-all)", "severity": "high"})
        if "pull_request_target" in txt:
            issues.append({"type": "使用 pull_request_target（可被 PR 注入攻击）", "severity": "high",
                           "note": "若同时 checkout PR head 代码并执行，存在 RCE 风险"})
        if re.search(r"uses:\s*[^\s]+@(master|main|v\d+)\s*$", txt, re.M) and \
           not re.search(r"uses:\s*[^\s]+@[0-9a-f]{40}", txt):
            issues.append({"type": "Action 未固定到 commit SHA", "severity": "medium"})
        if re.search(r"\$\{\{\s*github\.event\.(issue|pull_request|comment)\.(title|body|head\.ref)", txt):
            issues.append({"type": "直接将不可信事件字段插入脚本（脚本注入风险）", "severity": "high"})
        if re.search(r"(echo|print).*\$\{\{\s*secrets\.", txt):
            issues.append({"type": "在日志中回显 secrets", "severity": "high"})
        if not re.search(r"permissions\s*:", txt):
            issues.append({"type": "未声明 permissions（默认继承仓库权限，偏宽）", "severity": "low"})
        if issues:
            findings["workflow_issues"].append({"file": p, "issues": issues})

    # --- 配置类问题 ---
    cfg_checks = [
        ("允许所有来源 CORS", r"Access-Control-Allow-Origin['\"]?\s*[:,]\s*['\"]\*", "medium"),
        ("调试模式默认开启", r"(?i)DEBUG\s*[:=]\s*(True|true|1)\s*$", "medium"),
        ("关闭 TLS 校验", r"(?i)(verify\s*=\s*False|rejectUnauthorized\s*:\s*false)", "high"),
        ("明文密码出现在配置", r"(?i)(password|passwd)\s*[:=]\s*['\"][^'\"]{6,}", "high"),
    ]
    for f in text_files[:800]:
        if f["path"].split("/")[-1] not in ("settings.py", "config.py", "config.js", "config.ts",
                                            "application.properties", "application.yml",
                                            "appsettings.json", "docker-compose.yml", ".env.example",
                                            "config.yaml", "config.json", "nginx.conf"):
            continue
        txt = read_text(root / f["path"], 200_000) or ""
        for label, pat, sev in cfg_checks:
            m = re.search(pat, txt, re.M)
            if m:
                ln = txt[:m.start()].count("\n") + 1
                findings["config_issues"].append({"issue": label, "severity": sev,
                                                  "file": f["path"], "line": ln})

    findings["scan_scope"] = {
        "text_files_scanned": scanned,
        "code_files_total": len([f for f in files if f["ext"] in CODE_EXTS]),
        "code_files_skipped_as_examples": noise_skipped,
        "secret_patterns_used": len(SECRET_PATTERNS),
        "risk_patterns_used": len(RISK_PATTERNS),
        "python_masking": "注释与多行字符串(docstring)已抹除后匹配，减少文档示例误报",
        "note": "本阶段为静态正则/AST 初筛。命中项需结合上下文与数据流确认，"
                "未命中不代表绝对安全（正则无法覆盖业务逻辑漏洞）。",
    }
    return findings


# ---------------------------------------------------------------------------
# 依赖漏洞（OSV.dev，无需鉴权）
# ---------------------------------------------------------------------------

def osv_query(deps, max_pkgs=300, chunk=100):
    """只查询有确切版本的依赖。没有锁定版本 → 明确标记 unverified，不猜。

    优先查询"项目自身"依赖；示例/测试目录的依赖单独计数，不混入主结论。
    """
    pinned = [d for d in deps if d.get("pinned") and d.get("version")]
    proj = [d for d in pinned if d.get("scope", "project") == "project"]
    ex = [d for d in pinned if d.get("scope") != "project"]
    # 项目自身依赖排前，示例/测试目录排后。旧代码先算 (proj+ex) 又立刻用
    # pinned[:max_pkgs] 覆盖，示例目录的包会挤掉项目自身的查询配额。
    targets = (proj + ex)[:max_pkgs]
    result = {"queried": 0, "project_queried_count": 0, "example_queried_count": 0,
              "vulnerable_packages": [], "total_vulns": 0,
              "project_pinned_count": len(proj), "example_pinned_count": len(ex),
              "unpinned_packages": len(deps) - len(pinned),
              "ecosystems": sorted({d["ecosystem"] for d in deps}),
              "method": "OSV.dev /v1/querybatch（按确切版本查询）"}
    for i in range(0, len(targets), chunk):
        batch = targets[i:i + chunk]
        payload = {"queries": [{"package": {"name": d["name"], "ecosystem": d["ecosystem"]},
                                "version": d["version"]} for d in batch]}
        try:
            req = urllib.request.Request(
                "https://api.osv.dev/v1/querybatch",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read().decode("utf-8", "ignore"))
            items = res.get("results") if isinstance(res, dict) else None
            if not isinstance(items, list) or len(items) != len(batch) or any(not isinstance(x, dict) for x in items):
                raise ValueError("OSV returned an incomplete or invalid batch")
            if any(item.get("next_page_token") for item in items):
                raise ValueError("OSV returned paginated results; coverage is incomplete")
            result["queried"] += len(batch)
            result["project_queried_count"] += sum(1 for dep in batch if dep.get("scope", "project") == "project")
            result["example_queried_count"] += sum(1 for dep in batch if dep.get("scope", "project") != "project")
            for dep, item in zip(batch, items):
                vulns = item.get("vulns") or []
                if vulns:
                    ids = [v.get("id") for v in vulns if v.get("id")]
                    result["vulnerable_packages"].append({
                        "package": dep["name"], "version": dep["version"],
                        "ecosystem": dep["ecosystem"], "source_file": dep["source_file"],
                        "scope": dep.get("scope", "project"),
                        "vuln_ids": ids[:12], "vuln_count": len(ids),
                        "details_url": "https://osv.dev/list?q=%s&ecosystem=%s" % (
                            urllib.parse.quote(dep["name"]), urllib.parse.quote(dep["ecosystem"])),
                    })
                    result["total_vulns"] += len(ids)
        except Exception as e:  # noqa: BLE001
            result["error"] = "OSV query failed: %s" % str(e)[:160]
            break
    result["vulnerable_packages"].sort(key=lambda x: (x["scope"] != "project", -x["vuln_count"]))
    # 先统计再截断：否则命中包超过 60 个时，note 里的数量会被截断后的列表少算
    proj_vulns = [v for v in result["vulnerable_packages"] if v["scope"] == "project"]
    result["project_vulnerable_count"] = len(proj_vulns)
    result["example_vulnerable_count"] = len(result["vulnerable_packages"]) - len(proj_vulns)
    result["complete"] = result["queried"] == len(pinned) and not result.get("error")
    result["vulnerable_packages"] = result["vulnerable_packages"][:60]
    if result["project_pinned_count"] == 0:
        result["status"] = "unverified_project_scope"
        result["note"] = ("项目根目录没有锁文件/精确版本声明（只有范围约束），"
                          "无法确定实际安装版本，因此不能断言项目自身依赖是否存在已知 CVE。"
                          + (" 示例/测试目录中有 %d 个锁定版本，其漏洞仅供旁证，不代表项目本体。"
                             % result["example_pinned_count"] if result["example_pinned_count"] else ""))
    elif result.get("error") or result["project_queried_count"] < len(proj):
        result["status"] = "partial_findings" if proj_vulns else "unverified_incomplete"
        result["note"] = ("仅成功查询 %d/%d 个项目自身锁定版本；查询失败或达到上限，不能给出无已知漏洞的结论。"
                          % (result["project_queried_count"], len(proj)))
    elif not proj_vulns:
        result["status"] = "clean_on_queried_scope"
        result["note"] = ("已查询的 %d 个项目自身锁定版本未命中 OSV 已知漏洞；"
                          "这不等于无依赖风险（未锁定/传递依赖/dev 依赖未覆盖）。"
                          % result["project_queried_count"])
    else:
        result["status"] = "findings"
        result["note"] = "项目自身依赖中有 %d 个包命中已知漏洞，需升级到 OSV 建议的修复版本。" % len(proj_vulns)
    return result


# ---------------------------------------------------------------------------
# GitHub 仓库健康度
# ---------------------------------------------------------------------------

def analyze_github(repo, api, metadata_only=False):
    full = "%s/%s" % (repo["owner"], repo["repo"])
    out = {"repo": full, "unavailable": []}

    m = api.get("/repos/%s" % full)
    if m["status"] != 200:
        out["meta_error"] = m.get("error")
        out["unavailable"].append("repository metadata")
        return out
    d = m["data"]
    out["meta"] = {
        "full_name": d.get("full_name"), "description": d.get("description"),
        "html_url": d.get("html_url"), "homepage": d.get("homepage"),
        "default_branch": d.get("default_branch"), "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"), "pushed_at": d.get("pushed_at"),
        "stars": d.get("stargazers_count"), "forks": d.get("forks_count"),
        "watchers": d.get("subscribers_count"), "open_issues_count": d.get("open_issues_count"),
        "size_kb": d.get("size"), "language": d.get("language"),
        "topics": d.get("topics", []), "archived": d.get("archived"),
        "disabled": d.get("disabled"), "is_fork": d.get("fork"),
        "has_issues": d.get("has_issues"), "has_wiki": d.get("has_wiki"),
        "has_discussions": d.get("has_discussions"),
        "license": (d.get("license") or {}).get("spdx_id"),
        "network_count": d.get("network_count"),
    }
    # 年龄 & 新鲜度
    try:
        pushed = datetime.strptime(d["pushed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        created = datetime.strptime(d["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        out["age_days"] = (now - created).days
        out["days_since_last_push"] = (now - pushed).days
    except Exception:
        pass

    if metadata_only:
        out["api_calls_used"] = api.calls
        out["rate_limit"] = api.rate
        out["unavailable"].append("GitHub health metrics (metadata-only stage)")
        return out

    # 语言字节分布（官方统计，比本地估算更准）
    lg = api.get("/repos/%s/languages" % full)
    if lg["status"] == 200 and isinstance(lg["data"], dict):
        total = sum(lg["data"].values()) or 1
        out["languages_api"] = [{"language": k, "pct": round(v / total * 100, 1)}
                                for k, v in sorted(lg["data"].items(), key=lambda x: -x[1])[:10]]
    else:
        out["unavailable"].append("languages")

    # Issues（含 PR，需要过滤）
    issues = api.get_all("/repos/%s/issues" % full, {"state": "open", "sort": "created",
                                                     "direction": "desc"}, max_pages=2)
    real_issues = [i for i in issues if "pull_request" not in i]
    stale_cut = datetime.now(timezone.utc) - timedelta(days=180)
    stale = 0
    for i in real_issues:
        try:
            upd = datetime.strptime(i["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if upd < stale_cut:
                stale += 1
        except Exception:
            pass
    label_counter = Counter()
    for i in real_issues:
        for lb in i.get("labels", []):
            label_counter[lb.get("name")] += 1
    out["issues"] = {
        "open_sampled": len(real_issues),
        "open_total_reported": (d.get("open_issues_count") or 0),
        "stale_180d_sampled": stale,
        "top_labels": label_counter.most_common(12),
        "oldest_open": sorted([{"number": i["number"], "title": i["title"][:90],
                                "created_at": i["created_at"], "comments": i["comments"]}
                               for i in real_issues], key=lambda x: x["created_at"])[:8],
        "most_commented": sorted([{"number": i["number"], "title": i["title"][:90],
                                   "comments": i["comments"]} for i in real_issues],
                                 key=lambda x: -x["comments"])[:5],
    }

    # PRs
    prs = api.get_all("/repos/%s/pulls" % full, {"state": "open"}, max_pages=2)
    out["pulls"] = {
        "open_sampled": len(prs),
        "list": [{"number": p["number"], "title": p["title"][:90], "created_at": p["created_at"],
                  "draft": p.get("draft"), "user": (p.get("user") or {}).get("login")}
                 for p in prs[:10]],
    }

    # 提交活跃度（最近 100 条，按作者/时间分布）
    commits = api.get_all("/repos/%s/commits" % full, {"per_page": 100}, max_pages=1)
    if commits:
        authors = Counter((c.get("commit", {}).get("author", {}) or {}).get("name", "unknown")
                          for c in commits)
        dates = []
        for c in commits:
            try:
                dates.append(datetime.strptime(
                    c["commit"]["author"]["date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
            except Exception:
                pass
        months = Counter(d.strftime("%Y-%m") for d in dates)
        out["commits"] = {
            "sampled": len(commits),
            "top_authors": authors.most_common(8),
            "unique_authors_sampled": len(authors),
            "latest": commits[0]["commit"]["author"]["date"],
            "monthly_counts": dict(sorted(months.items())),
            "last_90d_count": sum(1 for x in dates
                                  if x > datetime.now(timezone.utc) - timedelta(days=90)),
        }
    else:
        out["unavailable"].append("commits")

    # 发版
    rel = api.get_all("/repos/%s/releases" % full, {"per_page": 30}, max_pages=1)
    if rel:
        out["releases"] = {
            "count_sampled": len(rel),
            "latest": {"tag": rel[0].get("tag_name"), "name": rel[0].get("name"),
                       "published_at": rel[0].get("published_at"),
                       "prerelease": rel[0].get("prerelease")},
            "recent": [{"tag": r.get("tag_name"), "published_at": r.get("published_at")} for r in rel[:12]],
        }
        try:
            latest_ts = datetime.strptime(rel[0]["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            out["releases"]["days_since_latest"] = (datetime.now(timezone.utc) - latest_ts).days
        except Exception:
            pass
    else:
        out["releases"] = {"count_sampled": 0, "note": "无 Release，或 API 不可用"}
        # tags 兜底
        tags = api.get_all("/repos/%s/tags" % full, {"per_page": 20}, max_pages=1)
        if tags:
            out["releases"]["tags_fallback"] = [t.get("name") for t in tags[:10]]

    # 贡献者
    contrib = api.get_all("/repos/%s/contributors" % full, {"per_page": 100, "anon": "false"}, max_pages=1)
    if contrib:
        total_c = sum(c.get("contributions", 0) for c in contrib) or 1
        top = [{"login": c["login"], "contributions": c["contributions"],
                "pct": round(c["contributions"] / total_c * 100, 1)} for c in contrib[:10]]
        out["contributors"] = {
            "count_sampled": len(contrib),
            "top": top,
            "bus_factor": len([c for c in contrib if c["contributions"] / total_c > 0.05]),
            "bus_factor_note": "贡献占比 >5% 的人数（近似 bus factor，仅供参考）",
        }
    else:
        out["unavailable"].append("contributors")

    # 分支
    branches = api.get_all("/repos/%s/branches" % full, {"per_page": 100}, max_pages=1)
    out["branches"] = {"count_sampled": len(branches),
                       "names": [b["name"] for b in branches[:20]]}

    # 社区健康指标（GitHub 官方）
    ch = api.get("/repos/%s/community/profile" % full)
    if ch["status"] == 200 and isinstance(ch["data"], dict):
        out["community"] = {
            "health_percentage": ch["data"].get("health_percentage"),
            "files": ch["data"].get("files"),
        }

    out["rate_limit"] = api.rate
    out["api_calls_used"] = api.calls
    out["api_notes"] = api.notes
    return out


# ---------------------------------------------------------------------------
# 同类项目检索
# ---------------------------------------------------------------------------

# --- 同类项目检索（最终版 v4）---

# 聚合列表/教程类仓库：不是"替代品"，直接排除
AGGREGATOR_RE = re.compile(
    r"(awesome|tutorial|learning|interview|cheat[\s-]?sheet|roadmap|course|books?|guide|"
    r"collection|resources?|study|handbook|examples?|boilerplate|starter|papers?|primer|"
    r"curated|list of|100[- ]days)", re.I)

# 品类词：定义"这是什么东西"（framework / orm / crawler …）
CATEGORY_TERMS = {
    "framework", "library", "orm", "database", "db", "server", "http", "web", "template",
    "engine", "cli", "editor", "parser", "compiler", "interpreter", "proxy", "crawler",
    "scraper", "spider", "bot", "agent", "sdk", "plugin", "extension", "dashboard",
    "monitor", "monitoring", "queue", "cache", "auth", "authentication", "api", "gateway",
    "storage", "client", "toolkit", "runtime", "bundler", "linter", "formatter", "testing",
    "component", "components", "chart", "charts", "visualization", "workflow", "scheduler",
    "analytics", "search", "indexer", "middleware", "router", "logger", "logging",
    "validator", "serializer", "migration", "container", "virtualization", "ide", "notes",
    "wiki", "cms", "ecommerce", "chat", "messaging", "sync", "backup", "deploy",
}

CATEGORY_PRIORITY = [
    "orm", "crawler", "scraper", "proxy", "compiler", "interpreter", "parser", "editor",
    "ide", "dashboard", "framework", "middleware", "router", "gateway", "scheduler",
    "queue", "cache", "server", "engine", "cli", "sdk", "toolkit", "library", "client",
    "monitor", "search", "validator", "serializer", "plugin", "extension", "web", "http",
]

# 领域词：与品类词组合成 "web framework" 这类可判别的短语
DOMAIN_TERMS = {
    "web", "http", "api", "rest", "graphql", "cli", "terminal", "desktop", "mobile",
    "android", "ios", "database", "data", "ml", "llm", "ai", "game", "image", "video",
    "audio", "pdf", "email", "chat", "search", "crawl", "scrape", "test", "ci", "deploy",
    "cloud", "docker", "kubernetes", "git", "markdown", "docs", "notes", "todo",
    "calendar", "finance", "trading", "ecommerce", "blog", "wiki", "monitor", "log",
    "queue", "cache", "auth", "security", "blockchain", "embed", "iot", "robot",
    "vision", "nlp", "sql", "network", "file", "shell", "gui", "electron",
    "flutter", "react",
}


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9+#.\-]{1,}", (text or "").lower())}


def _primary_category(desc, topics, repo_name):
    pool = _tokens(desc) | {t.lower() for t in topics} | _tokens(repo_name)
    for term in CATEGORY_PRIORITY:
        if term in pool:
            return term
    return None


def _pick_domain(desc, topics, repo_name, lang):
    """挑一个领域限定词，用来和品类词组成短语查询。"""
    pool = _tokens(desc) | {t.lower() for t in topics}
    for w in (lang or "").lower().split():
        pool.discard(w)
    cands = pool & DOMAIN_TERMS
    if not cands:
        return None
    desc_tokens = _tokens(desc)
    return sorted(cands, key=lambda x: (x not in desc_tokens, -len(x)))[0]


def _rank_topics(topics, repo_name):
    """按"具体度"排序 topic：带连字符/较长的比单词泛标签更可用。"""
    scored = []
    for t in topics:
        tl = t.lower()
        if tl in GENERIC_TOPICS or tl == (repo_name or "").lower():
            continue
        s = 0
        if "-" in tl or "_" in tl:
            s += 4
        if len(tl) >= 8:
            s += 2
        if tl in CATEGORY_TERMS:
            s += 2
        scored.append((s, tl))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, t in scored]


def search_competitors(repo, api, stack, meta, limit=12, min_wanted=6):
    """检索替代品/同类项目。

    关键教训：`topic:flask` 返回的多是"用 Flask 写的应用"，不是替代品；
    只按 star 搜描述又会捞进一堆无关热仓库。
    因此主力查询是「领域词 + 品类词」的 in:readme,description 组合
    （如 "web framework"），靠搜索引擎的 AND 语义做粗筛，
    再用具体 topic 查询补齐，最后统一过"聚合列表 / 下游使用者"过滤器。
    """
    out = {"queries": [], "candidates": [], "search_errors": [], "filtered_out": 0,
           "category": None, "domain": None, "strategy": None}
    self_full = ("%s/%s" % (repo["owner"], repo["repo"])).lower()
    topics = list((meta or {}).get("topics") or [])
    desc = (meta or {}).get("description") or ""
    langs = [l["language"] for l in stack.get("languages", [])[:2]]
    # 无本地克隆时用 API 的主语言兜底，否则短语检索会失去语言约束（会捞出 Dart/Swift 框架）
    if not langs:
        api_lang = (meta or {}).get("language")
        if api_lang:
            langs = [api_lang]
    lang = langs[0] if langs else None
    repo_name = repo["repo"]

    primary = _primary_category(desc, topics, repo_name)
    domain = _pick_domain(desc, topics, repo_name, lang)
    out["category"], out["domain"] = primary, domain
    ranked = _rank_topics(topics, repo_name)
    target_topics = {t.lower() for t in topics} - GENERIC_TOPICS

    phrase_bits = [b for b in (domain, primary) if b]
    if phrase_bits and lang:
        phrase_q = "%s in:readme,description language:%s" % (" ".join(phrase_bits), lang)
    elif phrase_bits:
        phrase_q = "%s in:readme,description" % " ".join(phrase_bits)
    else:
        phrase_q = "%s in:name,description" % repo_name
    queries = [phrase_q] + ["topic:%s" % t for t in ranked[:2]]

    now = datetime.now(timezone.utc)
    seen = set()

    def guard(it, q, phrase_mode):
        """只做低成本的合理性判断，不做功能判定。返回 None 表示剔除。"""
        full = it["full_name"]
        c_desc = it.get("description") or ""
        c_topics = {t.lower() for t in (it.get("topics") or [])}
        if AGGREGATOR_RE.search(full) or AGGREGATOR_RE.search(c_desc):
            return None
        if re.search(r"(built (with|on)|powered by|made with|using|based on)\s+" +
                     re.escape(repo_name), c_desc, re.I):
            return None
        if it.get("archived") or it.get("disabled"):
            return None
        if lang and it.get("language") and it.get("language") != lang:
            return None
        if phrase_mode:
            conf = "high"
            # 短语查询只保证 README 里同时出现两个词（很多仓库只是"提到"了这些词）。
            # 必须满足其一才保留：自称同类 / 共享具体 topic / 描述里确有该品类词。
            adj_now = 0
            if domain and primary:
                if re.search(r"\b%s[\s\-]+%s\b" % (re.escape(domain), re.escape(primary)),
                             c_desc, re.I) or \
                   re.search(r"\b%s[\s\-]+%s\b" % (re.escape(primary), re.escape(domain)),
                             c_desc, re.I):
                    adj_now = 1
            cat_ok = bool(primary and (primary in _tokens(c_desc) or primary in c_topics))
            if not (adj_now or (target_topics & c_topics) or cat_ok):
                return None
        else:
            # topic 查询：必须真是同一品类，否则就是"用了同一个框架的应用"
            cat_ok = bool(primary and (primary in _tokens(c_desc) or primary in c_topics))
            if not cat_ok:
                return None
            conf = "medium"
        try:
            days = (now - datetime.strptime(it["pushed_at"], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc)).days
        except Exception:
            days = 9999
        if days > 1095:
            return None
        shared = target_topics & c_topics
        d_hit = 1 if (domain and (domain in _tokens(c_desc) or domain in c_topics)) else 0
        c_hit = 1 if (primary and (primary in _tokens(c_desc) or primary in c_topics)) else 0
        # 描述里是否直接自称"<domain> <category>"（如 "web framework"）——最可靠的同类信号
        adj = 0
        if domain and primary:
            if re.search(r"\b%s[\s\-]+%s\b" % (re.escape(domain), re.escape(primary)), c_desc, re.I) or \
               re.search(r"\b%s[\s\-]+%s\b" % (re.escape(primary), re.escape(domain)), c_desc, re.I):
                adj = 1
        return {"conf": conf, "days": days, "shared": shared, "d_hit": d_hit, "c_hit": c_hit,
                "adj": adj}

    def push(it, q, g):
        stars = it.get("stargazers_count") or 0
        activity = max(0, 365 - min(365, g["days"])) / 365
        out["candidates"].append({
            "full_name": it["full_name"],
            "url": it["html_url"],
            "description": (it.get("description") or "")[:200],
            "stars": stars,
            "forks": it.get("forks_count"),
            "language": it.get("language"),
            "topics": (it.get("topics") or [])[:8],
            "license": (it.get("license") or {}).get("spdx_id"),
            "open_issues": it.get("open_issues_count"),
            "pushed_at": it.get("pushed_at"),
            "created_at": it.get("created_at"),
            "days_since_push": g["days"],
            "relevance": {"shared_topics": sorted(g["shared"]),
                          "domain_hit": bool(g["d_hit"]), "category_hit": bool(g["c_hit"]),
                          "self_described_as_peer": bool(g["adj"]),
                          "confidence": g["conf"]},
            "matched_query": q,
            "rank_score": round(stars ** 0.5 * 8 + activity * 50 + g["adj"] * 200
                                + g["d_hit"] * 60 + g["c_hit"] * 40 + len(g["shared"]) * 40, 1),
        })

    for idx, q in enumerate(queries[:4]):
        r = api.get("/search/repositories",
                    {"q": q, "sort": "stars", "order": "desc", "per_page": 30})
        if r["status"] != 200 or not isinstance(r.get("data"), dict):
            out["search_errors"].append({"query": q, "error": r.get("error")})
            continue
        out["queries"].append(q)
        for it in r["data"].get("items", []):
            key = it["full_name"].lower()
            if key == self_full or key in seen:
                continue
            seen.add(key)
            g = guard(it, q, phrase_mode=(idx == 0))
            if not g:
                out["filtered_out"] += 1
                continue
            push(it, q, g)

    out["candidates"].sort(key=lambda x: -x["rank_score"])
    out["candidates"] = out["candidates"][:limit]
    out["strategy"] = ("主力：领域词+品类词短语查询（in:readme,description）；"
                       "补充：具体 topic 查询 + 品类校验")
    out["note"] = ("候选只是线索，不是结论。必须逐个打开 README 与依赖清单核实功能，"
                   "确认许可证后才能写进对比表；star 不代表质量。")
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_summary(data, out_path):
    lines = []
    A = lines.append

    def v(x, dash="-"):
        return dash if x is None else x

    r = data.get("repository", {}).get("meta", {}) or {}
    local = bool(data.get("input", {}).get("local"))
    A("=" * 78)
    A("GitHub 项目体检 · 数据摘要" + ("（本地目录模式）" if local else ""))
    A("=" * 78)
    A("仓库      : %s" % v(r.get("full_name"), "?"))
    A("描述      : %s" % (r.get("description") or "-"))
    A("URL       : %s" % v(r.get("html_url"), "（本地目录，无远端）"))
    A("Star/Fork : %s / %s    Issues(open): %s" % (v(r.get("stars")), v(r.get("forks")),
                                                   v(r.get("open_issues_count"))))
    A("License   : %s" % (r.get("license") or data.get("stack", {}).get("license_detected") or "未识别"))
    A("默认分支  : %s" % v(r.get("default_branch")))
    A("最近推送  : %s" % v(r.get("pushed_at")))
    A("")
    st = data.get("stack", {})
    A("--- 技术栈 ---")
    A("语言      : %s" % ", ".join("%s %.1f%%" % (l["language"], l.get("pct_code", 0))
                                   for l in st.get("languages", [])[:5]))
    A("项目类型  : %s" % ", ".join(st.get("project_types", ["未识别"])))
    A("框架      : %s" % ", ".join(f["name"] for f in st.get("frameworks", [])[:8]))
    A("构建系统  : %s" % ", ".join(b["system"] for b in st.get("build_systems", [])[:6]))
    A("单仓多项目: %s" % ("是 " + str(st.get("monorepo", {}).get("signals", []))
                          if st.get("monorepo", {}).get("detected") else "否"))
    A("依赖数量  : %s" % v(st.get("dependency_count")))
    A("")
    q = data.get("quality", {})
    A("--- 代码质量 ---")
    A("总行数(抽样) : %s" % v(q.get("loc_total")))
    A("扫描文件数   : %s" % v(q.get("files_scanned_for_quality")))
    A("长函数/复杂函数: %s / %s" % (len(q.get("long_functions", [])), len(q.get("complex_functions", []))))
    A("裸 except     : %s" % v(q.get("bare_except_count")))
    A("TODO/FIXME    : %s %s" % (v(q.get("todo_count")), q.get("todo_by_kind")))
    A("重复代码窗口  : %s%%" % v(q.get("duplication", {}).get("duplicate_window_ratio_pct")))
    A("")
    s = data.get("security", {})
    A("--- 安全 ---")
    A("疑似硬编码密钥: %s" % len(s.get("secrets", [])))
    A("代码风险命中  : %s" % len(s.get("risks", [])))
    A("Actions 问题  : %s" % sum(len(w["issues"]) for w in s.get("workflow_issues", [])))
    dep = s.get("dependency_audit") or {}
    proj_v = [x for x in dep.get("vulnerable_packages", []) if x.get("scope") == "project"]
    A("依赖漏洞      : 项目自身 %s 个包 / 示例目录 %s 个包（查询 %s 个锁定版本，状态 %s）" % (
        dep.get("project_vulnerable_count", len(proj_v)),
        dep.get("example_vulnerable_count", len(dep.get("vulnerable_packages", [])) - len(proj_v)),
        dep.get("queried", 0), dep.get("status", "-")))
    if local:
        A("")
        A("--- GitHub 健康度 ---")
        A("（本地目录模式未采集，需提供仓库 URL 才能获取 Issues/PR/Star 等指标）")
    else:
        g = data.get("repository", {})
        A("--- GitHub 健康度 ---")
        iss = g.get("issues", {})
        A("Open Issues(抽样): %s（其中 180 天未更新 %s）" % (v(iss.get("open_sampled")),
                                                          v(iss.get("stale_180d_sampled"))))
        A("Open PRs(抽样)   : %s" % v(g.get("pulls", {}).get("open_sampled")))
        A("近 90 天提交     : %s" % v(g.get("commits", {}).get("last_90d_count")))
        A("最新 Release     : %s（%s 天前）" % (
            v((g.get("releases", {}).get("latest") or {}).get("tag")),
            v(g.get("releases", {}).get("days_since_latest"))))
        A("贡献者(抽样)     : %s   bus factor≈%s" % (
            v((g.get("contributors") or {}).get("count_sampled")),
            v((g.get("contributors") or {}).get("bus_factor"))))
        A("社区健康分       : %s" % v((g.get("community", {}) or {}).get("health_percentage")))
    A("")
    c = data.get("competitors", {})
    A("--- 同类项目候选（%s 个）---" % len(c.get("candidates", [])))
    for x in c.get("candidates", [])[:12]:
        A("  %-40s ★%-6s %-12s %s" % (x["full_name"], x.get("stars"), x.get("language") or "-",
                                      (x.get("license") or "-")))
    A("")
    A("--- 访问限制 / 未验证事项 ---")
    for note in data.get("access", {}).get("limitations", []):
        A("  · %s" % note)
    A("")
    A("完整数据: %s" % out_path)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="GitHub 项目全方位体检 · 数据采集器")
    ap.add_argument("--version", action="version", version=VERSION)
    ap.add_argument("target", nargs="?", help="GitHub 仓库 URL 或 owner/repo")
    ap.add_argument("--out", default="inspector-out", help="输出目录（默认 ./inspector-out）")
    ap.add_argument("--local", default=None,
                    help="改为审计本地目录（离线，不调用 GitHub API，仅做代码层分析）")
    ap.add_argument("--stage", default="all",
                    choices=["all", "meta", "clone", "analyze", "github", "competitors", "repo", "code"],
                    help="分阶段执行")
    ap.add_argument("--token", default=None, help="GitHub Token（默认读 GITHUB_TOKEN/GH_TOKEN）")
    ap.add_argument("--workdir", default=None, help="克隆目录（默认 <out>/repo）")
    ap.add_argument("--keep-clone", action="store_true", help="兼容旧命令；源码始终保留")
    ap.add_argument("--no-clone", action="store_true", help="只走 API，不下载源码")
    ap.add_argument("--max-files", type=int, default=8000)
    ap.add_argument("--budget", type=int, default=80, help="API 调用预算（匿名限流 60/h）")
    ap.add_argument("--skip-osv", action="store_true", help="跳过依赖漏洞查询")
    ap.add_argument("--allow-network", action="store_true", help="允许本地模式联网查询 OSV（默认离线）")
    ap.add_argument("--json-only", action="store_true", help="只打印 JSON 路径")
    args = ap.parse_args()
    args.stage = {"repo": "meta", "code": "analyze"}.get(args.stage, args.stage)
    if args.max_files < 1 or args.budget < 1:
        ap.error("--max-files 和 --budget 必须为正整数")

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    clone_dir = Path(args.workdir).resolve() if args.workdir else out_dir / "repo"

    t_start = time.time()
    data = {
        "inspector": {"version": VERSION, "generated_at": now_iso(),
                      "host": "workbuddy", "python": sys.version.split()[0]},
        "input": {"raw": args.target},
        "access": {"limitations": [], "api_calls": 0, "source_method": None},
    }

    # 0) 本地目录模式：离线只做代码层分析
    if args.local:
        src_root = Path(args.local).resolve()
        if not src_root.is_dir():
            safe_print(json.dumps({"ok": False, "error": "本地目录不存在: %s" % src_root}, ensure_ascii=False))
            return 2
        if out_dir == src_root:
            safe_print(json.dumps({"ok": False, "error": "输出目录不能与待审计源码目录相同"}, ensure_ascii=False))
            return 2
        data["input"] = {"raw": str(src_root), "local": True, "repo": src_root.name}
        data["repository"] = {"local": True, "meta": {"full_name": src_root.name,
                                                      "default_branch": None},
                              "unavailable": ["all GitHub API metrics (local mode)"]}
        data["access"]["source_method"] = "local"
        files, fstats = walk_files(src_root, max_files=args.max_files, exclude_paths=(out_dir,))
        data["access"]["files"] = fstats
        stack = detect_stack(src_root, files)
        structure = analyze_structure(src_root, files, stack)
        quality = analyze_quality(src_root, files, stack)
        security = analyze_security(src_root, files, stack)
        security["dependency_audit"] = ({"status": "skipped", "note": "--skip-osv 或本地离线模式；--allow-network 可联网查询"}
                                        if args.skip_osv or not args.allow_network else osv_query(stack["dependencies"]))
        data.update({"stack": stack, "structure": structure,
                     "quality": quality, "security": security, "competitors": {"candidates": []}})
        out_json = out_dir / "inspector-data.json"
        out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = build_summary(data, str(out_json))
        (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
        safe_print(str(out_json) if args.json_only else summary)
        return 0

    if not args.target:
        safe_print(json.dumps({"ok": False, "error": "需要 GitHub URL / owner/repo，或使用 --local <目录>"},
                         ensure_ascii=False))
        return 2

    # 1) 解析 URL
    try:
        parsed = parse_repo_url(args.target)
    except ValueError as e:
        safe_print(json.dumps({"ok": False, "error": "URL 解析失败: %s" % e}, ensure_ascii=False))
        return 2
    data["input"].update(parsed)
    log("目标仓库: %s/%s (ref=%s)" % (parsed["owner"], parsed["repo"], parsed["ref"]))

    api = GitHubAPI(token=args.token, budget=args.budget)

    # 2) 元数据
    gh = analyze_github(parsed, api, metadata_only=args.stage not in ("all", "github"))
    data["repository"] = gh
    if gh.get("meta_error"):
        data["access"]["limitations"].append(
            "无法读取仓库元数据：%s。若为私有仓库，需要用户提供授权（Token）。" % gh["meta_error"])
        out_json = out_dir / "inspector-data.json"
        out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_print(json.dumps({"ok": False, "stage": "meta", "error": gh["meta_error"],
                          "data": str(out_json)}, ensure_ascii=False))
        return 3

    meta = gh.get("meta", {})
    data["access"]["source_method"] = None

    # --stage 的真正语义：只跑请求的那一段，其余跳过。
    # 旧实现只判断了 competitors，`--stage analyze` 依旧跑完整流程（含克隆与竞品检索）。
    if args.stage == "meta":
        data["access"]["api_calls"] = api.calls
        data["access"]["rate_limit"] = api.rate
        data["access"]["limitations"].extend(api.notes)
        out_json = out_dir / "inspector-data.json"
        out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = build_summary(data, str(out_json))
        (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
        safe_print(str(out_json) if args.json_only else summary)
        return 0

    want_clone = args.stage in ("all", "clone", "analyze")
    want_analyze = args.stage in ("all", "analyze")
    want_competitors = args.stage in ("all", "competitors")

    stack, structure, quality, security = {}, {}, {}, {}
    src_root = None

    # 3) 源码
    if want_clone and not args.no_clone:
        if clone_dir.exists() or clone_dir.is_symlink():
            import uuid
            clone_dir = out_dir / ("repo-" + uuid.uuid4().hex[:12])
            data["access"]["limitations"].append(
                "原克隆目录已存在并保留，已改用新目录 %s。" % clone_dir.name)
        try:
            ref = resolve_repo_ref(parsed, api) or meta.get("default_branch")
            info = fetch_source(parsed, clone_dir, ref=ref)
        except ValueError as error:
            info = {"ok": False, "error": str(error), "method": None, "path": str(clone_dir)}
        data["access"]["clone"] = info
        data["access"]["source_method"] = info.get("method")
        if info["ok"]:
            src_root = clone_dir
        else:
            data["access"]["limitations"].append(
                "源码获取失败（git clone 与 zipball 均不可用）：%s。以下代码层分析缺失，"
                "仅有 API 层数据。" % (info.get("error") or "")[:200])
    elif not want_clone:
        data["access"]["limitations"].append(
            "本次仅执行 stage=%s，未克隆源码，无代码层证据。" % args.stage)
    else:
        data["access"]["limitations"].append("已指定 --no-clone，仅采集 API 数据，无代码层证据。")

    # 4) 代码层分析
    if src_root and want_analyze:
        files, fstats = walk_files(src_root, max_files=args.max_files)
        data["access"]["files"] = fstats
        if fstats["truncated"]:
            data["access"]["limitations"].append(
                "仓库文件数超过上限（%d），已截断扫描，超大仓库建议按模块分批审计。" % args.max_files)
        stack = detect_stack(src_root, files)
        structure = analyze_structure(src_root, files, stack)
        quality = analyze_quality(src_root, files, stack)
        security = analyze_security(src_root, files, stack)
        if not args.skip_osv:
            security["dependency_audit"] = osv_query(stack["dependencies"])
        else:
            security["dependency_audit"] = {"status": "skipped", "note": "--skip-osv 已启用"}
        data.update({"stack": stack, "structure": structure,
                     "quality": quality, "security": security})
    else:
        if src_root and not want_analyze:
            data["access"]["limitations"].append(
                "本次仅执行 stage=%s，已克隆源码但未做代码层分析。" % args.stage)
        data.update({"stack": {}, "structure": {}, "quality": {}, "security": {}})

    # 5) 同类项目
    if want_competitors:
        data["competitors"] = search_competitors(parsed, api, stack or {"languages": []}, meta)

    # 6) 汇总
    data["access"]["api_calls"] = api.calls
    data["access"]["rate_limit"] = api.rate
    data["access"]["limitations"].extend(api.notes)
    if api.rate.get("remaining") is not None and api.rate["remaining"] < 10 and not api.token:
        data["access"]["limitations"].append(
            "GitHub API 剩余配额仅 %s 次；如需完整数据请设置 GITHUB_TOKEN。" % api.rate["remaining"])
    if not api.token:
        data["access"]["limitations"].append(
            "当前为匿名访问（60 次/小时）。部分列表接口只抽样前 100~200 条，"
            "报告中的 issue/PR 计数为抽样值，需与 open_issues_count 区分。")
    data["elapsed_s"] = round(time.time() - t_start, 1)

    out_json = out_dir / "inspector-data.json"
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = build_summary(data, str(out_json))
    (out_dir / "summary.txt").write_text(summary, encoding="utf-8")

    if args.json_only:
        safe_print(str(out_json))
    else:
        safe_print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
