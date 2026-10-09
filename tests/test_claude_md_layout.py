# -*- coding: utf-8 -*-
"""守卫：项目 CLAUDE.md 的「写入约定」与 `.claude/rules/` 分层（设计 2026-09-29）。

判据由几条运行时 / 静态观察前提决定（Claude Code 2.1.283；运行时 PoC 见 spec E3–E5，
静态观察来自 full-review 4A 对 claude.exe 字符串的检索）：
1. `.claude/rules/*.md` 带 `paths` 时，只在 Read 工具读到匹配文件才加载；不带 `paths`
   则每次会话无条件常驻——等于绕过写入约定。frontmatter 的 YAML 解析失败时同样按
   「无 paths」处理（静默常驻），所以本守卫只认一种带引号的固定写法。
2. `paths` 按 gitignore 语义匹配（`ignore` 库）：模式命中目录即覆盖其下全部文件；
   末尾 `/**` 会先被剥掉——单段的 `hooks/**` 因此变成不锚定的 `hooks`，会命中任意
   深度的同名目录。`hooks/**/*` 与 `/VERSION` 是锚定的。
3. 加载以磁盘为准、只收 `.md`：未跟踪的 rules 文件照样会被加载，所以只比对 git 跟踪
   清单不够。
另外，CLAUDE.md 是 vault-loader 信号 I 的输入，不得出现能生效的注释；
rules 随公开发布分发，不得含私人标识。

判据都写成接收 `(root, tracked)` 的纯函数：对仓库真实状态断言一次，再在 `tmp_path`
小树上逐个制造违规断言它们能报出来（判别力证据入库，不依赖一次性的变异脚本）。
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
RULES_REL = ".claude/rules"

# 体积：UTF-16 单元（与 Claude Code 启动警告同单位），按 read_text()（换行统一为 LF）计；
# Windows CRLF 检出时实际文件每行多 1 个单元，口径差约 1.4%。
# CLAUDE.md 上限：2026-09-29 迁移后实测 10,699，取约 1.2 倍。超限时按「写入约定」合并同组
# 索引行或把正文挪进 rules；调整上限本身需用户确认。
CLAUDE_MD_BUDGET = 13000
# rules 体积（用户 2026-09-29 确认）：单份正文（去 frontmatter）与单次 Read 带回的正文合计。
# 「全部 rules + CLAUDE.md 不比迁移前的常驻 CLAUDE.md 贵」只报告、不硬拦。
MAX_RULE_BODY = 11000
MAX_ONDEMAND_PER_FILE = 18000
PRE_SPLIT_RESIDENT = 45430  # 迁移前常驻 CLAUDE.md（基线 3ac359f，LF 口径）
MAX_RULES_PER_FILE = 2

RULE_NAME_RE = re.compile(r"^\.claude/rules/[^/]+\.md$")
RULE_REF_RE = re.compile(r"\.claude/rules/[^\s`)<>*]+\.md")
MIGRATED_SECTION_RE = re.compile(r"^## 迁移原文[^\n]*\n.*?(?=^## |\Z)", re.M | re.S)

# 代码注释按名引用、必须仍能在 CLAUDE.md 找到的节名与短语（多行或无引号形态，扫描抓不到的）
CLAUDE_REFS = [
    pytest.param("skills/vault-loader/tests/integration/test_perf.py", "开发与测试", id="perf-dev"),
    pytest.param("tests/test_cache_contract.py", "开发与测试", id="cache-dev"),
    pytest.param("tests/test_cache_contract.py", "核心数据契约", id="cache-contract"),
    pytest.param("tests/test_cache_contract.py", "改一端的 cache schema 必须同步另一端", id="cache-sync"),
    pytest.param("skills/vault-loader/tests/test_fail_open.py", "所有 hook fail-open", id="failopen-vl"),
    pytest.param("tests/test_hook_command_claude.py", "所有 hook fail-open", id="failopen-claude-cmd"),
    pytest.param("tests/test_no_corpus_in_repo.py", "分发边界（重要）", id="corpus-dist"),
    pytest.param("tests/test_push_guard_installed.py", "分发边界", id="pushguard-dist"),
    pytest.param("tests/test_plugin_version_sync.py", "发布流程", id="version-release"),
    pytest.param("skills/vault-loader/tests/test_pin_report.py", "坏值清洗只有一处", id="pin-numeric"),
    pytest.param("skills/vault-loader/scripts/_vault_init.py", "vault-loader 对 Vault 只读", id="vault-readonly"),
]
# 引用的正文已迁入 rules：在 CLAUDE.md ∪ rules 中找到即可
UNION_REFS = [
    pytest.param("skills/vault-loader/scripts/_topic.py", "切一层 session 会让", id="topic-session"),
    pytest.param("skills/vault-loader/tests/test_session_topic.py", "切一层 session 会让", id="st-session"),
    pytest.param("skills/vault-loader/tests/test_topic_repo_scope.py", "锁绝对下界", id="scope-floor"),
]
# 同行、带书名号的「CLAUDE.md「短语」」形态由全集扫描覆盖；已知悬空且暂不处理的放这里
REF_RE = re.compile(r"CLAUDE\.md[^「“\n]{0,12}[「“]([^」”\n]{1,60})[」”]")
REF_EXEMPT = {
    ("skills/vault-loader/tests/test_readonly_sandbox.py", "源码级断言会被注释满足"):
        "基线就悬空：该短语出自全局 ~/.claude 的文档，不在本仓库 CLAUDE.md",
}

# 私人标识：ASCII 边界而非 \b——Python 的 \b 把中文算作单词字符，紧贴中文的 token 会漏检
_NB = r"(?<![0-9A-Za-z])"
_NA = r"(?![0-9A-Za-z])"
LEAK_PATTERNS = {
    "盘符绝对路径": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
    "POSIX 家目录": re.compile(r"(?<![A-Za-z0-9_~.])/(?:home|Users|root)/[^/\s`]+"),
    "UNC 路径": re.compile(r"\\\\[A-Za-z0-9._-]+\\"),
    "Claude 项目目录编码": re.compile(r"projects[\\/][A-Za-z]--"),
    "git ssh 远端": re.compile(r"git@[^\s:]+:|ssh://"),
    "http(s) URL": re.compile(
        r"https?://(?!(?:[\w-]+\.)*example\.(?:com|org|net)(?![\w.-])|localhost(?![\w.-])"
        r"|127\.0\.0\.1(?![\d.]))[^\s`)>\]]+"),
    "邮箱": re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    "UUID": re.compile(_NB + r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}" + _NA),
    "IPv4": re.compile(r"(?<![0-9A-Za-z.])(?!127\.0\.0\.1(?![\d.]))"
                       r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![0-9A-Za-z.])"),
    "会话 ID 前缀": re.compile(r"(?:会话|session)\s*`?[0-9a-f]{8}" + _NA, re.I),
}
# 每条 pattern 一个阳性样本（字面量拆开拼接，避免本文件自己触发脱敏闸门）；含紧贴中文的形态
LEAK_SAMPLES = {
    "盘符绝对路径": "见" + "D" + ":" + "\\Work\\x",
    "POSIX 家目录": "见" + "/ho" + "me/alice/repo",
    "UNC 路径": "见" + "\\\\" + "fileserver\\share",
    "Claude 项目目录编码": "~/.claude/pro" + "jects/D--Work-demo/memory",
    "git ssh 远端": "git" + "@git.example.org:team/repo.git",
    "http(s) URL": "见" + "https" + "://git.corp.internal/team/repo",
    "邮箱": "联系" + "alice" + "@" + "corp.internal",
    "UUID": "会话" + "-".join(["0" * 8, "0" * 4, "0" * 4, "0" * 4, "0" * 12]) + "已归档",
    "IPv4": "内网" + "192." + "168.10.2" + "可达",
    "会话 ID 前缀": "实证见会话" + "1a2b" + "3c4d" + "已归档",
}
LEAK_NEGATIVES = ["版本 1.4.1 发布", "见 https://docs.example.com/a", "`@` 导入", "127.0.0.1:8080",
                  "`\\\\?\\UNC\\`", "`~/.claude/projects/<hash>/`", "dis" + chr(0x200B) + "able"]
# 泄漏扫描的放行项：(文件, 命中原文, 所在行必须包含的锚串)。三者同时成立才放行——
# 只按命中原文放行太粗（例如 `D:/` 会放过 CHANGELOG 将来出现的任何盘符路径），
# 按整个文件豁免更粗。当前三项都在 CHANGELOG：opt_out 示例路径与两个格式规范 URL。
LEAK_ALLOW = frozenset({
    ("CHANGELOG.md", "D:/", '"D:/work/secret"'),
    ("CHANGELOG.md", "https://keepachangelog.com/zh-CN/1.1.0/", "[Keep a Changelog]"),
    ("CHANGELOG.md", "https://semver.org/lang/zh-CN/", "[语义化版本]"),
})

# 测试读数（写入约定 ①）：会随下一次提交过期的套件计数
COUNT_RE = re.compile(r"(?<![0-9A-Za-z])\d+\s*(?:passed|failed|skipped|errors?)(?![A-Za-z])"
                      r"|(?<![0-9A-Za-z])\d+p/\d+s(?![A-Za-z])", re.I)

RULES_ADMISSION_OK = [".claude/rules/a.md"]
RULES_ADMISSION_BAD = [".claude/rules/sub/a.md", ".claude/rules/a.md.bak", ".claude/settings.json",
                       ".claude/worktrees/w/x.md", "skills/x/.claude/rules/a.md",
                       ".claude/rules/x.md/y.md"]


# ---------------------------------------------------------------- helpers

def _clean_git_env() -> dict[str, str]:
    # 剥掉继承来的 GIT_*（Windows 上 os.environ 键名已大写，.upper() 只为跨平台一致）
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


def _git(root: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, cwd=root, env=_clean_git_env(),
                          input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)


def _tracked(root: Path = ROOT) -> list[str]:
    """本仓库自己的 git 跟踪清单。

    - git 不可用 / 不是 git 检出（插件缓存）→ skip；
    - 是 git 检出（有 `.git`）却调用失败 → fail：门禁不能把「git 坏了」静默算成通过；
    - 嵌在别的 git 仓库里（例如插件缓存位于 `~/.claude` 仓库内）→ skip，不拿外层清单比对。
    """
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        err = top.stderr.decode("utf-8", "replace").strip()
        if (root / ".git").exists():
            pytest.fail(f"这是 git 检出但 git rev-parse 失败，门禁不能静默跳过：{err}")
        pytest.skip(f"非 git 仓库（例如插件缓存里的副本）：{err}")
    if Path(top.stdout.decode("utf-8", "replace").strip()).resolve() != root.resolve():
        pytest.skip("不是本仓库自己的 checkout（嵌在别的 git 仓库里），不拿外层清单比对")
    proc = _git(root, "ls-files", "-z")
    if proc.returncode != 0:
        pytest.fail(f"git ls-files 失败：{proc.stderr.decode('utf-8', 'replace').strip()}")
    files = [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    if not files:
        pytest.skip("跟踪清单为空（非有效 checkout）")
    return files


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    return _tracked()


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def convention_section(text: str) -> str:
    m = re.search(r"^## 写入约定[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        raise AssertionError("CLAUDE.md 缺少「## 写入约定」节")
    return m.group(1)


def mapped_rule_files(text: str) -> set[str]:
    """写入约定节内表格行里出现的 `.claude/rules/<x>.md`。"""
    found: set[str] = set()
    for line in convention_section(text).splitlines():
        if line.startswith("|"):
            found.update(m.group(1) for m in re.finditer(r"`(\.claude/rules/[^`]+)`", line))
    return found


def disk_rule_files(root: Path) -> set[str]:
    """`.claude/rules/` 下全部文件（任意深度、任意扩展名）：守卫的卫生面。"""
    d = root / RULES_REL
    if not d.is_dir():
        return set()
    return {p.relative_to(root).as_posix() for p in d.rglob("*") if p.is_file()}


def rule_md_files(root: Path) -> list[str]:
    """Claude Code 实际会加载的 rules（只收 `.md`）：读取正文的判据只遍历这些。"""
    return sorted(p for p in disk_rule_files(root) if p.endswith(".md"))


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_PATH_ITEM_RE = re.compile(r'  - "([^"\n]+)"')


def parse_paths(text: str) -> list[str] | None:
    """只认一种 frontmatter：首行 `paths:`，其后每行 `  - "<glob>"`。其他写法一律返回 None。"""
    m = _FM_RE.match(text)
    if not m:
        return None
    lines = m.group(1).split("\n")
    if lines[0] != "paths:" or len(lines) < 2:
        return None
    out = []
    for ln in lines[1:]:
        mm = _PATH_ITEM_RE.fullmatch(ln)
        if not mm:
            return None
        out.append(mm.group(1))
    return out


def rule_body(text: str) -> str:
    m = _FM_RE.match(text)
    return text[m.end():] if m else text


_ROOT_FILE_RE = re.compile(r"^/[^/*?\[\]{}!\\]+$")
_ANCHORED_RE = re.compile(r"^[^/*?\[\]{}!\\#][^?\[\]{}!\\]*/[^?\[\]{}!\\]*$")


def glob_problem(g: str) -> str | None:
    """按 Claude Code 的实际语义判 glob 是否合规，合规返回 None。"""
    if g.endswith("/**"):
        if "/" not in g[:-3].strip("/"):
            return "以 /** 结尾且前缀只有一段：Claude Code 剥掉 /** 后变成不锚定的同名目录，匹配任意深度"
        return "以 /** 结尾：剥掉后虽仍锚定，但本仓库目录统一写 <dir>/**/*（守卫的匹配器只建模这一种）"
    if g.strip("/") in ("", "**"):
        return "等价于无条件加载"
    if g.endswith("/"):
        return "以 / 结尾：gitignore 语义下不锚定"
    if _ROOT_FILE_RE.fullmatch(g):
        return None
    if g.startswith("/"):
        return "前导 / 只用于根级单文件"
    if not _ANCHORED_RE.fullmatch(g):
        return "不锚定，或含本仓库不采用的语法（{} ! ? [ ]；Claude Code 支持花括号展开，但守卫的匹配器不建模它）"
    return None


def glob_regex(g: str) -> re.Pattern[str]:
    """合规 glob → 对仓库相对路径的全匹配正则（gitignore 锚定语义的子集）。

    gitignore 语义：模式命中某个目录时，其下全部文件都算命中，故尾部追加 `(?:/.*)?`。
    """
    s = g[1:] if g.startswith("/") else g
    out, i = [], 0
    while i < len(s):
        if s.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif s[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(s[i]))
            i += 1
    return re.compile("".join(out) + "(?:/.*)?")


def outside_migrated(text: str) -> str:
    """去掉「## 迁移原文」小节：历史原文按约定豁免测试读数等条款，新写入内容不豁免。"""
    return MIGRATED_SECTION_RE.sub("", text)


def _load_signal_collect(root: Path = ROOT) -> ModuleType:
    path = root / "skills" / "vault-loader" / "scripts" / "_signal_collect.py"
    name = "_cv_layout_signal_collect"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    # dataclass 在 `from __future__ import annotations` 下要从 sys.modules 查所属模块
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 判据（纯函数，返回问题清单）

def mapping_problems(root: Path, tracked: list[str]) -> list[str]:
    mapped = mapped_rule_files(_read(root, "CLAUDE.md"))
    tracked_rules = {p for p in tracked if p.startswith(RULES_REL + "/")}
    disk = disk_rule_files(root)
    probs = [f"{p}: 只允许 .claude/rules/ 下一层 .md" for p in sorted(mapped | tracked_rules | disk)
             if not RULE_NAME_RE.fullmatch(p)]
    if not mapped:
        probs.append("写入约定的映射表为空")
    sets = {"映射表": mapped, "git 跟踪": tracked_rules, "磁盘": disk}
    for p in sorted(mapped | tracked_rules | disk):
        missing = [k for k, s in sets.items() if p not in s]
        if missing:
            probs.append(f"{p}: 缺于 {missing}（未跟踪的 rules 也会被 Claude Code 加载）")
    return probs


def frontmatter_problems(root: Path) -> list[str]:
    probs = []
    for rel in rule_md_files(root):
        paths = parse_paths(_read(root, rel))
        if not paths:
            probs.append(f"{rel}: frontmatter 缺失或不是固定写法（不带 paths 或 YAML 写错都会每次会话无条件常驻）")
            continue
        probs += [f"{rel}: {g!r} {why}" for g in paths if (why := glob_problem(g))]
    return probs


def _rule_globs(root: Path) -> dict[str, list[re.Pattern[str]]]:
    return {rel: [glob_regex(g) for g in (parse_paths(_read(root, rel)) or []) if glob_problem(g) is None]
            for rel in rule_md_files(root)}


def glob_match_problems(root: Path, tracked: list[str]) -> list[str]:
    probs = []
    for rel in rule_md_files(root):
        for g in parse_paths(_read(root, rel)) or []:
            if glob_problem(g) is None:
                rx = glob_regex(g)
                if not any(rx.fullmatch(p) for p in tracked):
                    probs.append(f"{rel}: {g!r} 一个被跟踪文件都不匹配（改名后失效？）")
    return probs


def coverage_problems(root: Path, tracked: list[str], limit: int = MAX_RULES_PER_FILE) -> list[str]:
    globs = _rule_globs(root)
    probs = []
    for p in tracked:
        hit = [rel for rel, rs in globs.items() if any(r.fullmatch(p) for r in rs)]
        if len(hit) > limit:
            probs.append(f"{p}: 被 {len(hit)} 份 rules 覆盖 {hit}（上限 {limit}）")
    return probs


def volume_problems(root: Path, tracked: list[str], body_cap: int = MAX_RULE_BODY,
                    per_file_cap: int = MAX_ONDEMAND_PER_FILE) -> list[str]:
    sizes = {rel: _u16(rule_body(_read(root, rel))) for rel in rule_md_files(root)}
    probs = [f"{rel}: 正文 {n} 单元，超过单份上限 {body_cap}" for rel, n in sizes.items() if n > body_cap]
    globs = _rule_globs(root)
    for p in tracked:
        n = sum(sizes[rel] for rel, rs in globs.items() if any(r.fullmatch(p) for r in rs))
        if n > per_file_cap:
            probs.append(f"{p}: 读一次带回 rules 正文 {n} 单元，超过上限 {per_file_cap}（按体积切细 rules 或收窄 paths）")
    return probs


def pointer_problems(root: Path) -> list[str]:
    known = disk_rule_files(root)  # 字符串集合：大小写精确，不受 Windows 文件系统影响
    refs = set(RULE_REF_RE.findall(_read(root, "CLAUDE.md")))
    return [f"CLAUDE.md 指向不存在（或大小写不符）的 rules 文件：{r}" for r in sorted(refs - known)]


def index_pointer_problems(root: Path) -> list[str]:
    text = _read(root, "CLAUDE.md")
    if "## 硬约束索引" not in text:
        return ["CLAUDE.md 缺少「## 硬约束索引」节"]
    body = text.split("## 硬约束索引", 1)[1]  # 映射表在此之前，不计入
    return [f"{rel}: 索引区没有任何指向它的行（rules 只在 Read 时加载，每份都要有常驻入口）"
            for rel in rule_md_files(root) if rel not in body]


def leak_universe(tracked: list[str]) -> list[str]:
    """泄漏扫描论域：全部受跟踪的 Markdown（它们随公开发布分发）。"""
    return sorted(p for p in tracked if p.endswith(".md"))


def _leak_allowed(rel: str, text: str, line: str) -> bool:
    return any(rel == a_rel and text == a_text and anchor in line
               for a_rel, a_text, anchor in LEAK_ALLOW)


def leak_problems(root: Path, rels: list[str]) -> list[str]:
    probs = []
    for rel in rels:
        content = _read(root, rel)
        for name, pat in LEAK_PATTERNS.items():
            for m in pat.finditer(content):
                start = content.rfind("\n", 0, m.start()) + 1
                end = content.find("\n", m.end())
                line = content[start:] if end == -1 else content[start:end]
                if not _leak_allowed(rel, m.group(0), line):
                    probs.append(f"{rel}: {name} {m.group(0)!r}")
    return probs


def count_problems(root: Path) -> list[str]:
    probs = []
    for rel in ["CLAUDE.md"] + rule_md_files(root):
        probs += [f"{rel}: {m.group(0)!r}" for m in COUNT_RE.finditer(outside_migrated(_read(root, rel)))]
    return probs


def _mini(tmp_path: Path) -> Path:
    """复制指令文件与 rules 到 tmp_path，供判别用例在其上制造违规。"""
    for rel in ["CLAUDE.md", *sorted(disk_rule_files(ROOT))]:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    return tmp_path


def _sub(p: Path, old: str, new: str) -> None:
    t = p.read_text(encoding="utf-8")
    assert old in t, f"判别用例的锚点不存在：{p.name} {old!r}"
    p.write_text(t.replace(old, new, 1), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- 判据自身的区分力（不依赖仓库状态）

@pytest.mark.parametrize("g,ok", [
    ("hooks/**/*", True), ("/VERSION", True), ("skills/*/tests/**/*", True),
    ("skills/vault-loader/scripts/_state.py", True), (".claude-plugin/**/*", True),
    ("hooks/**", False), ("skills/vault-loader/**", False), ("scripts", False), ("VERSION", False),
    ("**", False), ("/**", False), ("**/x.py", False), ("/hooks/**/*", False), ("hooks/", False),
    ("a/{b,c}.py", False), ("!a/b", False), ("a/b?.py", False), ("#a/b", False),
])
def test_glob_problem_classifies_known_forms(g, ok):
    assert (glob_problem(g) is None) is ok, (g, glob_problem(g))


@pytest.mark.parametrize("g,needle", [
    pytest.param("hooks/**", "任意深度", id="single-seg-dstar"),
    pytest.param("skills/vault-loader/**", "仍锚定", id="multi-seg-dstar"),
    pytest.param("a/{b,c}.py", "不采用", id="brace"),
])
def test_glob_problem_reason_names_real_mechanism(g, needle):
    assert needle in (glob_problem(g) or ""), glob_problem(g)


def test_glob_regex_semantics():
    r = glob_regex("scripts/**/*")
    assert r.fullmatch("scripts/migrate_context_vault.py") and r.fullmatch("scripts/a/b.py")
    assert not r.fullmatch("skills/vault-loader/scripts/_state.py")
    v = glob_regex("/VERSION")
    assert v.fullmatch("VERSION") and not v.fullmatch("skills/x/VERSION")
    t = glob_regex("skills/*/tests/**/*")
    assert t.fullmatch("skills/vault-loader/tests/integration/test_perf.py")
    assert not t.fullmatch("skills/a/b/tests/x.py")                      # * 不跨 /
    assert glob_regex("hooks/**/*").fullmatch("hooks/a/b/c.sh")          # ** 跨多层
    assert glob_regex("a/**/*").fullmatch("a/x")                         # ** 可为零层


def test_glob_regex_models_directory_match():
    # gitignore 语义：模式命中目录，其下全部文件都算命中（Claude Code 用 ignore 库匹配）
    r = glob_regex("skills/vault-loader/tests/*")
    assert r.fullmatch("skills/vault-loader/tests/integration/test_perf.py")
    assert not r.fullmatch("x/skills/vault-loader/tests/a.py")


@pytest.mark.skipif(sys.version_info < (3, 13), reason="PurePosixPath.full_match 需要 3.13+")
def test_glob_regex_agrees_with_stdlib_oracle_on_files(tracked):
    globs = sorted({g for rel in rule_md_files(ROOT) for g in (parse_paths(_read(ROOT, rel)) or [])
                    if glob_problem(g) is None})
    probes = tracked + ["hooks/a/b.py", "skills/v/w/tests/a.py", "x/hooks/a.py"]
    # 预言机只建模「文件全匹配」，不含目录覆盖语义；比较时去掉尾部的目录覆盖分支
    diff = [(g, p) for g in globs for p in probes
            if bool(re.compile(glob_regex(g).pattern[:-len("(?:/.*)?")]).fullmatch(p))
            != PurePosixPath(p).full_match(g.lstrip("/"))]
    assert not diff, diff[:10]


def test_parse_paths_accepts_only_fixed_form():
    assert parse_paths('---\npaths:\n  - "a/**/*"\n  - "/V"\n---\nbody') == ["a/**/*", "/V"]
    assert parse_paths("---\npaths: a/**/*\n---\n") is None
    assert parse_paths("---\npaths:\n  - a/**/*\n---\n") is None
    assert parse_paths("---\npaths:\n---\n") is None
    assert parse_paths("no frontmatter") is None


def test_convention_section_tolerates_being_last():
    assert convention_section("# x\n## 写入约定\nbody\n") == "body\n"


def test_leak_samples_cover_every_pattern():
    assert set(LEAK_SAMPLES) == set(LEAK_PATTERNS)


@pytest.mark.parametrize("name", sorted(LEAK_SAMPLES))
def test_each_leak_pattern_fires(name):
    assert LEAK_PATTERNS[name].search(LEAK_SAMPLES[name]), (name, LEAK_SAMPLES[name])


@pytest.mark.parametrize("text", LEAK_NEGATIVES)
def test_leak_patterns_spare_known_benign_text(text):
    assert not [n for n, p in LEAK_PATTERNS.items() if p.search(text)], text


def test_leak_universe_covers_every_tracked_markdown(tracked):
    """泄漏扫描论域 = 全部受跟踪 `*.md`。

    2.0.0 之前只扫两份根级指令文件 + rules，README / MIGRATION / CHANGELOG /
    SKILL.md / references 这批随公开发布分发的用户文档不在其内——而它们正是
    移除 Codex 时要大改的文件。
    """
    universe = set(leak_universe(tracked))
    assert universe == {p for p in tracked if p.endswith(".md")}
    assert {"README.md", "CHANGELOG.md", "docs/MIGRATION.md"} <= universe


def test_leak_problems_scans_non_instruction_docs(tmp_path):
    root = _mini(tmp_path)
    (root / "README.md").write_text("说明\n" + LEAK_SAMPLES["邮箱"] + "\n", encoding="utf-8")
    rels = ["CLAUDE.md", "README.md", *rule_md_files(root)]
    assert any(p.startswith("README.md: 邮箱") for p in leak_problems(root, rels))


def test_leak_allow_entries_are_live():
    """放行项防腐：每一项都必须仍恰好命中一次，否则就是死条目或已失效的豁免。"""
    for rel, text, anchor in LEAK_ALLOW:
        content = _read(ROOT, rel)
        lines = [ln for ln in content.splitlines() if anchor in ln and text in ln]
        assert len(lines) == 1, f"放行项不再唯一命中，应更新或删除：{(rel, text, anchor)}"


def test_count_re_discriminates():
    assert COUNT_RE.search("全套 165 passed / 2 skipped")
    assert COUNT_RE.search("tests/ 165p/2s")
    assert not COUNT_RE.search("实测 178 行 / 22 路径")


def test_outside_migrated_drops_only_that_section():
    t = "# t\n\n> note\n\n## 迁移原文（基线）\n\n5 passed\n\n## 后续新增\n\n6 passed\n"
    assert "5 passed" not in outside_migrated(t) and "6 passed" in outside_migrated(t)


# ---------------------------------------------------------------- 判据在小树上能报出违规（入库的判别力）

@pytest.mark.parametrize("mutate,needle", [
    pytest.param(lambda t: (t / RULES_REL / "extra.md").write_text("x", encoding="utf-8"),
                 "缺于 ['映射表', 'git 跟踪']", id="untracked-rule"),
    pytest.param(lambda t: _sub(t / "CLAUDE.md", "| 测试方法 | `.claude/rules/testing.md` |",
                                "| 测试方法 | `.claude/rules/testing.md` |\n| 幽灵 | `.claude/rules/ghost.md` |"),
                 "ghost.md: 缺于", id="table-extra-row"),
    pytest.param(lambda t: ((t / RULES_REL / "sub").mkdir(), (t / RULES_REL / "sub" / "x.md").write_text(
        "x", encoding="utf-8")), "只允许", id="nested-rule"),
])
def test_mapping_problems_discriminate(tmp_path, tracked, mutate, needle):
    root = _mini(tmp_path)
    assert mapping_problems(root, tracked) == []
    mutate(root)
    assert any(needle in p for p in mapping_problems(root, tracked)), mapping_problems(root, tracked)


def test_rules_dir_editor_litter_is_reported_not_crashing(tmp_path, tracked):
    root = _mini(tmp_path)
    (root / RULES_REL / ".testing.md.swp").write_bytes(b"\xff\xfe\x00binary")
    assert frontmatter_problems(root) == []          # 不因解码失败崩溃
    assert any(".swp" in p for p in mapping_problems(root, tracked))


def test_frontmatter_problems_discriminate(tmp_path):
    root = _mini(tmp_path)
    assert frontmatter_problems(root) == []
    _sub(root / RULES_REL / "hooks-runtime.md", '"hooks/**/*"', '"hooks/**"')
    assert any("任意深度" in p for p in frontmatter_problems(root))
    t = root / RULES_REL / "testing.md"
    t.write_text(rule_body(_read(root, f"{RULES_REL}/testing.md")), encoding="utf-8")
    assert any("testing.md: frontmatter" in p for p in frontmatter_problems(root))


def test_glob_match_and_coverage_problems_discriminate(tmp_path, tracked):
    root = _mini(tmp_path)
    assert glob_match_problems(root, tracked) == [] and coverage_problems(root, tracked) == []
    _sub(root / RULES_REL / "metrics.md", '"skills/vault-loader/scripts/analyze_metrics.py"',
         '"skills/vault-loader/scripts/analyze_metrics_gone.py"')
    assert any("gone" in p for p in glob_match_problems(root, tracked))
    _sub(root / RULES_REL / "metrics.md", "paths:\n", 'paths:\n  - "skills/vault-loader/scripts/_state.py"\n')
    assert any("_state.py: 被 3 份" in p for p in coverage_problems(root, tracked))


def test_volume_problems_discriminate(tmp_path, tracked):
    root = _mini(tmp_path)
    assert volume_problems(root, tracked) == []
    assert any("单份上限" in p for p in volume_problems(root, tracked, body_cap=100))
    assert any("读一次带回" in p for p in volume_problems(root, tracked, per_file_cap=100))


def test_pointer_and_index_problems_discriminate(tmp_path):
    root = _mini(tmp_path)
    assert pointer_problems(root) == [] and index_pointer_problems(root) == []
    (root / RULES_REL / "metrics.md").rename(root / RULES_REL / "Metrics.md")
    assert any("metrics.md" in p for p in pointer_problems(root))
    root2 = _mini(tmp_path / "b")
    text = _read(root2, "CLAUDE.md")
    head, body = text.split("## 硬约束索引", 1)
    (root2 / "CLAUDE.md").write_text(head + "## 硬约束索引" + body.replace(".claude/rules/metrics.md", "X"),
                                     encoding="utf-8")
    assert any("metrics.md: 索引区" in p for p in index_pointer_problems(root2))


def test_leak_and_count_problems_discriminate(tmp_path):
    root = _mini(tmp_path)
    rels = ["CLAUDE.md", *rule_md_files(root)]
    assert leak_problems(root, rels) == [] and count_problems(root) == []
    with (root / RULES_REL / "summarize-session.md").open("a", encoding="utf-8") as f:
        # 另起一节：写在「迁移原文」小节里的读数按约定豁免，新写入内容不豁免
        f.write("\n## 后续新增\n\n" + LEAK_SAMPLES["邮箱"] + "\n全套 165 passed\n")
    assert any("邮箱" in p for p in leak_problems(root, rels))
    assert any("165 passed" in p for p in count_problems(root))


def test_signal_i_guard_discriminates(tmp_path):
    root = _mini(tmp_path)
    mod = _load_signal_collect()
    assert mod.collect_signal_i_project_claude_md(root).disabled is False
    with (root / "CLAUDE.md").open("a", encoding="utf-8") as f:
        f.write("\n<!-- vault-loader: disable -->\n")
    assert mod.collect_signal_i_project_claude_md(root).disabled is True


def test_tracked_fails_on_broken_checkout(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    (tmp_path / ".git").write_text("gitdir: does-not-exist\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception):
        _tracked(tmp_path)


def test_tracked_skips_copy_nested_in_another_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    outer = tmp_path / "outer"
    copy = outer / "cache" / "plugin"
    copy.mkdir(parents=True)
    (copy / "CLAUDE.md").write_text("x", encoding="utf-8")
    _git(outer, "init", "-q")
    _git(outer, "add", "cache/plugin/CLAUDE.md")
    with pytest.raises(pytest.skip.Exception):
        _tracked(copy)


# ---------------------------------------------------------------- 对仓库真实状态的断言

def test_convention_declares_body_location_sentence():
    sec = convention_section(_read(ROOT, "CLAUDE.md"))
    # summarize-session 1.4.1 只认「明确写出规则正文存放位置」的约定（SKILL.md 第二步），靠的就是这句
    assert re.search(r"^- \*\*规则正文写入仓库根下 `\.claude/rules/` 的对应文件\*\*", sec, re.M), \
        "写入约定缺少「规则正文写入仓库根下 .claude/rules/ 的对应文件」声明句，1.4.1 会按无约定整段追加"


def test_summarize_session_still_keys_on_explicit_body_location():
    # 两端都钉：SKILL.md 判据措辞一改，这里提醒复核 CLAUDE.md 的声明句还满不满足
    assert "只有明确写出了规则正文存放位置的才算约定" in _read(ROOT, "skills/summarize-session/SKILL.md")


def test_rules_mapping_git_and_disk_agree(tracked):
    probs = mapping_problems(ROOT, tracked)
    assert not probs, "映射表 / git 跟踪 / 磁盘三方不一致：\n" + "\n".join(probs)


def test_every_rule_file_is_path_scoped_with_anchored_globs():
    probs = frontmatter_problems(ROOT)
    assert not probs, "\n".join(probs)


def test_every_glob_matches_a_tracked_file(tracked):
    probs = glob_match_problems(ROOT, tracked)
    assert not probs, "\n".join(probs)


def test_no_file_is_covered_by_more_than_two_rules(tracked):
    probs = coverage_problems(ROOT, tracked)
    assert not probs, "读这些文件会一次拉回过多正文：\n" + "\n".join(probs)


def test_rule_volume_within_caps(tracked):
    probs = volume_problems(ROOT, tracked)
    assert not probs, "\n".join(probs)


def test_full_union_vs_pre_split_is_reported():
    """「全部 rules + CLAUDE.md 不比迁移前贵」只报告不硬拦（用户 2026-09-29 确认）。"""
    total = _u16(_read(ROOT, "CLAUDE.md")) + sum(_u16(rule_body(_read(ROOT, r))) for r in rule_md_files(ROOT))
    if total > PRE_SPLIT_RESIDENT:
        warnings.warn(f"CLAUDE.md + 全部 rules 正文 = {total}，已超过迁移前常驻 {PRE_SPLIT_RESIDENT}："
                      f"触及全部子系统的会话已比迁移前更贵", UserWarning)


def test_rule_pointers_in_claude_md_resolve():
    probs = pointer_problems(ROOT)
    assert not probs, "\n".join(probs)


def test_every_rule_file_has_an_index_pointer():
    probs = index_pointer_problems(ROOT)
    assert not probs, "\n".join(probs)


def test_claude_md_within_budget():
    n = _u16(_read(ROOT, "CLAUDE.md"))
    assert n <= CLAUDE_MD_BUDGET, (
        f"CLAUDE.md 为 {n} UTF-16 单元，超过上限 {CLAUDE_MD_BUDGET}（余量 {CLAUDE_MD_BUDGET - n}）。"
        f"按「写入约定」合并同组索引行或把正文挪进 .claude/rules/；调整上限需用户确认。")


@pytest.mark.parametrize("referrer,phrase", CLAUDE_REFS)
def test_phrases_referenced_by_comments_stay_in_claude_md(referrer, phrase):
    assert phrase in _read(ROOT, referrer), f"{referrer} 已不再引用「{phrase}」，请从 CLAUDE_REFS 删除该项"
    assert phrase in _read(ROOT, "CLAUDE.md"), f"{referrer} 按名引用 CLAUDE.md「{phrase}」，但已找不到"


@pytest.mark.parametrize("referrer,phrase", UNION_REFS)
def test_phrases_referenced_by_comments_stay_findable(referrer, phrase):
    assert phrase in _read(ROOT, referrer), f"{referrer} 已不再引用「{phrase}」，请从 UNION_REFS 删除该项"
    pool = _read(ROOT, "CLAUDE.md") + "".join(_read(ROOT, r) for r in rule_md_files(ROOT))
    assert phrase in pool, f"{referrer} 引用的「{phrase}」在 CLAUDE.md 与 rules 里都找不到"


def test_scanned_references_resolve(tracked):
    pool = _read(ROOT, "CLAUDE.md") + "".join(_read(ROOT, r) for r in rule_md_files(ROOT))
    bad = []
    for rel in tracked:
        if rel == "CLAUDE.md" or rel.startswith(RULES_REL + "/") or rel == "tests/test_claude_md_layout.py" \
                or not rel.endswith((".py", ".md", ".cmd", ".sh")):
            continue
        for ph in REF_RE.findall(_read(ROOT, rel)):
            if ph not in pool and (rel, ph) not in REF_EXEMPT:
                bad.append(f"{rel}: 「{ph}」")
    assert not bad, "代码/文档按名引用 CLAUDE.md，但 CLAUDE.md 与 rules 里都找不到：\n" + "\n".join(bad)


def test_ref_exempt_entries_are_still_dangling():
    pool = _read(ROOT, "CLAUDE.md") + "".join(_read(ROOT, r) for r in rule_md_files(ROOT))
    stale = [k for k in REF_EXEMPT if k[1] not in _read(ROOT, k[0]) or k[1] in pool]
    assert not stale, f"这些豁免已不需要（引用已删除或已可解析），从 REF_EXEMPT 删掉：{stale}"


def test_rule_name_regex_matches_gitignore_admission(tracked):
    out = _git(ROOT, "check-ignore", "--no-index", "--stdin",
               stdin="\n".join(RULES_ADMISSION_OK + RULES_ADMISSION_BAD).encode())
    ignored = set(out.stdout.decode("utf-8", "replace").split())
    samples = RULES_ADMISSION_OK + RULES_ADMISSION_BAD
    by_git = {p for p in samples if p not in ignored}
    by_regex = {p for p in samples if RULE_NAME_RE.fullmatch(p)}
    assert by_git == by_regex == set(RULES_ADMISSION_OK), (
        f".gitignore 放行 {sorted(by_git)}，RULE_NAME_RE 放行 {sorted(by_regex)}，应只放行 {RULES_ADMISSION_OK}")


def test_instruction_files_do_not_trigger_vault_loader_signal_i():
    res = _load_signal_collect().collect_signal_i_project_claude_md(ROOT)
    assert res.disabled is False, "CLAUDE.md 出现了能生效的停用注释，vault-loader 会在本仓库静默停用"
    assert not res.tags and not res.extra_paths, f"出现了能生效的 tags / extra_paths 注释：{res.tags} {res.extra_paths}"


def test_distributed_instruction_files_have_no_private_markers(tracked):
    probs = leak_problems(ROOT, leak_universe(tracked))
    assert not probs, "随公开发布分发的 Markdown 里出现私人标识：\n" + "\n".join(probs)


def test_instruction_files_carry_no_test_counts():
    probs = count_problems(ROOT)
    assert not probs, "测试读数写进 commit message，不写入 CLAUDE.md 与 rules（写入约定 ①）：\n" + "\n".join(probs)
