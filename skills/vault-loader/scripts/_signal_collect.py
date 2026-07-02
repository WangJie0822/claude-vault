"""信号采集模块。

信号源：
- A：project_root basename → 项目笔记/<basename>/ 直接命中
- B：cwd 关键词 → tag 映射（按完整 cwd 路径子串匹配，刻意用 cwd 非 project_root）
- D：最近 commit 关键词。**方案 B'' 后无生产调用方**（旧 SessionStart 打分用，已废；
  prompt_submit 不传 commit_keywords）；保留仅供单测/未来扩展，勿误以为生效。
- F：同项目近 N 天工作日志（按 project_root basename 匹配）
- I：项目 CLAUDE.md vault-loader 注释
- J：UserPromptSubmit prompt 关键词
- collect_recent_commits：最近 commit 原始 oneline（SessionStart 展示用）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# ===== 信号 A =====

def collect_signal_a_project_dir(
    project_root: Path, vault_path: Path, extra_paths: list[str]
) -> set[str]:
    """扫描 项目笔记/<project_root-basename>/ 及 extra_paths 下所有 .md 文件，
    返回相对 vault_path 的路径集合。

    project_root 由调用方决定（SessionStart 传 git 根，使子目录启动也取仓库名）。"""
    paths: set[str] = set()

    basename = project_root.name
    candidates = [vault_path / "项目笔记" / basename]
    candidates.extend(vault_path / extra for extra in extra_paths)

    for d in candidates:
        if not d.exists() or not d.is_dir():
            continue
        for md in d.rglob("*.md"):
            # 排除系统生成的索引文件(根索引 未分类 索引.md / 文件名==父目录名+' 索引.md' / 遗留 INDEX.md)
            if md.name in ("INDEX.md", "未分类 索引.md") or md.name == md.parent.name + " 索引.md":
                continue
            rel = md.relative_to(vault_path)
            paths.add(str(rel).replace("\\", "/"))

    return paths


# ===== 信号 B =====

def collect_signal_b_keyword_map(
    cwd: Path, keyword_to_tags: dict[str, list[str]]
) -> set[str]:
    """cwd 绝对路径（小写）做子串匹配，命中 key 的 tags 全部合并。"""
    cwd_lower = str(cwd).lower()
    tags: set[str] = set()
    for key, mapped in keyword_to_tags.items():
        if key.lower() in cwd_lower:
            tags.update(mapped)
    return tags


# ===== 信号 I =====

@dataclass
class ProjectClaudeMdResult:
    tags: set[str] = field(default_factory=set)
    extra_paths: list[str] = field(default_factory=list)
    disabled: bool = False


_DISABLE_RE = re.compile(r"<!--\s*vault-loader:\s*disable\s*-->")
_TAGS_RE = re.compile(r"<!--\s*vault-loader:\s*tags=\[([^\]]*)\]\s*-->")
_PATHS_RE = re.compile(r"<!--\s*vault-loader:\s*extra_paths=\[([^\]]*)\]\s*-->")


def _split_comma_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def collect_signal_i_project_claude_md(project_root: Path) -> ProjectClaudeMdResult:
    """读 project_root/CLAUDE.md，提取 vault-loader 注释。"""
    result = ProjectClaudeMdResult()
    claude_md = project_root / "CLAUDE.md"
    if not claude_md.exists():
        return result

    try:
        text = claude_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    except UnicodeDecodeError:
        return result

    if _DISABLE_RE.search(text):
        result.disabled = True

    for m in _TAGS_RE.finditer(text):
        result.tags.update(_split_comma_list(m.group(1)))

    for m in _PATHS_RE.finditer(text):
        result.extra_paths.extend(_split_comma_list(m.group(1)))

    return result


# ===== 信号 D =====

import re as _re
import subprocess
import time

# 结构化 commit：[类型|模块|功能][影响范围]描述
_STRUCT_COMMIT_RE = _re.compile(r"\[([^\]]+)\]")

# 英文 token：≥4 字母（避免 "for", "the"）
_EN_TOKEN_RE = _re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")
# 中文 token：≥3 字连续中文
_CN_TOKEN_RE = _re.compile(r"[一-鿿]{3,}")


def collect_signal_d_commit_keywords(cwd: Path) -> set[str]:
    """从 `git log --oneline -10` 提取关键词。

    优先级：[类型|模块|功能] 中的"模块"和"功能"段；其次标题中的 token。
    非 git 仓库 / 空 / 超时 → 返回空集合。

    注意：方案 B'' 后**无生产调用方**（旧 SessionStart 打分用，已废弃；
    展示近期提交改用 collect_recent_commits）。保留仅供单测/未来扩展。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "log", "--oneline", "-10"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return set()

    if result.returncode != 0:
        return set()

    keywords: set[str] = set()

    for line in result.stdout.splitlines():
        # commit hash 后面的部分
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        title = parts[1]

        # 提取所有 [...] 段
        for m in _STRUCT_COMMIT_RE.finditer(title):
            inner = m.group(1)
            # 第一个段（"类型|模块|功能"）按 | 切分，第 2-3 段是模块/功能
            if "|" in inner:
                segs = [s.strip() for s in inner.split("|")]
                for s in segs[1:]:  # 跳过"类型"
                    if s and len(s) >= 3:
                        keywords.add(s.lower())

        # 标题中英文/中文 token
        for m in _EN_TOKEN_RE.finditer(title):
            keywords.add(m.group(0).lower())
        for m in _CN_TOKEN_RE.finditer(title):
            keywords.add(m.group(0))

    return keywords


def collect_recent_commits(cwd: Path, max_commits: int) -> list[str]:
    """返回最近 max_commits 条 commit 的原始 oneline 字符串（用于 SessionStart 展示）。

    非 git 仓库 / 空 / 超时 → 空列表。沿用 D 信号子进程模式（list 形式无注入、
    timeout=1.0、utf-8/errors=replace）。
    """
    if max_commits <= 0:
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "log", "--oneline", "-n", str(max_commits)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


# ===== 信号 F =====

@dataclass
class WorklogResult:
    paths: list[str] = field(default_factory=list)        # 相对 vault 的路径
    keywords: set[str] = field(default_factory=set)       # 条目标题提取的关键词


_WORKLOG_PROJECT_FRONTMATTER_RE = _re.compile(r"^项目:\s*(.+)$", _re.MULTILINE)
_WORKLOG_PROJECT_BODY_RE = _re.compile(r"\*\*项目\*\*:\s*(.+)$", _re.MULTILINE)
_WORKLOG_ENTRY_TITLE_RE = _re.compile(r"^##\s+\d+:\d+\s+~\s+\d+:\d+\s+\|[^|]+\|\s*(.+)$", _re.MULTILINE)


def collect_signal_f_recent_worklogs(
    project_root: Path, vault_path: Path, days: int
) -> WorklogResult:
    """扫描 vault/工作日志/*.md，过滤 mtime 在 days 天内、frontmatter 或正文标记
    项目为 project_root.name 的文件，输出路径与条目关键词。

    project_root 由调用方决定（SessionStart 传 git 根）。"""
    result = WorklogResult()
    worklog_dir = vault_path / "工作日志"
    if not worklog_dir.exists():
        return result

    project_name = project_root.name
    threshold = time.time() - days * 86400

    for md in sorted(worklog_dir.rglob("*.md"), reverse=True):
        if md.name in ("INDEX.md", "未分类 索引.md") or md.name == md.parent.name + " 索引.md":
            continue
        # 过滤 macOS AppleDouble metadata 文件（._ 开头），它们是二进制资源叉，非真 markdown
        if md.name.startswith("._"):
            continue
        try:
            st = md.stat()
            if st.st_mtime < threshold:
                continue
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        except UnicodeDecodeError:
            continue

        # 项目匹配
        frontmatter_hit = any(
            project_name == m.group(1).strip()
            for m in _WORKLOG_PROJECT_FRONTMATTER_RE.finditer(text)
        )
        body_hit = any(
            project_name == m.group(1).strip()
            for m in _WORKLOG_PROJECT_BODY_RE.finditer(text)
        )
        if not (frontmatter_hit or body_hit):
            continue

        rel = str(md.relative_to(vault_path)).replace("\\", "/")
        result.paths.append(rel)

        # 提取条目标题关键词
        for m in _WORKLOG_ENTRY_TITLE_RE.finditer(text):
            title = m.group(1)
            for em in _EN_TOKEN_RE.finditer(title):
                result.keywords.add(em.group(0).lower())
            for cm in _CN_TOKEN_RE.finditer(title):
                result.keywords.add(cm.group(0))

    return result


# ===== 信号 J =====

MAX_PROMPT_BYTES = 4 * 1024  # 仅读 prompt 前 4 KB


def _strip_slash_command(prompt: str) -> str:
    """若 prompt 去左空白后以 '/' 开头，丢弃首个空白分隔 token（slash 命令名，
    含 /cmd 与 /plugin:cmd），返回其后正文；纯命令无正文返回空串。"""
    stripped = prompt.lstrip()
    if not stripped.startswith("/"):
        return prompt
    m = re.search(r"\s", stripped)
    if m is None:
        return ""
    return stripped[m.start():]


_EN_SUBTOKEN_SPLIT_RE = re.compile(r"[_-]")
_HEX_RE = re.compile(r"[0-9a-f]+")


def _is_noise_token(tok: str) -> bool:
    """判定 token 是否为 hash/UUID 型机器噪声（不当关键词）。
    - hex+含数字 ≥8：会话 UUID/commit hash 片段（a9ee6be0 / d40d47a666e）；
      纯 hex 无数字（deadbeef）不算，保护 hex 拼写的真实词。
    - 字母数字混合 ≥16：tool-use-id 等超长 id 片段（01jlfopjalhp6zzsumd6wjtl）。
    治 task-notification 的 UUID/tool-id 碎片污染（split 放大前的源头过滤）。"""
    if len(tok) >= 8 and _HEX_RE.fullmatch(tok) and any(c.isdigit() for c in tok):
        return True
    if len(tok) >= 16 and any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok):
        return True
    return False


def _split_english_subtokens(token: str, en_subtoken_min: int) -> set[str]:
    """把含 _ / - 的英文 token 按分隔符切子片，过滤过短/纯数字/噪声碎片。
    仅用于 J 信号——治路径碎片黏连（analyze_bugs→analyze,bugs；harvest_201718→harvest）。
    不动全局 _EN_TOKEN_RE，避免外溢到 D/F 信号。"""
    parts = _EN_SUBTOKEN_SPLIT_RE.split(token)
    # B1：含空段（连续/首尾分隔符，如 flattened 项目路径 d--work-...-cashbook）不切分，
    # 避免切出 cashbook/owner 等常见词假命中。
    if any(p == "" for p in parts):
        return set()
    return {p for p in parts
            if len(p) >= en_subtoken_min and not p.isdigit() and not _is_noise_token(p)}


# ===== 第0层：CJK bigram 分词（J-private，不动全局 _CN_TOKEN_RE，避免外溢 D/F 信号）=====

# J 专用：≥2 字连续中文（全局 _CN_TOKEN_RE 是 {3,}，供 D/F 信号，保持不动）
_CN_TOKEN_RE_J = _re.compile(r"[一-鿿]{2,}")

# 函数词 bigram 停用表（闭集 108 词：汉语功能词+超泛口语词+趋向补语；
# 唯一基准 = spec 2026-07-02-vault-loader-recall-layer0-1-design.md 附录 A，不得增删）
_CJK_FUNCTION_BIGRAMS = frozenset("""
怎么 什么 为什 如何 哪里 哪个 哪些 是否 能否 可否
一下 一个 一些 一样 这个 这些 那个 那些 这里 那里 这样 那样 这么 那么 这边 那边
我们 你们 他们 她们 它们 咱们 自己 大家 别人
现在 刚才 之前 之后 以前 以后 然后 接着 首先 其次 最后 目前 已经 马上 立刻
应该 需要 必须 可以 可能 也许 大概 或者 还是 而且 并且 但是 不过 因为 所以 如果 虽然 即使 就是 不是 没有 还有 只有 只是 其实 真的 确实
继续 好的 好了 谢谢 请问 帮我 麻烦 不用 不要 知道 觉得 认为 感觉 希望 想要
时候 东西 事情 问题 地方 看看 试试
出来 起来 下来 上去 进去 出去 回来 过来 过去
""".split())


def _split_cjk_bigrams(run: str) -> list[str]:
    """CJK 连续段 → 滑窗 bigram（保序）：len==2 取自身，≥3 切相邻 bigram；停用表过滤。
    只治查询侧分词，不建词典（dict-free，规避分析文档 F2 词典噪声）。"""
    toks = [run] if len(run) == 2 else [run[i:i + 2] for i in range(len(run) - 1)]
    return [t for t in toks if t not in _CJK_FUNCTION_BIGRAMS]


def is_pure_cjk_keywords(keywords: list[str]) -> bool:
    """关键词集是否全部为 CJK token（触发点1纯 CJK 放宽的判定，供 prompt_submit_load）。"""
    return bool(keywords) and all(_CN_TOKEN_RE_J.fullmatch(k) for k in keywords)


def collect_signal_j_prompt_keywords(
    prompt: str,
    strip_slash_command: bool = True,
    split_english_token: bool = True,
    en_subtoken_min: int = 4,
    split_cjk_bigram: bool = True,
    max_keywords: int | None = None,
) -> set[str]:
    """从用户 prompt 提取关键词（单趟位置扫描 → 真文档序）。
    - strip_slash_command：剥首个 slash 命令名 token（默认 True）
    - split_english_token：含 _/- 英文 token 按 [_-] 再切分（默认 True）
    - en_subtoken_min：英文子片最小长度（默认 4；3 经实证为召回灾难）
    - split_cjk_bigram：CJK ≥2 字段滑窗 bigram + 停用表（默认 True；False=旧 mega-token {3,} 行为）
    - max_keywords：头尾文档序截断（first ⌈N/2⌉ + last ⌊N/2⌋，None/0=不截断）——
      纯 first-N 会在「长粘贴+末尾提问」丢光提问词（评审 R1 实证），故两端都保留
    - 仅读前 4 KB；英文 ≥4 字母 lowercase
    """
    if not prompt:
        return set()

    if strip_slash_command:
        prompt = _strip_slash_command(prompt)

    truncated = prompt[:MAX_PROMPT_BYTES]

    # 单趟：EN 与 CJK 各自 finditer 后按 match 起始位置归并（真文档序——旧双趟结构
    # 英文恒排前，混合 prompt 截断时会系统性丢中文）
    events: list[tuple[int, list[str]]] = []
    for m in _EN_TOKEN_RE.finditer(truncated):
        tok = m.group(0).lower()
        toks: list[str] = [] if _is_noise_token(tok) else [tok]
        if split_english_token:
            toks += sorted(_split_english_subtokens(tok, en_subtoken_min) - {tok})
        events.append((m.start(), toks))
    cn_re = _CN_TOKEN_RE_J if split_cjk_bigram else _CN_TOKEN_RE
    for m in cn_re.finditer(truncated):
        run = m.group(0)
        events.append((m.start(), _split_cjk_bigrams(run) if split_cjk_bigram else [run]))
    events.sort(key=lambda x: x[0])

    ordered: list[str] = []
    seen: set[str] = set()
    for _, toks in events:
        for t in toks:
            if t not in seen:
                seen.add(t)
                ordered.append(t)

    if max_keywords and len(ordered) > max_keywords:
        hn = (max_keywords + 1) // 2
        tn = max_keywords // 2
        head = ordered[:hn]
        head_set = set(head)
        tail = [t for t in (ordered[-tn:] if tn else []) if t not in head_set]
        ordered = (head + tail)[:max_keywords]

    return set(ordered)
